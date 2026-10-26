import argparse
import json
import os
import random
import subprocess
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from clients.identities import load_identities
from deployment.onboard_apim_users import (
    API_VERSION,
    ApimClient,
    ProvisioningError,
    main,
    parse_products,
    save_key,
)


class FakeAzure:
    def __init__(self):
        self.products = {"premium", "standard", "free"}
        self.users = {}
        self.subscriptions = {}
        self.calls = []
        self.fail_on = None
        self.secret_override = None

    def __call__(self, command, *, capture_output, text, check):
        assert command[:2] == ["az", "rest"]
        assert capture_output and text and not check
        assert command[command.index("--subscription") + 1] == "azure-sub"
        assert "--only-show-errors" in command
        method = command[command.index("--method") + 1]
        url = command[command.index("--url") + 1]
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "management.azure.com"
        assert f"api-version={API_VERSION}" in parsed.query
        path = parsed.path.split("/service/demo-apim/", 1)[1]
        body = json.loads(command[command.index("--body") + 1]) if "--body" in command else None
        self.calls.append((method, path, body))
        if self.fail_on == (method, path):
            return subprocess.CompletedProcess(command, 1, "", "Synthetic Azure error")
        response = {}
        if method == "get":
            if path == "users":
                response = {"value": list(self.users.values())}
            elif path == "subscriptions":
                response = {"value": list(self.subscriptions.values())}
            elif path.startswith("products/") and path.split("/")[1] in self.products:
                response = {"name": path.split("/")[1]}
            else:
                return subprocess.CompletedProcess(command, 1, "", "Resource not found")
        elif method == "put":
            assert command[command.index("--output") + 1] == "none"
            collection, name = path.split("/")
            entity = {"name": name, "properties": body["properties"]}
            if collection == "users":
                self.users[name] = entity
            elif collection == "subscriptions":
                self.subscriptions[name] = entity
            else:
                pytest.fail(f"Unexpected mutation: {path}")
        elif method == "post" and path.endswith("/listSecrets"):
            assert command[command.index("--output") + 1] == "json"
            subscription_id = path.split("/")[1]
            assert subscription_id in self.subscriptions
            response = (
                self.secret_override
                if self.secret_override is not None
                else {
                    "primaryKey": f"test-primary-{subscription_id}",
                    "secondaryKey": f"test-secondary-{subscription_id}",
                }
            )
        else:
            pytest.fail(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")


@pytest.fixture
def azure(monkeypatch):
    fake = FakeAzure()
    monkeypatch.setattr("deployment.onboard_apim_users.subprocess.run", fake)
    return fake


@pytest.fixture
def csv_path(tmp_path):
    path = tmp_path / "identities.csv"
    path.write_text(
        "finance,risk,alice@example.com\nretail,cards,bob@example.com\n"
        "finance,treasury,ALICE@example.com\n",
        encoding="utf-8",
    )
    return path


def arguments(csv_path, *extra):
    return [
        str(csv_path),
        "--azure-subscription-id",
        "azure-sub",
        "--resource-group",
        "demo-rg",
        "--service-name",
        "demo-apim",
        *extra,
    ]


def test_users_only_deduplicates_email_and_does_not_change_csv(azure, csv_path, capsys):
    original = csv_path.read_bytes()
    assert main(arguments(csv_path)) == 0
    assert len(azure.users) == 2
    assert {u["properties"]["firstName"] for u in azure.users.values()} == {"alice", "bob"}
    assert all(u["properties"]["lastName"] == "Demo" for u in azure.users.values())
    assert all(u["properties"]["state"] == "active" for u in azure.users.values())
    assert csv_path.read_bytes() == original
    assert not csv_path.with_name(csv_path.name + ".subs").exists()
    assert not azure.subscriptions
    assert not any(path.startswith(("products/", "subscriptions")) for _, path, _ in azure.calls)
    assert "No subscription keys retrieved" in capsys.readouterr().out


def test_creates_random_product_subscription_and_persists_primary_keys(
    azure, csv_path, capsys, caplog
):
    original = csv_path.read_bytes()
    destination = csv_path.with_name(csv_path.name + ".subs")
    assert (
        main(arguments(csv_path, "--subscriptions", "premium, standard, free", "--seed", "42")) == 0
    )
    rng = random.Random(42)
    scopes = [item["properties"]["scope"] for item in azure.subscriptions.values()]
    assert scopes == [f"/products/{rng.choice(['premium', 'standard', 'free'])}" for _ in range(2)]
    assert csv_path.read_bytes() == original
    identities = load_identities(destination)
    assert len(identities) == 3
    assert identities[0].subscription_key == identities[2].subscription_key
    assert identities[0].subscription_key != identities[1].subscription_key
    assert identities[2].department == "finance" and identities[2].team == "treasury"
    assert identities[2].user == "ALICE@example.com"
    for item in azure.subscriptions.values():
        properties = item["properties"]
        assert properties["ownerId"].removeprefix("/users/") in azure.users
        assert properties["state"] == "active"
        assert "primaryKey" not in properties and "secondaryKey" not in properties
    captured = capsys.readouterr()
    for identity in identities:
        assert identity.subscription_key.startswith("test-primary-")
        assert identity.subscription_key not in captured.out + captured.err + caplog.text
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


def test_rerun_keeps_assignments_and_keys_even_if_product_candidates_change(
    azure, csv_path, capsys
):
    destination = csv_path.with_name(csv_path.name + ".subs")
    original = csv_path.read_bytes()
    assert main(arguments(csv_path, "--subscriptions", "premium", "--seed", "42")) == 0
    first_users = json.dumps(azure.users, sort_keys=True)
    first_subscriptions = json.dumps(azure.subscriptions, sort_keys=True)
    first_csv = destination.read_bytes()
    azure.calls.clear()
    assert main(arguments(csv_path, "--subscriptions", "free", "--seed", "17")) == 0
    assert json.dumps(azure.users, sort_keys=True) == first_users
    assert json.dumps(azure.subscriptions, sort_keys=True) == first_subscriptions
    assert destination.read_bytes() == first_csv
    assert csv_path.read_bytes() == original
    assert all(method != "put" for method, _, _ in azure.calls)
    assert len([call for call in azure.calls if call[0] == "post"]) == 2
    assert "scope=/products/premium" in capsys.readouterr().out


def test_refreshes_csv_after_primary_key_rotation(azure, csv_path):
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 0
    azure.secret_override = {"primaryKey": "rotated-test-key"}
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 0
    assert all(
        identity.subscription_key == "rotated-test-key"
        for identity in load_identities(csv_path.with_name(csv_path.name + ".subs"))
    )


def test_reuses_existing_user_by_email_with_non_generated_id(azure, csv_path):
    azure.users["existing-id"] = {
        "name": "existing-id",
        "properties": {
            "email": "ALICE@example.com",
            "firstName": "Existing",
            "lastName": "Person",
            "state": "active",
        },
    }
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 0
    assert len(azure.users) == 2
    assert azure.users["existing-id"]["properties"]["firstName"] == "Existing"
    owners = [s["properties"]["ownerId"] for s in azure.subscriptions.values()]
    assert "/users/existing-id" in owners


def test_dry_run_reads_only_and_does_not_retrieve_secrets(azure, csv_path, capsys):
    original = csv_path.read_bytes()
    assert main(arguments(csv_path, "--subscriptions", "premium,standard", "--dry-run")) == 0
    assert all(method == "get" for method, _, _ in azure.calls)
    assert not azure.users and not azure.subscriptions
    assert csv_path.read_bytes() == original
    assert not csv_path.with_name(csv_path.name + ".subs").exists()
    assert "DRY RUN" in capsys.readouterr().out


def test_invalid_product_stops_before_mutation(azure, csv_path, caplog):
    original = csv_path.read_bytes()
    assert main(arguments(csv_path, "--subscriptions", "premium,missing")) == 1
    assert not azure.users and not azure.subscriptions
    assert csv_path.read_bytes() == original
    assert "Resource not found" in caplog.text


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "@example.com", "a@@example.com", "a b@example.com", "a@-invalid.com"],
)
def test_invalid_email_stops_before_azure(azure, csv_path, email, caplog):
    csv_path.write_text(f"finance,risk,{email}\n", encoding="utf-8")
    assert main(arguments(csv_path)) == 1
    assert not azure.calls
    assert "email address" in caplog.text


