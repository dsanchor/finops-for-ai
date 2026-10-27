# FinOps tools: reconcile and allocate AI cost with FOCUS

These scripts split the **real** AI bill across the consumers seen by the AI gateway
(agent, department, team, user, product and subscription). The output uses the
[FOCUS](https://focus.finops.org/) format.

```text
AI gateway ──► Application Insights ──► gateway_usage.py ──► gateway usage (who · tokens) ─┐
(llm-emit-token-metric)                 daily summary,                                     ├─► reconcile_allocate.py ─► allocated FOCUS
                                        users hashed                                       │                            + reconciliation summary
Cost Management · FOCUS cost export ─────────────────────► FOCUS bill (what · cost) ───────┘
```

| Script | Input | Output |
| --- | --- | --- |
| [gateway_usage.py](gateway_usage.py) | Application Insights `customMetrics` | Daily gateway usage (CSV/Parquet) |
| [reconcile_allocate.py](reconcile_allocate.py) | FOCUS bill + gateway usage + meter map | `allocated_focus.csv` + `reconciliation_summary.csv` |

## Setup

The scripts use the optional `finops` dependency group, which isn't installed in the agent
images:

```bash
uv sync --group finops      # or: uv sync --group dev, which includes it
az login                    # gateway_usage.py uses DefaultAzureCredential
```

The identity needs read access to the Application Insights logs, for example **Monitoring
Reader** or **Log Analytics Reader** on the resource.

## 1. Gateway usage: daily summary

```bash
export GATEWAY_USAGE_HASH_KEY="<secret, stable over time>"
uv run python -m tools.gateway_usage \
  --app-insights-resource-id "/subscriptions/<subscription-id>/resourceGroups/<rg>/providers/Microsoft.Insights/components/<app-insights>" \
  --start 2026-10-01 --end 2026-11-01 \
  --out data/gateway_usage.csv
```

The script queries one UTC day at a time and writes one row per day and dimension
combination. `--end` is exclusive.

| Column | Content |
| --- | --- |
| `date` | UTC day (`YYYY-MM-DD`) |
| `service`, `region` | APIM instance and region |
| `model` | Deployment name from the request body (`gpt-5.4-mini`, ...) |
| `agent`, `product`, `subscription_id` | Agent (`X-Agent`), APIM product and subscription |
| `department`, `team`, `user` | Identity dimensions; `user` is hashed |
| `requests` | Number of model calls |
| `prompt_tokens`, `cached_prompt_tokens`, `cache_creation_tokens`, `completion_tokens`, `reasoning_tokens`, `total_tokens` | Token metrics |

- **Users are pseudonymised** with HMAC-SHA256 (`u_<16 hex>`). Keep
  `GATEWAY_USAGE_HASH_KEY` secret and stable, so the same user keeps the same hash. Use
  `--keep-user-ids` only if you explicitly want the identities in clear text.
- Missing dimensions are written as `N/A`. Tokens with `model = N/A` can't be matched to a
  bill meter and will remain unallocated.
- In production, a Log Analytics **summary rule** plus a **data export rule** can produce
  the same daily table without running this script.

## 2. FOCUS bill

In **Cost Management > Exports**, create an export of type **Cost and usage details
(FOCUS)** with daily granularity. Download the CSV or Parquet files, or read them from the
storage account. `--bill` accepts files or folders and can be repeated.

## 3. Meter map

The bill only knows meters, such as `5.4 mini Inp Gl 1M Tokens`. The gateway only knows
deployments and token types. Generate a template for the AI accounts behind the gateway,
then review it:

```bash
uv run python -m tools.reconcile_allocate \
  --bill <export-folder> --usage data/gateway_usage.csv \
  --resource-group <resource-group> \
  --write-meter-map data/meter_map.csv
```

The script guesses `model` (from the deployments in the usage file) and `token_type`.
Fix or complete them and leave the row empty for meters that don't belong to the gateway.

| `token_type` | Gateway quantity |
| --- | --- |
| `input` | `prompt_tokens - cached_prompt_tokens` |
| `cached_input` | `cached_prompt_tokens` |
| `output` | `completion_tokens` (includes reasoning tokens) |
| `cache_write` | `cache_creation_tokens` (removed from `input` for that model) |

Example for the demo account:

```csv
meter,model,token_type
5.4 mini Inp Gl 1M Tokens,gpt-5.4-mini,input
5.4 mini Opt Gl 1M Tokens,gpt-5.4-mini,output
5.6 sol ShortCo Inp Std Gl 1M Tokens,gpt-5.6-sol,input
5.6 sol ShortCo Cd Wr Std Gl 1M Tokens,gpt-5.6-sol,input
5.6 sol ShortCo Cd Inp Std Gl 1M Tokens,gpt-5.6-sol,cached_input
5.6 sol ShortCo Opt Std Gl 1M Tokens,gpt-5.6-sol,output
```

> **Cache write meters (`Cd Wr`).** For these models, APIM reports `Cache Creation Tokens =
> 0` and counts those tokens in `Prompt Tokens`. Map them to `input`: the input charge and
> the cache-write charge are then split together using the gateway's uncached prompt
> tokens. Use `cache_write` only if the gateway reports cache creation tokens for that
> model.

The meter column is `SkuMeter`, or `x_SkuMeterName` when the export doesn't fill it. Use
`--meter-column` to choose another one.

## 4. Reconcile and allocate

```bash
uv run python -m tools.reconcile_allocate \
  --bill <export-folder> --usage data/gateway_usage.csv \
  --resource-group <resource-group> \
  --meter-map data/meter_map.csv --out-dir data/out
```

The gateway usage doesn't say which account served each call, so you must tell the script
which AI accounts sit behind the gateway. Use either option, or both; each can be repeated:

| Option | Scope |
| --- | --- |
| `--resource-group <rg>` | Every `Microsoft.CognitiveServices` account in that resource group. Other resources in the group (APIM, Container Apps, storage...) are ignored |
| `--resource <name-or-arm-id>` | One specific AI account |

Only the charges in scope are reconciled. If the same model is billed in accounts outside
the scope, those charges are excluded. Use `--resource` when a resource group contains AI
accounts that aren't behind the gateway.

### How the cost is split

For each bill charge of a mapped meter, the script takes the gateway tokens of the same
**model, token type and charge period**:

```text
consumer cost = charge cost × consumer tokens / max(billed tokens, gateway tokens)
unallocated   = charge cost × (1 − gateway tokens / max(billed tokens, gateway tokens))
```

- All cost columns (`BilledCost`, `EffectiveCost`, `ListCost`, `ContractedCost` and the
  `x_*Cost*` variants) are scaled by the same ratio, as are `ConsumedQuantity` and
  `PricingQuantity`. Unit prices stay unchanged.
- The real price is always used: discounts, commitments (`EffectiveCost`) and
  meter-specific prices such as cache write come from the bill.
- If the gateway sees more tokens than the bill (timing differences), the whole charge is
  allocated proportionally and nothing becomes negative.
- Meters of the in-scope accounts that aren't in the map stay unallocated, with the
  reason recorded.
- The script checks that the output adds up to the bill in scope, for every cost column.

### Outputs

**`allocated_focus.csv`** (or `.parquet` with `--format parquet`): every original FOCUS
column, plus:

| Column | Content |
| --- | --- |
| `AllocatedMethodId` | `ai-gateway-token-share-v1` |
| `AllocatedMethodDetails` | JSON with `AllocatedRatio`, `UsageUnit = Tokens` and `UsageQuantity` |
| `AllocatedResourceId`, `AllocatedResourceName` | Agent, or `unallocated` |
| `AllocatedTags` | JSON with agent, department, team, user, product, subscription |
| `x_AllocationStatus` | `Allocated` or `Unallocated` |
| `x_UnallocatedReason` | Not seen by the gateway, or meter not mapped |
| `x_AllocatedRatio`, `x_GatewayModel`, `x_GatewayTokenType` | Allocation detail |
| `x_AllocatedAgent`, `x_AllocatedDepartment`, `x_AllocatedTeam`, `x_AllocatedUser`, `x_AllocatedProduct`, `x_AllocatedSubscriptionId` | Flat columns for BI tools |

**`reconciliation_summary.csv`**: one row per charge period, model and token type, with
billed tokens, gateway tokens, `coverage` (gateway ÷ billed), billed cost and allocated
cost. Low coverage points to traffic that bypassed the gateway, tokens without a model, or
a wrong meter mapping.

The `Allocated*` columns follow the split cost allocation columns introduced in FOCUS 1.3.

## Privacy

- The usage and allocated files contain hashed users and APIM subscription IDs.
- Store them in a protected location, such as the FinOps data lake.
- Don't commit them to the repository.
