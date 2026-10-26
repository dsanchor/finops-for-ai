"""Create APIM developer users and optional product subscriptions from identities CSV."""

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote, urlsplit

from clients.identities import Identity, load_identities

API_VERSION = "2024-05-01"
ARM_ENDPOINT = "https://management.azure.com"
logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    pass


class ApimClient:
    def __init__(self, subscription_id: str, resource_group: str, service_name: str):
        self.subscription_id = subscription_id
        self.resource_id = (
            f"/subscriptions/{quote(subscription_id, safe='')}"
            f"/resourceGroups/{quote(resource_group, safe='')}"
            f"/providers/Microsoft.ApiManagement/service/{quote(service_name, safe='')}"
        )
        self.url = ARM_ENDPOINT + self.resource_id

    def request(
        self, method: str, url: str, body: dict | None = None, *, read_response: bool = False
    ) -> dict:
        read_response = read_response or method == "get"
        command = [
            "az",
            "rest",
            "--method",
            method,
            "--url",
            url,
            "--subscription",
            self.subscription_id,
            "--only-show-errors",
            "--output",
            "json" if read_response else "none",
        ]
        if body is not None:
            command.extend(["--body", json.dumps(body)])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise ProvisioningError(
                f"Azure CLI {method.upper()} {url} failed "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        if not read_response:
            return {}
        content = json.loads(result.stdout)
        if not isinstance(content, dict):
            raise ProvisioningError(f"Expected a JSON object from {url}")
        return content

    def entity_url(self, collection: str, entity_id: str) -> str:
        return f"{self.url}/{collection}/{quote(entity_id, safe='')}?api-version={API_VERSION}"

    def list_entities(self, collection: str) -> list[dict]:
        collection_url = f"{self.url}/{collection}"
        next_url = f"{collection_url}?api-version={API_VERSION}"
        visited = set()
        entities = []
        while next_url:
            parsed = urlsplit(next_url)
            expected = urlsplit(collection_url)
            if (
                parsed.scheme != expected.scheme
                or parsed.netloc != expected.netloc
                or parsed.path.casefold() != expected.path.casefold()
                or next_url in visited
            ):
                raise ProvisioningError(f"Invalid pagination link for {collection}")
            visited.add(next_url)
            page = self.request("get", next_url)
            values = page.get("value")
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise ProvisioningError(f"Invalid list response for {collection}")
            entities.extend(values)
            next_url = page.get("nextLink")
            if next_url is not None and not isinstance(next_url, str):
                raise ProvisioningError(f"Invalid pagination link for {collection}")
        return entities


@dataclass(frozen=True)
class UserPlan:
    email: str
    user_id: str
    create_user: bool
    subscription_id: str | None = None
    product_id: str | None = None
    existing_subscription: dict | None = None


def parse_products(value: str) -> list[str]:
    products = [item.strip() for item in value.split(",")]
    if any(not item or re.search(r"[/\\?#&+<>*\s:]", item) for item in products):
        raise argparse.ArgumentTypeError(
            "--subscriptions must contain comma-separated product IDs, e.g. premium,standard,free"
        )
    if len(set(products)) != len(products):
        raise argparse.ArgumentTypeError("--subscriptions must not contain duplicate product IDs")
    return products


def unique_emails(identities: list[Identity], path: Path) -> list[str]:
    emails = {}
    for row, identity in enumerate(identities, 1):
        email = identity.user
        parts = email.split("@")
        if len(parts) != 2:
            raise ValueError(f"{path}:{row}: user must be an email address")
        local, domain = parts
        if (
            len(email) > 254
            or len(local) > 64
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in domain.split(".")
            )
        ):
            raise ValueError(f"{path}:{row}: user must be a valid email address")
        emails.setdefault(email.casefold(), email)
    return list(emails.values())


def relative_reference(value: str, client: ApimClient) -> str:
    if value.casefold().startswith(client.resource_id.casefold() + "/"):
        return value[len(client.resource_id) :]
    return value


def plan_users(
    client: ApimClient, emails: list[str], products: list[str] | None, seed: int | None
) -> list[UserPlan]:
    for product in products or []:
        client.request("get", client.entity_url("products", product))
    users = client.list_entities("users")
    by_email = {}
    by_id = {}
    for user in users:
        email = user["properties"]["email"].casefold()
        if email in by_email:
            raise ProvisioningError(f"Multiple APIM users have the same email: {email}")
        by_email[email] = user
        by_id[user["name"]] = user
    subscriptions = (
        {item["name"]: item for item in client.list_entities("subscriptions")} if products else {}
    )
    rng = random.Random(seed)
    plans = []
    for email in emails:
        digest = hashlib.sha256(email.casefold().encode("utf-8")).hexdigest()[:24]
        existing_user = by_email.get(email.casefold())
        user_id = existing_user["name"] if existing_user else f"demo-user-{digest}"
        if not existing_user and user_id in by_id:
            raise ProvisioningError(f"Generated user ID is already in use: {user_id}")
        if existing_user and existing_user["properties"]["state"] != "active":
            raise ProvisioningError(
                f"Existing APIM user {email} is not active; activate it explicitly before retrying"
            )
        if not products:
            plans.append(UserPlan(email, user_id, existing_user is None))
            continue
        subscription_id = f"demo-sub-{digest}"
        existing_subscription = subscriptions.get(subscription_id)
        if existing_subscription:
            owner_id = existing_subscription["properties"].get("ownerId")
            owner = relative_reference(owner_id, client) if isinstance(owner_id, str) else ""
            if not existing_user or owner.casefold() != f"/users/{user_id}".casefold():
                raise ProvisioningError(
                    f"Subscription {subscription_id} belongs to another user; no changes made"
                )
            product_id = None
        else:
            product_id = rng.choice(products)
        plans.append(
            UserPlan(
                email,
                user_id,
                existing_user is None,
                subscription_id,
                product_id,
                existing_subscription,
            )
        )
    return plans


