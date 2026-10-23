#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "Deployment failed at line %s.\n" "$LINENO" >&2' ERR

if [[ "${1:-}" == "--help" ]]; then
    cat <<'HELP'
Deploy the risk and customer agents to Azure Container Apps.

Required environment variables:
  AZURE_SUBSCRIPTION_ID       Subscription for the Container Apps resources
  RESOURCE_GROUP             Container Apps resource group (created if needed)
  LOCATION                   Azure region for new resources
  FOUNDRY_RESOURCE_ID         Existing Foundry account resource ID (RBAC scope)
  FOUNDRY_PROJECT_ENDPOINT    https://<account>.services.ai.azure.com/api/projects/<project>
  RISK_IMAGE                 Public GHCR image for the risk agent
  CUSTOMER_IMAGE             Public GHCR image for the customer agent

Optional:
  CONTAINER_APPS_ENVIRONMENT  Default: ai-cost-control
  RISK_APP_NAME              Default: risk-agent
  CUSTOMER_APP_NAME          Default: customer-agent
  RISK_MODEL_DEPLOYMENT      Default: gpt-5.6-sol
  CUSTOMER_MODEL_DEPLOYMENT  Default: gpt-5.4-mini

Requires Bash, Azure CLI with the containerapp extension, curl with
--retry-all-errors support, and an authenticated Azure CLI session.
WARNING: External HTTPS ingress is enabled without client authentication.
HELP
    exit 0