def test_inactive_existing_user_is_not_silently_reactivated(azure, csv_path, caplog):
    azure.users["blocked-user"] = {
        "name": "blocked-user",
        "properties": {"email": "alice@example.com", "state": "blocked"},
    }
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 1
    assert all(method == "get" for method, _, _ in azure.calls)
    assert "not active" in caplog.text


@pytest.mark.parametrize("secrets", [{}, {"primaryKey": ""}, {"primaryKey": "bad\nkey"}])
def test_missing_or_invalid_secret_does_not_corrupt_csv(azure, csv_path, secrets, caplog):
    azure.secret_override = secrets
    original = csv_path.read_bytes()
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 1
    assert csv_path.read_bytes() == original
    assert not csv_path.with_name(csv_path.name + ".subs").exists()
    assert "Completed:" not in caplog.text
    assert not list(csv_path.parent.glob(".identities.csv.*.tmp"))


def test_failed_refresh_preserves_existing_destination_keys(azure, csv_path):
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 0
    destination = csv_path.with_name(csv_path.name + ".subs")
    existing = load_identities(destination)
    original = csv_path.read_bytes()
    second_subscription = list(azure.subscriptions)[1]
    azure.fail_on = ("post", f"subscriptions/{second_subscription}/listSecrets")
    azure.secret_override = {"primaryKey": "rotated-first-test-key"}
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 1
    updated = load_identities(destination)
    assert updated[0].subscription_key == "rotated-first-test-key"
    assert updated[1].subscription_key == existing[1].subscription_key
    assert updated[2].subscription_key == "rotated-first-test-key"
    assert csv_path.read_bytes() == original


