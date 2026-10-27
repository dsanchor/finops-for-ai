# FinOps and FOCUS: from gateway telemetry to cost allocation

This document extends [README.md](README.md). The demo measures **tokens**; this
document explains how to turn them into **cost**: how to estimate it, how to take
the data out of Application Insights, and how to split the **real bill** across
the gateway's consumers in **FOCUS** (FinOps Open Cost and Usage Specification)
format, so the FinOps team can use it next to the rest of the cloud bill.

The step-by-step instructions for the tools are in
[tools/README.md](../tools/README.md). All values are for the demo environment.
Prices are never included in this repository.

## Contents

1. [Three levels of cost](#1-three-levels-of-cost)
2. [Price table](#2-price-table)
3. [Cost panels for Grafana](#3-cost-panels-for-grafana)
4. [Attribution coverage](#4-attribution-coverage)
5. [Exporting the data out of Application Insights](#5-exporting-the-data-out-of-application-insights)
6. [The allocated FOCUS dataset](#6-the-allocated-focus-dataset)
7. [Reconciling with the Cost Management FOCUS export](#7-reconciling-with-the-cost-management-focus-export)
8. [The tools](#8-the-tools)
9. [Chargeback models](#9-chargeback-models)
10. [Limits and recommendations](#10-limits-and-recommendations)

## 1. Three levels of cost

Token metrics are not a bill, and the bill has no owner. The provider invoice
shows one shared Foundry account; the gateway knows who consumed each token.
FinOps needs both.

| Level | How it's calculated | Where | Use |
| --- | --- | --- | --- |
| Estimated, list | Gateway tokens × retail price | Grafana recipes (section 3) | What-if, model comparison |
| Estimated, contracted | Gateway tokens × negotiated price | Grafana recipes (section 3) | Near-real-time showback |
| Reconciled | Real charge from the Cost Management FOCUS export, split by gateway token share | [reconcile_allocate.py](../tools/reconcile_allocate.py) (sections 6–8) | Monthly chargeback |

The reconciled level doesn't need a price table: it uses the bill's own costs,
including discounts, commitments and meter-specific prices.

## 2. Price table

Only the estimated levels need prices, per **model deployment** and **token type**:

| `token_type` | Tokens | Notes |
| --- | --- | --- |
| `input` | `Prompt Tokens` − `Prompt Cached Tokens` | Uncached input |
| `cached_input` | `Prompt Cached Tokens` | Usually billed at a lower rate |
| `output` | `Completion Tokens` | Already includes `Completion Reasoning Tokens` |

Some models also bill **cache writes** (meters such as `… Cd Wr …`) at a different
price. APIM reports `Cache Creation Tokens = 0` for them and counts those tokens
in `Prompt Tokens`, so an estimate with the input price undercounts them. The
reconciled level gets them right.

Sources:

- **List prices:** the [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
  (`https://prices.azure.com/api/retail/prices`). It's unauthenticated and returns
  retail prices without negotiated discounts.
- **Contracted prices:** the price sheet in Cost Management for your billing
  account (Enterprise Agreement or Microsoft Customer Agreement).
- **The FOCUS export itself:** `ListUnitPrice` and `ContractedUnitPrice` per meter.
- **Provisioned throughput (PTU):** there's no per-token price. The cost is the
  reservation, so use the reconciled level or a blended internal rate (section 9).

Don't infer the model or its price from the deployment name: a deployment name
is a label, not a model version.

Where to keep it:

- Demo: a `datatable` inside the Grafana query (section 3).
- Production: a versioned CSV in storage with effective dates, loaded with
  `externaldata`, or a custom Log Analytics table.

## 3. Cost panels for Grafana

These panels are **recipes for the estimated level**: they aren't part of the
dashboard in this repository. They need a price table (section 2) and give
near-real-time showback; the reconciled cost comes from the tools in section 8.

The queries use the Application Insights schema the dashboard already uses
(`customMetrics`, `valueSum`, `valueCount`, `customDimensions`). Add your dashboard
variable filters (agent, product, department, team, user, model) to the `where`
clauses.

### Base block

Paste this at the top of each cost panel:

```kusto
// Contracted price in EUR per 1M tokens. Fill from your price sheet.
let prices = datatable(model:string, inPer1M:real, cachedPer1M:real, outPer1M:real)
[
    "gpt-5.6-sol",  0.0, 0.0, 0.0,
    "gpt-5.4-mini", 0.0, 0.0, 0.0
];
let usage = (from:datetime, to:datetime, grain:timespan) {
    customMetrics
    | where timestamp between (from .. to)
    | where name in ("Prompt Tokens", "Prompt Cached Tokens", "Completion Tokens", "Total Tokens")
    | extend model      = tostring(customDimensions.model),
             agent      = tostring(customDimensions.agent),
             product    = tostring(customDimensions.product),
             department = tostring(customDimensions.department),
             team       = tostring(customDimensions.team)
    | summarize Prompt     = sumif(valueSum, name == "Prompt Tokens"),
                Cached     = sumif(valueSum, name == "Prompt Cached Tokens"),
                Completion = sumif(valueSum, name == "Completion Tokens"),
                Requests   = sumif(valueCount, name == "Total Tokens")
        by timestamp = bin(timestamp, grain), model, agent, product, department, team
    | lookup kind=leftouter prices on model
    | extend Priced     = isnotnull(inPer1M),
             InputCost  = (Prompt - Cached) * inPer1M / 1e6,
             CachedCost = Cached * cachedPer1M / 1e6,
             OutputCost = Completion * outPer1M / 1e6
    | extend Cost = InputCost + CachedCost + OutputCost
};
```

- `valueCount` of `Total Tokens` is the number of model calls in each
  pre-aggregated interval.
- Don't add `Total Tokens` to the cost: it's prompt plus completion.

### Estimated cost over time, by department (time series, stacked)

```kusto
usage($__timeFrom(), $__timeTo(), $__interval)
| summarize Cost = sum(Cost) by timestamp, department
| order by timestamp asc
```

### Showback and unit economics (table)

```kusto
let u = usage($__timeFrom(), $__timeTo(), 1d);
let total = toscalar(u | summarize sum(Cost));
u
| summarize Cost = sum(Cost), Requests = sum(Requests), Prompt = sum(Prompt),
            Cached = sum(Cached), Completion = sum(Completion)
    by department, team, agent, model
| extend CostPerRequest   = iff(Requests > 0, Cost / Requests, real(null)),
         TokensPerRequest = iff(Requests > 0, (Prompt + Completion) / Requests, real(null)),
         OutputInputRatio = iff(Prompt > 0, round(Completion / Prompt, 2), real(null)),
         CacheSharePct    = iff(Prompt > 0, round(100.0 * Cached / Prompt, 1), real(null)),
         PctOfTotal       = round(100.0 * Cost / total, 1)
| order by Cost desc
```

### Savings from prompt caching (stat)

```kusto
usage($__timeFrom(), $__timeTo(), 1d)
| summarize Savings = sum(Cached * (inPer1M - cachedPer1M) / 1e6)
```

### Budget burn and month-end forecast (bar gauge)

This panel ignores the time picker and always shows the current month (UTC).

```kusto
let budgets = datatable(department:string, monthlyBudget:real) [ "cib", 0.0, "retail", 0.0 ];
let monthStart = startofmonth(now());
let daysInMonth = toreal(datetime_diff('day', endofmonth(now()), monthStart) + 1);
let elapsedDays = (now() - monthStart) / 1d;
usage(monthStart, now(), 1d)
| summarize MonthToDate = sum(Cost) by department
| lookup kind=leftouter budgets on department
| extend Forecast = MonthToDate / elapsedDays * daysInMonth,
         BurnPct = round(100.0 * MonthToDate / monthlyBudget, 1),
         ForecastPct = round(100.0 * Forecast / monthlyBudget, 1)
```

### Unpriced tokens (data quality)

```kusto
usage($__timeFrom(), $__timeTo(), 1d)
| where not(Priced)
| summarize Tokens = sum(Prompt + Completion) by model
```

## 4. Attribution coverage

Coverage tells FinOps how much of the consumption the gateway can attribute.
Low coverage means unallocated cost.

### Gateway versus Foundry (bypass detection)

In Grafana, build one panel with two queries and an expression:

1. **A**, Azure Monitor **Metrics** on the Foundry account: `ProcessedPromptTokens`
   and `GeneratedTokens`, aggregation Total, split by `ModelDeploymentName`.
2. **B**, Logs on Application Insights: `Prompt Tokens` + `Completion Tokens` from
   `customMetrics`, by `model`.
3. Expression: `$B / $A`.

Values well below 100% mean traffic reaches Foundry without the gateway (for
example, through the public Container Apps ingress). Compare trends rather than
exact values: the two sources may count edge cases differently.

### Metrics versus requests (dropped metrics)

Custom metrics beyond Azure Monitor limits are discarded silently (section 10).
Compare metered calls with successful model requests:

```kusto
let calls = toscalar(
    requests
    | where $__timeFilter(timestamp)
    | where tostring(customDimensions["API Name"]) == "finopsforai"
    | where tostring(customDimensions["HTTP Method"]) == "POST" and resultCode == "200"
    | summarize sum(itemCount));
customMetrics
| where $__timeFilter(timestamp) and name == "Total Tokens"
| summarize Metered = sum(valueCount)
| extend ModelCalls = calls, CoveragePct = round(100.0 * Metered / calls, 1)
```

### Bill versus gateway (reconciliation coverage)

After each reconciliation, `reconciliation_summary.csv` has a `coverage` column
per day, model and token type: gateway tokens ÷ billed tokens. Track it as the
main FinOps KPI of the gateway. Typical causes of low coverage:

- Traffic that reached Foundry without the gateway (public Container Apps ingress).
- Tokens emitted without a `model` dimension (for example, before the model API
  policy extracted `requestedModel`). They can't be matched to a meter.
- A meter mapped to the wrong model or token type.

## 5. Exporting the data out of Application Insights

Application Insights keeps data for the retention configured in its Log
Analytics workspace. A monthly chargeback and a history of FinOps data need a
copy outside it: the **gateway usage** dataset (who · tokens).

Naming: the Application Insights query surface and the workspace use different
names for the same data.

| Application Insights (`customMetrics`) | Workspace table (`AppMetrics`) |
| --- | --- |
| `timestamp` | `TimeGenerated` |
| `name` | `Name` |
| `valueSum` | `Sum` |
| `valueCount` | `ItemCount` |
| `customDimensions` | `Properties` |

### Gateway usage schema

Whichever option you use, the reconciliation expects one row per UTC day and
dimension combination:

| Column | Content |
| --- | --- |
| `date` | UTC day (`YYYY-MM-DD`) |
| `service`, `region` | APIM instance and region |
| `model`, `agent`, `product`, `subscription_id`, `department`, `team`, `user` | Dimensions; `user` pseudonymised |
| `requests` | Model calls (`valueCount` of `Total Tokens`) |
| `prompt_tokens`, `cached_prompt_tokens`, `cache_creation_tokens`, `completion_tokens`, `reasoning_tokens`, `total_tokens` | Token metrics |

Missing dimensions are `N/A`. It's **tokens, not cost**: prices change, so cost is
applied downstream.

### Options

| Option | How | Strengths | Watch out for | Use for |
| --- | --- | --- | --- | --- |
| **A. Summary rule + data export rule** | A daily KQL aggregation writes `AiGatewayUsageDaily_CL`; a data export rule streams that table to Storage | No code. No query cost on Analytics tables; you pay ingestion of the small summary | Only processes recent data (no backfill). Export is JSON lines in 5-minute folders, with possible duplicates after retries | Production baseline |
| **B. Scheduled job** | [gateway_usage.py](../tools/gateway_usage.py), a Fabric notebook, a Function or a Logic App runs KQL through the Logs Query API | Writes the schema directly, with keyed user hashes. Can backfill within retention | Subject to query limits. You operate the job | Demo, pilot, backfill |
| **C. Export job (preview)** | On-demand export of a table's historical records to Blob Storage, in Parquet | Backfill at scale | Preview. Raw records, not aggregated | First load |
| **D. Grafana CSV** | Panel > Inspect > Data > Download CSV | Instant | Manual | Demo only |

### Option A: summary rule

Query, in workspace schema. It produces the gateway usage schema; `date` comes
from `_BinStartTime`. Don't add a time filter: the bin defines the window.

```kusto
AppMetrics
| where _ResourceId =~ "/subscriptions/<subscription-id>/resourcegroups/<resource-group>/providers/microsoft.insights/components/<app-insights-name>"
| where Name in ("Prompt Tokens", "Prompt Cached Tokens", "Cache Creation Tokens",
                 "Completion Tokens", "Completion Reasoning Tokens", "Total Tokens")
| extend service         = tostring(Properties["Service Name"]),
         region          = tostring(Properties.Region),
         model           = tostring(Properties.model),
         agent           = tostring(Properties.agent),
         product         = tostring(Properties.product),
         subscription_id = tostring(Properties.subscriptionId),
         department      = tostring(Properties.department),
         team            = tostring(Properties.team),
         user            = strcat("u_", substring(hash_sha256(tolower(tostring(Properties.user))), 0, 16))
| summarize requests              = sumif(ItemCount, Name == "Total Tokens"),
            prompt_tokens         = sumif(Sum, Name == "Prompt Tokens"),
            cached_prompt_tokens  = sumif(Sum, Name == "Prompt Cached Tokens"),
            cache_creation_tokens = sumif(Sum, Name == "Cache Creation Tokens"),
            completion_tokens     = sumif(Sum, Name == "Completion Tokens"),
            reasoning_tokens      = sumif(Sum, Name == "Completion Reasoning Tokens"),
            total_tokens          = sumif(Sum, Name == "Total Tokens")
    by service, region, model, agent, product, subscription_id, department, team, user
```

- Users are hashed before leaving the workspace. This hash isn't keyed, so
  anyone can hash a known email and compare. `gateway_usage.py` uses HMAC with a
  secret key instead. Use one method consistently: hashes from the two options
  don't match.
- When loading the export, rename `_BinStartTime` to `date` (UTC day) and
  deduplicate by `date` and the dimension columns.

`summary-rule.json`:

```json
{
  "properties": {
    "ruleType": "User",
    "description": "Daily AI gateway token usage for FinOps",
    "ruleDefinition": {
      "query": "<the query above>",
      "binSize": 1440,
      "binStartTime": "<YYYY-MM-DD>T00:00Z",
      "destinationTable": "AiGatewayUsageDaily_CL"
    }
  }
}
```

```bash
WS_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.OperationalInsights/workspaces/$WS"
az rest --method put \
  --url "https://management.azure.com$WS_ID/summaryLogs/ai-gateway-usage-daily?api-version=<api-version>" \
  --body @summary-rule.json
```

`binStartTime` must be a whole hour, no earlier than the creation time minus the
bin size. Use 00:00 UTC to align with FOCUS days and with daily quotas.

### Option A: data export rule

```bash
az monitor log-analytics workspace data-export create \
  --resource-group "$RG" --workspace-name "$WS" \
  --name ai-gateway-finops \
  --tables AiGatewayUsageDaily_CL \
  --destination "$STORAGE_ACCOUNT_ID"
```

The storage account must be in the workspace region and can't be Premium. Each
table gets a container named `am-` plus the table name.

## 6. The allocated FOCUS dataset

The gateway usage is **not** FOCUS: it's an allocation key (who · tokens). The
FOCUS dataset is the **provider's bill**, from the Cost Management FOCUS export,
with each AI charge split across the gateway's consumers.

- Every original FOCUS column is kept: billing account, periods, meter, prices,
  `ServiceName`, `ResourceId`, and so on.
- Each AI charge becomes one row per consumer, plus an unallocated row when the
  gateway didn't see all the consumption.
- All cost columns (`BilledCost`, `EffectiveCost`, `ListCost`, `ContractedCost`
  and the `x_*Cost*` variants) and the quantities (`ConsumedQuantity`,
  `PricingQuantity`) are multiplied by the allocation ratio. Unit prices don't
  change.
- The rows of each charge add up exactly to the original charge.

Columns added:

| Column | Content | Notes |
| --- | --- | --- |
| `AllocatedMethodId` | `ai-gateway-token-share-v1` | FOCUS 1.3 split cost allocation |
| `AllocatedMethodDetails` | `{"Elements":[{"AllocatedRatio":…,"UsageUnit":"Tokens","UsageQuantity":…}]}` | Ratio applied and tokens of the consumer |
| `AllocatedResourceId` / `AllocatedResourceName` | `agent`, or `unallocated` | Who consumed the shared resource |
| `AllocatedTags` | `{"agent":…,"department":…,"team":…,"user":…,"product":…,"subscription_id":…}` | Empty for unallocated rows |
| `x_AllocationStatus` | `Allocated` or `Unallocated` | |
| `x_UnallocatedReason` | `Consumption not seen by the AI gateway` or `Meter not mapped to a gateway model` | |
| `x_AllocatedRatio` | Share of the original charge | |
| `x_GatewayModel`, `x_GatewayTokenType` | Model and token type used for matching | |
| `x_AllocatedAgent`, `x_AllocatedDepartment`, `x_AllocatedTeam`, `x_AllocatedUser`, `x_AllocatedProduct`, `x_AllocatedSubscriptionId` | Flat copies of the tags, for BI tools | FOCUS reserves the `x_` prefix for custom columns |

Use it as the FOCUS dataset for showback and chargeback: filter
`x_AllocationStatus = Allocated`, group by any `x_Allocated*` column and sum
`EffectiveCost` (amortized, includes discounts) or `BilledCost` (invoiced).

## 7. Reconciling with the Cost Management FOCUS export

1. Create a Cost Management export of type **Cost and usage details (FOCUS)**,
   daily, to Storage, for the scope that contains the Foundry account. Choose
   the newest FOCUS version available: version 1.0 lacks `InvoiceId` and
   `ServiceSubcategory`, which FOCUS 1.2 validation reports as missing (section 8).
   If the FinOps team runs
   [FinOps hubs](https://learn.microsoft.com/cloud-computing/finops/toolkit/hubs/finops-hubs-overview),
   reuse its data.
2. **Scope:** keep only the AI accounts behind the gateway, by account
   (`--resource`) or by resource group (`--resource-group`, which takes only its
   `Microsoft.CognitiveServices` accounts). The gateway usage doesn't say which
   account served a call, so the same model billed in other accounts stays out.
3. **Meter map:** map each meter to a model deployment and token type. The meter
   is in `SkuMeter`, or in `x_SkuMeterName` when the export doesn't fill it. The
   tool generates a template with guesses; copy the exact strings from your
   export: they're not listed here on purpose.

   | `token_type` | Gateway tokens matched |
   | --- | --- |
   | `input` | `prompt_tokens − cached_prompt_tokens` |
   | `cached_input` | `cached_prompt_tokens` |
   | `output` | `completion_tokens` (includes reasoning) |
   | `cache_write` | `cache_creation_tokens`, removed from `input` for that model |

   Map **cache write** meters to `input` unless the gateway reports
   `Cache Creation Tokens` for that model: the input and cache-write charges are
   then split together using the uncached prompt tokens.
4. For each charge period, model and token type (the **origin charge**), split
   the cost across gateway consumers by token share:
   `AllocatedRatio = consumer tokens ÷ max(billed tokens, gateway tokens)`.
5. Add one **unallocated** row with the remainder: consumption that the gateway
   didn't see. Meters of the in-scope accounts that aren't mapped are also
   unallocated, with their reason. The platform team owns this cost, and its
   share is a KPI.

The allocated rows plus the unallocated row add up exactly to the origin
charge, as FOCUS 1.3 split cost allocation requires. If the gateway counts more
tokens than the bill (timing), the whole charge is allocated proportionally and
nothing becomes negative.

Illustrative example for one day and one meter:

| Charge | Tokens | `AllocatedRatio` | `EffectiveCost` |
| --- | ---: | ---: | ---: |
| Origin: model input tokens | 10,000,000 | 1.00 | €100.00 |
| `risk` · CIB / Risk | 6,200,000 | 0.62 | €62.00 |
| `customer-support` · Retail / Support | 3,100,000 | 0.31 | €31.00 |
| Unallocated | 700,000 | 0.07 | €7.00 |

Provisioned throughput uses the same method with the PTU charge as the origin.
Check how those charges appear in your export first; unused capacity stays in
the unallocated row or becomes a platform cost.

Cost data for a day can arrive or change later (24–48 hours). Re-run the
reconciliation for the open month every day, using the same date range for the
gateway usage and the bill, and close the month after the invoice.

## 8. The tools

Two scripts in [tools/](../tools/) implement sections 5 (option B), 6 and 7.
Full parameters and outputs: [tools/README.md](../tools/README.md).

| Script | Input | Output |
| --- | --- | --- |
| [gateway_usage.py](../tools/gateway_usage.py) | Application Insights `customMetrics` | Gateway usage (section 5 schema) |
| [reconcile_allocate.py](../tools/reconcile_allocate.py) | FOCUS export + gateway usage + meter map | `allocated_focus.csv` (section 6) and `reconciliation_summary.csv` (section 4) |

```bash
uv sync --group finops
az login
export GATEWAY_USAGE_HASH_KEY="<secret, stable over time>"

# 1. Gateway usage (UTC days, end exclusive)
uv run python -m tools.gateway_usage \
  --app-insights-resource-id "<app-insights-arm-id>" \
  --start 2026-10-01 --end 2026-11-01 --out data/gateway_usage.csv

# 2. Meter map template (once, and when models change): review it
uv run python -m tools.reconcile_allocate \
  --bill data/bill --usage data/gateway_usage.csv \
  --resource-group <resource-group> --write-meter-map data/meter_map.csv

# 3. Reconcile and allocate
uv run python -m tools.reconcile_allocate \
  --bill data/bill --usage data/gateway_usage.csv \
  --resource-group <resource-group> --meter-map data/meter_map.csv --out-dir data/out
```

- `gateway_usage.py` uses `DefaultAzureCredential`. The identity needs read
  access to the Application Insights logs (for example, Monitoring Reader or Log
  Analytics Reader). Users are hashed with HMAC-SHA256 and the secret key.
- `--bill` accepts files or folders, CSV or Parquet, and can be repeated.
- `reconcile_allocate.py` checks that every cost column of the output adds up
  to the bill in scope, and prints the allocated and unallocated totals.
- `data/` is ignored by Git: the files contain hashed users and subscription IDs.
- Schedule steps 1 and 3 daily after the bill is updated, for example as a
  Container Apps job, a Fabric notebook or a pipeline.

### Validating the FOCUS output

The FinOps Foundation [focus-validator](https://github.com/finopsfoundation/focus_validator)
checks a dataset against the specification. Version 2.2.1 needs two workarounds:
it pins NumPy 1.26 (so use Python 3.12 and `pyarrow<20`), and it opens its
currency list with a path relative to the working directory.

```bash
VALIDATOR='uvx --python 3.12 --with pyarrow<20 --from focus-validator==2.2.1'
PKG=$($VALIDATOR python -c 'import focus_validator, os; print(os.path.dirname(focus_validator.__file__))')
mkdir -p /tmp/focus-run/focus_validator/rules
cp "$PKG/rules/currency_codes.csv" /tmp/focus-run/focus_validator/rules/

OUT="$PWD/data/out"
(cd /tmp/focus-run && $VALIDATOR focus-validator \
  --data-file "$OUT/allocated_focus.csv" --validate-version 1.2 \
  --output-type web --output-destination "$OUT/focus-validation.html")
```

Validate the original export the same way and compare. Failures in both come
from the provider's export. With a FOCUS 1.0 export, the only failures are the
missing `InvoiceId` and `ServiceSubcategory` columns. Use `--validate-version
1.3.0.1` to also check the `Allocated*` columns.

## 9. Chargeback models

| Model | How it works | Good for |
| --- | --- | --- |
| Per token, contracted price | Each area pays its input, cached and output tokens at the negotiated price per model | Pay-as-you-go |
| Blended internal rate | Total real cost (PTU + pay-as-you-go) ÷ total tokens = one stable rate per model | Predictable budgets |
| Reserved capacity + overflow | Fixed share of PTU for areas that need guaranteed throughput, plus pay-as-you-go tokens beyond it | Critical workloads |

In all models:

- **Platform cost** (API Management, monitoring, dashboards) is split by requests
  or charged as a fixed fee per onboarded agent.
- **Unallocated cost** is owned by the platform team and tracked as a KPI.
- Start with **showback** for two or three months, then move to chargeback.
- The `free`, `standard` and `premium` products can map to internal price plans.

## 10. Limits and recommendations

- **Custom metric limits.** The `llm-emit-token-metric` documentation states up
  to 5 custom dimensions per policy, 100 unique values per dimension and 1,000
  active time series per namespace. Data beyond the value and series limits is
  discarded silently. The model API policy emits 7 dimensions (`model`,
  `subscriptionId`, `product`, `agent`, `user`, `team`, `department`), and all 7
  currently arrive in `customMetrics`. Don't rely on this beyond the documented
  limit: validate it with the coverage checks in section 4. At scale, keep
  low-cardinality dimensions in metrics (`model`, `agent`, `department`, `team`,
  `product`) and move `user` and `subscriptionId` to LLM logs or the export path.
- **Personal data.** `user` holds email addresses. Hash it in every export,
  keep the hash key secret, and restrict the user panels in Grafana.
- **Missing usage.** Token counts come from the model response. Broken streams
  (`ClientConnectionFailure`) can lose them: watch the coverage panels.
- **Tokens without a model.** Usage emitted without the `model` dimension can't
  be matched to a meter and ends up unallocated.
- **UTC everywhere.** Daily quotas, summary rule bins, gateway usage days and
  FOCUS dates all use UTC.
- **Bypass.** While the Container Apps ingress is public, calls that skip the
  gateway show up only as unallocated cost.
- **A metric is not a bill.** Use the estimated cost for daily decisions and the
  reconciled cost for chargeback.