fi
if (( $# != 0 )); then
    printf 'Unexpected arguments. Use --help for configuration.\n' >&2
    exit 1
fi

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

for tool in az curl; do
    command -v "$tool" >/dev/null || die "Required command not found: $tool"
done
: "${AZURE_SUBSCRIPTION_ID:?Set AZURE_SUBSCRIPTION_ID (see --help).}"
: "${RESOURCE_GROUP:?Set RESOURCE_GROUP (see --help).}"
: "${LOCATION:?Set LOCATION (see --help).}"
: "${FOUNDRY_RESOURCE_ID:?Set FOUNDRY_RESOURCE_ID (see --help).}"
: "${FOUNDRY_PROJECT_ENDPOINT:?Set FOUNDRY_PROJECT_ENDPOINT (see --help).}"
: "${RISK_IMAGE:?Set RISK_IMAGE (see --help).}"
: "${CUSTOMER_IMAGE:?Set CUSTOMER_IMAGE (see --help).}"

CONTAINER_APPS_ENVIRONMENT="${CONTAINER_APPS_ENVIRONMENT:-ai-cost-control}"
RISK_APP_NAME="${RISK_APP_NAME:-risk-agent}"
CUSTOMER_APP_NAME="${CUSTOMER_APP_NAME:-customer-agent}"
RISK_MODEL_DEPLOYMENT="${RISK_MODEL_DEPLOYMENT:-gpt-5.6-sol}"
CUSTOMER_MODEL_DEPLOYMENT="${CUSTOMER_MODEL_DEPLOYMENT:-gpt-5.4-mini}"
FOUNDRY_RESOURCE_ID="${FOUNDRY_RESOURCE_ID%/}"
FOUNDRY_PROJECT_ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT%/}"
FOUNDRY_USER_ROLE_ID="53ca6127-db72-4b80-b1b0-d745d6d5456d"

for name in "$RISK_APP_NAME" "$CUSTOMER_APP_NAME" "$CONTAINER_APPS_ENVIRONMENT"; do
    [[ "$name" =~ ^[a-z][a-z0-9-]*[a-z0-9]$ && ${#name} -le 32 && "$name" != *--* ]] \
        || die "Invalid resource name: $name (2-32 lowercase letters, digits or single hyphens)."
done
[[ "$RISK_APP_NAME" != "$CUSTOMER_APP_NAME" ]] || die "App names must be different."
[[ "$FOUNDRY_RESOURCE_ID" =~ ^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+$ ]] \
    || die "FOUNDRY_RESOURCE_ID must identify a Foundry account, not a project."
[[ "$FOUNDRY_PROJECT_ENDPOINT" =~ ^https://[a-zA-Z0-9.-]+/api/projects/[a-zA-Z0-9._-]+$ ]] \
    || die "Invalid Foundry project endpoint."
for image in "$RISK_IMAGE" "$CUSTOMER_IMAGE"; do
    [[ "$image" == ghcr.io/* && "$image" != *[[:space:]]* ]] \
        || die "Use a publicly pullable ghcr.io image: $image"
done
foundry_subscription="${FOUNDRY_RESOURCE_ID#/subscriptions/}"
foundry_subscription="${foundry_subscription%%/*}"

azure() {
    az "$@" --subscription "$AZURE_SUBSCRIPTION_ID" --only-show-errors
}

foundry() {
    az "$@" --subscription "$foundry_subscription" --only-show-errors
}

# Check the existing Foundry project before provisioning any resources.
azure account show --output none
az extension show --name containerapp --only-show-errors --output none
foundry resource show \
    --ids "$FOUNDRY_RESOURCE_ID/projects/${FOUNDRY_PROJECT_ENDPOINT##*/}" --output none
for provider in Microsoft.App Microsoft.OperationalInsights; do
    azure provider register --namespace "$provider" --wait --output none
done
azure group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

environment_id="$(azure containerapp env list --resource-group "$RESOURCE_GROUP" \
    --query "[?name=='$CONTAINER_APPS_ENVIRONMENT'].id | [0]" --output tsv)"
if [[ -z "$environment_id" ]]; then
    environment_id="$(azure containerapp env create --name "$CONTAINER_APPS_ENVIRONMENT" \
        --resource-group "$RESOURCE_GROUP" --location "$LOCATION" \
        --logs-destination none --query id --output tsv)"
fi
[[ -n "$environment_id" ]] || die "Container Apps environment ID was not returned."

deploy_agent() {
    local name="$1" prefix="$2" port="$3" image="$4" model="$5"
    local existing_id existing_environment principal_id assignment_id fqdn url
    local -a settings=(
        "FOUNDRY_PROJECT_ENDPOINT=$FOUNDRY_PROJECT_ENDPOINT"
        "${prefix}_FOUNDRY_PROJECT_ENDPOINT=$FOUNDRY_PROJECT_ENDPOINT"
        "AUTH_MODE=managed_identity"
        "${prefix}_AUTH_MODE=managed_identity"
        "MANAGED_IDENTITY_CLIENT_ID="
        "${prefix}_MANAGED_IDENTITY_CLIENT_ID="
        "${prefix}_HOST=0.0.0.0"
        "${prefix}_PORT=$port"
        "${prefix}_MODEL_DEPLOYMENT=$model"
    )

    existing_id="$(azure containerapp list --resource-group "$RESOURCE_GROUP" \
        --query "[?name=='$name'].id | [0]" --output tsv)"
    if [[ -z "$existing_id" ]]; then
        azure containerapp create --name "$name" --resource-group "$RESOURCE_GROUP" \
            --environment "$environment_id" --image "$image" --system-assigned \
            --ingress external --allow-insecure false --target-port "$port" --transport http \
            --revisions-mode single --min-replicas 1 --max-replicas 1 \
            --cpu 0.5 --memory 1Gi --env-vars "${settings[@]}" --output none
    else
        existing_environment="$(azure containerapp show --name "$name" \
            --resource-group "$RESOURCE_GROUP" --query properties.managedEnvironmentId --output tsv)"
        [[ "${existing_environment,,}" == "${environment_id,,}" ]] \
            || die "$name belongs to another Container Apps environment."
        azure containerapp identity assign --name "$name" \
            --resource-group "$RESOURCE_GROUP" --system-assigned --output none
        azure containerapp ingress enable --name "$name" --resource-group "$RESOURCE_GROUP" \
            --type external --allow-insecure false --target-port "$port" --transport http \
            --output none
        azure containerapp revision set-mode --name "$name" \
            --resource-group "$RESOURCE_GROUP" --mode single --output none
        azure containerapp update --name "$name" --resource-group "$RESOURCE_GROUP" \
            --image "$image" --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1Gi \
            --set-env-vars "${settings[@]}" --output none
    fi

    principal_id="$(azure containerapp show --name "$name" --resource-group "$RESOURCE_GROUP" \
        --query identity.principalId --output tsv)"
    [[ -n "$principal_id" ]] || die "System-assigned identity missing for $name."
    assignment_id="$(foundry role assignment list --scope "$FOUNDRY_RESOURCE_ID" \
        --query "[?principalId=='$principal_id' && ends_with(roleDefinitionId, '/$FOUNDRY_USER_ROLE_ID')].id | [0]" \
        --output tsv)"
    if [[ -z "$assignment_id" ]]; then
        foundry role assignment create --assignee-object-id "$principal_id" \
            --assignee-principal-type ServicePrincipal --role "$FOUNDRY_USER_ROLE_ID" \
            --scope "$FOUNDRY_RESOURCE_ID" --output none
    fi

    fqdn="$(azure containerapp show --name "$name" --resource-group "$RESOURCE_GROUP" \
        --query properties.configuration.ingress.fqdn --output tsv)"
    [[ -n "$fqdn" ]] || die "Ingress hostname missing for $name."
    url="https://$fqdn"
    azure containerapp update --name "$name" --resource-group "$RESOURCE_GROUP" \
        --set-env-vars "${prefix}_PUBLIC_URL=$url" --output none
    curl --fail --silent --show-error --retry 12 --retry-all-errors --retry-delay 5 \
        --retry-max-time 180 --max-time 15 "$url/healthz"
    printf '\n%s: %s (identity: %s)\n' "$name" "$url" "$principal_id"
}

printf 'WARNING: Both agents will be publicly reachable without client authentication.\n' >&2
deploy_agent "$RISK_APP_NAME" RISK 8001 "$RISK_IMAGE" "$RISK_MODEL_DEPLOYMENT"
deploy_agent "$CUSTOMER_APP_NAME" CUSTOMER 8002 "$CUSTOMER_IMAGE" "$CUSTOMER_MODEL_DEPLOYMENT"
printf 'Health checks passed. They do not verify Foundry inference or RBAC propagation.\n'