def save_key(
    path: Path,
    identities: list[Identity],
    email: str,
    key: str,
    expected_content: bytes | None,
) -> bytes:
    if not key or len(key) > 256 or any(not 33 <= ord(char) <= 126 for char in key):
        raise ProvisioningError("APIM returned an invalid primary subscription key")
    updated = [
        replace(identity, subscription_key=key)
        if identity.user.casefold() == email.casefold()
        else identity
        for identity in identities
    ]
    content = io.StringIO(newline="")
    writer = csv.writer(content)
    for identity in updated:
        writer.writerow(
            [identity.department, identity.team, identity.user, identity.subscription_key or ""]
        )
    encoded = content.getvalue().encode("utf-8")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        current_content = path.read_bytes() if path.exists() else None
        if path.is_symlink() or current_content != expected_content:
            raise ProvisioningError(
                "Destination identities CSV changed during onboarding; refusing to overwrite"
            )
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    identities[:] = updated
    return encoded


def apply_plan(
    client: ApimClient,
    plans: list[UserPlan],
    dry_run: bool,
    identities: list[Identity],
    destination: Path,
    expected_content: bytes | None,
) -> None:
    for plan in plans:
        action = "CREATE" if plan.create_user else "KEEP"
        print(f"{action} user {plan.email} ({plan.user_id})")
        if plan.create_user and not dry_run:
            client.request(
                "put",
                client.entity_url("users", plan.user_id),
                {
                    "properties": {
                        "firstName": plan.email.split("@", 1)[0],
                        "lastName": "Demo",
                        "email": plan.email,
                        "state": "active",
                    }
                },
            )
        if plan.existing_subscription:
            properties = plan.existing_subscription["properties"]
            print(
                f"KEEP subscription {plan.subscription_id}: "
                f"scope={properties['scope']}, state={properties['state']}"
            )
        elif plan.subscription_id is not None:
            print(f"CREATE subscription {plan.subscription_id}: product={plan.product_id}")
            if not dry_run:
                client.request(
                    "put",
                    client.entity_url("subscriptions", plan.subscription_id),
                    {
                        "properties": {
                            "displayName": f"Demo {plan.email}"[:100],
                            "ownerId": f"/users/{plan.user_id}",
                            "scope": f"/products/{plan.product_id}",
                            "state": "active",
                        }
                    },
                )
        if plan.subscription_id is not None and not dry_run:
            url = (
                f"{client.url}/subscriptions/{quote(plan.subscription_id, safe='')}"
                f"/listSecrets?api-version={API_VERSION}"
            )
            secrets = client.request("post", url, read_response=True)
            key = secrets.get("primaryKey")
            if not isinstance(key, str):
                raise ProvisioningError(f"APIM returned no primary key for {plan.subscription_id}")
            expected_content = save_key(destination, identities, plan.email, key, expected_content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "identities_file",
        type=Path,
        help="CSV without header: department,team,email[,subscription_key]",
    )
    for flag, variable in (
        ("--azure-subscription-id", "AZURE_SUBSCRIPTION_ID"),
        ("--resource-group", "RESOURCE_GROUP"),
        ("--service-name", "APIM_NAME"),
    ):
        default = os.getenv(variable, "").strip()
        parser.add_argument(
            flag, default=default, required=not default, help=f"Or export {variable}"
        )
    parser.add_argument(
        "--subscriptions",
        type=parse_products,
        help="Existing product IDs; save keys to <identities_file>.subs",
    )
    parser.add_argument("--seed", type=int, help="Seed for reproducible random product assignments")
    parser.add_argument("--dry-run", action="store_true", help="Read and plan only; do not write")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    destination = args.identities_file.with_name(args.identities_file.name + ".subs")
    try:
        identities = load_identities(args.identities_file)
        emails = unique_emails(identities, args.identities_file)
        expected_content = None
        if args.subscriptions:
            if args.identities_file.is_symlink() or destination.is_symlink():
                raise ValueError("Use regular identities CSV files, not symbolic links")
            if destination.exists():
                expected_content = destination.read_bytes()
                previous_keys = {}
                for identity in load_identities(destination):
                    if identity.subscription_key is None:
                        continue
                    email = identity.user.casefold()
                    if email in previous_keys and previous_keys[email] != identity.subscription_key:
                        raise ValueError(
                            "Destination identities CSV has conflicting keys for the same email"
                        )
                    previous_keys[email] = identity.subscription_key
                identities = [
                    replace(
                        identity,
                        subscription_key=previous_keys.get(
                            identity.user.casefold(), identity.subscription_key
                        ),
                    )
                    for identity in identities
                ]
        client = ApimClient(args.azure_subscription_id, args.resource_group, args.service_name)
        plans = plan_users(client, emails, args.subscriptions, args.seed)
        if args.dry_run:
            print("DRY RUN: no resources will be modified")
        elif args.subscriptions:
            logger.warning(
                "The destination %s will contain primary subscription keys. "
                "The original CSV is not modified. Keep the destination private.",
                destination,
            )
        apply_plan(client, plans, args.dry_run, identities, destination, expected_content)
    except (OSError, ValueError, ProvisioningError) as exc:
        logger.error("APIM onboarding failed: %s", exc)
        return 1
    print(
        f"{'Planned' if args.dry_run else 'Completed'}: {len(plans)} unique users, "
        f"{sum(plan.subscription_id is not None for plan in plans)} subscriptions. "
        + (
            f" Primary keys saved in {destination} (not displayed)."
            if args.subscriptions and not args.dry_run
            else " No subscription keys retrieved."
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
