import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deployment" / "deploy.sh"
ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
FOUNDRY_ID = (
    "/subscriptions/foundry-sub/resourceGroups/foundry-rg"
    "/providers/Microsoft.CognitiveServices/accounts/foundry-account"
)
ENVIRONMENT_ID = (
    "/subscriptions/app-sub/resourceGroups/rg-agents"
    "/providers/Microsoft.App/managedEnvironments/ai-cost-control"
)

FAKE_CLI = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
tool = Path(sys.argv[0]).name
log = Path(os.environ["CLI_LOG"])
previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
with log.open("a") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")

def value(flag):
    return args[args.index(flag) + 1] if flag in args else ""

command = " ".join(args[:3])
if os.environ.get("FAIL_COMMAND") and command.startswith(os.environ["FAIL_COMMAND"]):
    print("Simulated Azure authorization failure", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["extension", "show"] and "--subscription" in args:
    sys.exit("extension show does not accept --subscription")

if tool == "curl":
    print('{"status":"ok"}')
elif args[:2] == ["containerapp", "show"]:
    query = value("--query")
    if query == "identity.principalId":
        print(value("--name") + "-principal")
    elif query == "properties.managedEnvironmentId":
        print(os.environ["ENVIRONMENT_ID"])
    elif query == "properties.configuration.ingress.fqdn":
        print(value("--name") + ".example.azurecontainerapps.io")
    else:
        sys.exit("Unexpected show query: " + query)
elif args[:3] == ["containerapp", "env", "list"]:
    if any(call[1:4] == ["containerapp", "env", "create"] for call in previous):
        print(os.environ["ENVIRONMENT_ID"])
elif args[:3] == ["containerapp", "env", "create"]:
    print(os.environ["ENVIRONMENT_ID"])
elif args[:2] == ["containerapp", "list"]:
    for call in previous:
        if call[1:3] == ["containerapp", "create"]:
            name = call[call.index("--name") + 1]
            if "name=='" + name + "'" in value("--query"):
                print("/apps/" + name)
                break
elif args[:3] == ["role", "assignment", "list"]:
    for call in previous:
        if call[1:4] == ["role", "assignment", "create"]:
            principal = call[call.index("--assignee-object-id") + 1]
            if principal in value("--query"):
                print("/assignments/" + principal)
                break
elif not any(
    args[:len(command.split())] == command.split()
    for command in (
        "account show", "extension show", "resource show", "provider register", "group create",
        "containerapp create", "containerapp update", "containerapp identity assign",
        "containerapp ingress enable", "containerapp revision set-mode", "role assignment create",
        "containerapp secret set",
    )
):
    sys.exit("Unexpected Azure command: " + " ".join(args))
"""


@pytest.fixture
def deployment_cli(tmp_path):
    for tool in ("az", "curl"):
        executable = tmp_path / tool
        executable.write_text(f"#!/usr/bin/env python3\n{FAKE_CLI}")
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CLI_LOG": str(tmp_path / "commands.jsonl"),
        "ENVIRONMENT_ID": ENVIRONMENT_ID,
        "AZURE_SUBSCRIPTION_ID": "app-sub",
        "RESOURCE_GROUP": "rg-agents",
        "LOCATION": "swedencentral",
        "FOUNDRY_RESOURCE_ID": FOUNDRY_ID,
        "OPENAI_BASE_URL": "https://finops4ai.azure-api.net/finopsforai/openai/v1",
        "RISK_IMAGE": "ghcr.io/demo/project-risk:sha-123",
        "CUSTOMER_IMAGE": "ghcr.io/demo/project-customer:sha-123",
    }
    for key in (
        "CONTAINER_APPS_ENVIRONMENT",
        "RISK_APP_NAME",
        "CUSTOMER_APP_NAME",
        "RISK_MODEL_DEPLOYMENT",
        "CUSTOMER_MODEL_DEPLOYMENT",
        "RISK_OPENAI_BASE_URL",
        "CUSTOMER_OPENAI_BASE_URL",
        "FOUNDRY_PROJECT_ENDPOINT",
        "FAIL_COMMAND",
        "MAX_RETRIES",
        "RISK_MAX_RETRIES",
        "CUSTOMER_MAX_RETRIES",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "APIM_SERVICE_ID",
    ):
        env.pop(key, None)
    return env


def run_deployment(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
    )


def commands(env):
    log = Path(env["CLI_LOG"])
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_deploys_both_agents_with_managed_identities(deployment_cli):
    result = run_deployment(deployment_cli)
    assert result.returncode == 0, result.stderr
    calls = commands(deployment_cli)
    created = [call for call in calls if call[1:3] == ["containerapp", "create"]]
    assert len(created) == 2
    for call, name, prefix, port in zip(
        created,
        ("risk-agent", "customer-agent"),
        ("RISK", "CUSTOMER"),
        ("8001", "8002"),
        strict=True,
    ):
        assert call[call.index("--name") + 1] == name
        assert "--system-assigned" in call
        assert call[call.index("--environment") + 1] == ENVIRONMENT_ID
        assert call[call.index("--ingress") + 1] == "external"
        assert call[call.index("--target-port") + 1] == port
        assert call[call.index("--max-replicas") + 1] == "1"
        assert f"{prefix}_AUTH_MODE=managed_identity" in call
        assert f"{prefix}_MANAGED_IDENTITY_CLIENT_ID=" in call
        assert f"{prefix}_MAX_RETRIES=0" in call
        assert f"{prefix}_OPENAI_BASE_URL={deployment_cli['OPENAI_BASE_URL']}" in call
        assert not any("FOUNDRY_PROJECT_ENDPOINT" in argument for argument in call)
        expected_url = f"https://{name}.example.azurecontainerapps.io"
        assert any(f"{prefix}_PUBLIC_URL={expected_url}" in update for update in calls)
        assert any(update[0] == "curl" and f"{expected_url}/healthz" in update for update in calls)
    roles = [call for call in calls if call[1:4] == ["role", "assignment", "create"]]
    assert len(roles) == 2
    for call in roles:
        assert call[call.index("--scope") + 1] == FOUNDRY_ID
        assert call[call.index("--role") + 1] == ROLE_ID
        assert call[call.index("--assignee-principal-type") + 1] == "ServicePrincipal"
    account_lookup = next(call for call in calls if call[1:3] == ["resource", "show"])
    assert account_lookup[account_lookup.index("--ids") + 1] == FOUNDRY_ID
    for call in calls:
        if call[0] == "az" and call[1] == "extension":
            assert "--subscription" not in call
        elif call[0] == "az":
            subscription = "foundry-sub" if call[1] in ("resource", "role") else "app-sub"
            assert call[call.index("--subscription") + 1] == subscription


def test_rerun_reuses_resources_and_roles(deployment_cli):
    for _ in range(2):
        result = run_deployment(deployment_cli)
        assert result.returncode == 0, result.stderr
    calls = commands(deployment_cli)
    assert sum(call[1:4] == ["containerapp", "env", "create"] for call in calls) == 1
    assert sum(call[1:3] == ["containerapp", "create"] for call in calls) == 2
    assert sum(call[1:4] == ["role", "assignment", "create"] for call in calls) == 2
    assert sum(call[1:4] == ["containerapp", "identity", "assign"] for call in calls) == 2
    updates = [call for call in calls if call[1:3] == ["containerapp", "update"]]
    assert any(deployment_cli["CUSTOMER_IMAGE"] in call for call in updates)


def test_exception_export_uses_container_app_secret_and_retries_override(deployment_cli):
    deployment_cli["APPLICATIONINSIGHTS_CONNECTION_STRING"] = "test-connection-string"
    deployment_cli["APIM_SERVICE_ID"] = "finops4ai"
    deployment_cli["RISK_MAX_RETRIES"] = "1"
    for _ in range(2):
        result = run_deployment(deployment_cli)
        assert result.returncode == 0, result.stderr
        assert "test-connection-string" not in result.stdout + result.stderr
    calls = commands(deployment_cli)
    for prefix in ("RISK", "CUSTOMER"):
        configured = [call for call in calls if f"{prefix}_OPENAI_BASE_URL=" in " ".join(call)]
        for call in configured:
            assert "APIM_SERVICE_ID=finops4ai" in call
            assert (
                "APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:applicationinsights-connection-string"
                in call
            )
            assert f"{prefix}_MAX_RETRIES={'1' if prefix == 'RISK' else '0'}" in call
    assert sum(call[1:4] == ["containerapp", "secret", "set"] for call in calls) == 2


@pytest.mark.parametrize("customer_override", [None, "", "https://customer.example/openai/v1"])
def test_deploys_model_endpoint_overrides(deployment_cli, customer_override):
    apim_url = "https://finops4ai.azure-api.net/finopsforai/openai/v1"
    deployment_cli["OPENAI_BASE_URL"] = apim_url
    if customer_override is not None:
        deployment_cli["CUSTOMER_OPENAI_BASE_URL"] = customer_override
    for _ in range(2):
        result = run_deployment(deployment_cli)
        assert result.returncode == 0, result.stderr
    calls = commands(deployment_cli)
    for prefix, endpoint in (
        ("RISK", apim_url),
        ("CUSTOMER", customer_override or apim_url),
    ):
        configured = [call for call in calls if f"{prefix}_MODEL_DEPLOYMENT=" in " ".join(call)]
        assert len(configured) == 2
        for call in configured:
            assert "OPENAI_BASE_URL=" in call
            assert f"{prefix}_OPENAI_BASE_URL={endpoint}" in call


def test_deploys_with_only_per_agent_endpoints(deployment_cli):
    deployment_cli.pop("OPENAI_BASE_URL")
    deployment_cli["RISK_OPENAI_BASE_URL"] = "https://risk.example/openai/v1"
    deployment_cli["CUSTOMER_OPENAI_BASE_URL"] = "https://customer.example/openai/v1"
    result = run_deployment(deployment_cli)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("RISK_IMAGE", "", "Set RISK_IMAGE"),
        ("RISK_IMAGE", "private.example/agent:latest", "publicly pullable ghcr.io"),
        ("OPENAI_BASE_URL", "", "Set OPENAI_BASE_URL"),
        ("OPENAI_BASE_URL", "http://account.openai.azure.com/openai/v1", "Invalid OpenAI"),
        ("FOUNDRY_RESOURCE_ID", f"{FOUNDRY_ID}/projects/demo", "not a project"),
        ("CUSTOMER_APP_NAME", "risk-agent", "App names must be different"),
        ("RISK_APP_NAME", "invalid'name", "Invalid resource name"),
    ],
)
def test_rejects_invalid_configuration_before_azure(deployment_cli, key, value, error):
    deployment_cli[key] = value
    result = run_deployment(deployment_cli)
    assert result.returncode != 0
    assert error in result.stderr
    assert commands(deployment_cli) == []


def test_azure_failure_stops_deployment(deployment_cli):
    deployment_cli["FAIL_COMMAND"] = "role assignment create"
    result = run_deployment(deployment_cli)
    assert result.returncode != 0
    assert "Simulated Azure authorization failure" in result.stderr
    assert "Deployment failed at line" in result.stderr
    assert not any("customer-agent" in call for call in commands(deployment_cli))
    assert not any(call[0] == "curl" for call in commands(deployment_cli))


def test_help_does_not_require_configuration(deployment_cli):
    deployment_cli.pop("FOUNDRY_RESOURCE_ID")
    result = run_deployment(deployment_cli, "--help")
    assert result.returncode == 0
    assert "WARNING: External HTTPS ingress" in result.stdout
    assert commands(deployment_cli) == []