def test_destination_symlink_is_rejected_before_azure(azure, csv_path, tmp_path):
    target = tmp_path / "other.csv"
    target.write_text("private data", encoding="utf-8")
    csv_path.with_name(csv_path.name + ".subs").symlink_to(target)
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 1
    assert not azure.calls
    assert target.read_text(encoding="utf-8") == "private data"


def test_save_key_handles_new_destination_and_detects_unexpected_creation(csv_path):
    destination = csv_path.with_name(csv_path.name + ".subs")
    identities = load_identities(csv_path)
    expected = save_key(destination, identities, "alice@example.com", "test-key", None)
    assert destination.read_bytes() == expected
    with pytest.raises(ProvisioningError, match="changed during onboarding"):
        save_key(destination, identities, "bob@example.com", "another-test-key", None)
    assert destination.read_bytes() == expected
    assert not list(destination.parent.glob(".identities.csv.subs.*.tmp"))


def test_failed_subscription_can_be_retried_without_duplicate_user(azure, csv_path, caplog):
    original_runner = azure.__call__

    def fail_subscription(command, **kwargs):
        if command[command.index("--method") + 1] == "put":
            url = command[command.index("--url") + 1]
            if "/demo-apim/subscriptions/" in url:
                return subprocess.CompletedProcess(command, 1, "", "Subscription creation failed")
        return original_runner(command, **kwargs)

    with patch("deployment.onboard_apim_users.subprocess.run", fail_subscription):
        assert main(arguments(csv_path, "--subscriptions", "premium")) == 1
    assert len(azure.users) == 1
    assert not azure.subscriptions
    assert "Subscription creation failed" in caplog.text
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 0
    assert len(azure.users) == 2 and len(azure.subscriptions) == 2


def test_save_key_refuses_to_overwrite_external_csv_changes(csv_path):
    identities = load_identities(csv_path)
    original = csv_path.read_bytes()
    csv_path.write_text("finance,other,new@example.com\n", encoding="utf-8")
    changed = csv_path.read_bytes()
    with pytest.raises(ProvisioningError, match="changed during onboarding"):
        save_key(csv_path, identities, "alice@example.com", "test-key", original)
    assert csv_path.read_bytes() == changed
    assert all(identity.subscription_key is None for identity in identities)
    assert not list(csv_path.parent.glob(".identities.csv.*.tmp"))


def test_list_pagination_and_full_owner_reference(azure, csv_path, monkeypatch):
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 0
    client = ApimClient("azure-sub", "demo-rg", "demo-apim")
    for item in azure.subscriptions.values():
        item["properties"]["ownerId"] = client.resource_id + item["properties"]["ownerId"]
    real_request = ApimClient.request

    def paginated(self, method, url, *args, **kwargs):
        if method == "get" and urlsplit(url).path.endswith("/users"):
            entries = list(azure.users.values())
            if "page=2" not in url:
                return {
                    "value": entries[:1],
                    "nextLink": self.url + f"/users?api-version={API_VERSION}&page=2",
                }
            return {"value": entries[1:]}
        return real_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(ApimClient, "request", paginated)
    azure.calls.clear()
    assert main(arguments(csv_path, "--subscriptions", "free")) == 0
    assert all(method != "put" for method, _, _ in azure.calls)


def test_foreign_pagination_link_is_rejected_without_request(azure, monkeypatch):
    monkeypatch.setattr(
        ApimClient,
        "request",
        lambda *args: {"value": [], "nextLink": "https://example.com/users"},
    )
    with pytest.raises(ProvisioningError, match="pagination"):
        ApimClient("azure-sub", "demo-rg", "demo-apim").list_entities("users")
    assert not azure.calls


def test_conflicting_subscription_owner_stops_before_mutation(azure, csv_path, caplog):
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 0
    next(iter(azure.subscriptions.values()))["properties"]["ownerId"] = "/users/somebody-else"
    azure.calls.clear()
    assert main(arguments(csv_path, "--subscriptions", "premium")) == 1
    assert all(method == "get" for method, _, _ in azure.calls)
    assert "belongs to another user" in caplog.text


@pytest.mark.parametrize(
    "value", ["", "premium,,free", "premium,premium", "/products/free", "bad id"]
)
def test_invalid_product_lists_are_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_products(value)


def test_environment_configuration(azure, csv_path, monkeypatch):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "azure-sub")
    monkeypatch.setenv("RESOURCE_GROUP", "demo-rg")
    monkeypatch.setenv("APIM_NAME", "demo-apim")
    assert main([str(csv_path)]) == 0
    assert len(azure.users) == 2


def test_missing_cli_is_reported(csv_path, monkeypatch, caplog):
    def missing(*args, **kwargs):
        raise FileNotFoundError("az executable not found")

    monkeypatch.setattr("deployment.onboard_apim_users.subprocess.run", missing)
    assert main(arguments(csv_path)) == 1
    assert "az executable not found" in caplog.text
