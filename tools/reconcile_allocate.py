"""Reconcile a FOCUS bill with AI gateway usage and allocate the real cost to consumers.

Inputs:
  * FOCUS cost export from Cost Management (CSV or Parquet, one or more files or folders).
  * Daily gateway usage produced by ``tools.gateway_usage``.
  * Meter map: which bill meter corresponds to which gateway model and token type.

For every bill charge of a mapped meter, the gateway tokens of the same model, token type
and charge period are used as allocation keys. Each consumer (agent, department, team,
user, product, subscription) receives ``cost x consumer_tokens / max(billed, gateway)``.
Consumption the gateway did not see stays in an "Unallocated" row, so the allocated output
always adds up to the original bill. The FOCUS 1.3 split cost allocation columns
(``Allocated*``) describe each split.

Usage:
  # 1. Create a meter map template from the bill and review it
  python -m tools.reconcile_allocate --bill <export> --usage gateway_usage.csv \\
      --write-meter-map meter_map.csv

  # 2. Reconcile and allocate
  python -m tools.reconcile_allocate --bill <export> --usage gateway_usage.csv \\
      --meter-map meter_map.csv --out-dir out/
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import pandas as pd

METHOD_ID = "ai-gateway-token-share-v1"
TOKEN_TYPES = ("input", "cached_input", "cache_write", "output")
DEFAULT_DIMENSIONS = ("agent", "department", "team", "user", "product", "subscription_id")
QUANTITY_COLUMNS = ("ConsumedQuantity", "PricingQuantity")
GROUP_KEYS = ["_start", "_end", "model", "token_type"]
MISSING = "N/A"
TOLERANCE = 1e-6


# --------------------------------------------------------------------------- input
def read_table(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path, low_memory=False)


def read_usage(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path, keep_default_na=False, na_values=[""])


def read_bill(paths: list[str]) -> pd.DataFrame:
    files: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for pattern in ("*.csv", "*.parquet", "*.csv.gz"):
                files.extend(sorted(glob.glob(os.path.join(path, "**", pattern), recursive=True)))
        else:
            files.append(path)
    if not files:
        raise ValueError("no bill files found")
    return pd.concat([read_table(file) for file in files], ignore_index=True)


def meter_column(bill: pd.DataFrame, requested: str | None) -> str:
    if requested:
        if requested not in bill:
            raise ValueError(f"meter column {requested!r} is not in the bill")
        return requested
    for candidate in ("SkuMeter", "x_SkuMeterName"):
        if candidate in bill and bill[candidate].notna().any():
            return candidate
    raise ValueError("the bill has no SkuMeter or x_SkuMeterName column; use --meter-column")


def load_meter_map(path: str) -> pd.DataFrame:
    meters = pd.read_csv(path, dtype=str).fillna("")
    missing = {"meter", "model", "token_type"} - set(meters)
    if missing:
        raise ValueError(f"meter map lacks columns: {sorted(missing)}")
    meters = meters[(meters["model"].str.strip() != "") & (meters["token_type"].str.strip() != "")]
    meters = meters.assign(
        meter=meters["meter"].str.strip(),
        model=meters["model"].str.strip(),
        token_type=meters["token_type"].str.strip(),
    )
    invalid = sorted(set(meters["token_type"]) - set(TOKEN_TYPES))
    if invalid:
        raise ValueError(f"invalid token_type values {invalid}; use {list(TOKEN_TYPES)}")
    duplicated = meters["meter"][meters["meter"].duplicated()].unique()
    if len(duplicated):
        raise ValueError(f"meters mapped more than once: {list(duplicated)}")
    return meters[["meter", "model", "token_type"]]


AI_PROVIDER = "/providers/microsoft.cognitiveservices/"


def in_scope(
    bill: pd.DataFrame, resources: list[str], resource_groups: list[str] | None = None
) -> pd.Series:
    """Rows of the given AI accounts, plus every AI account of the given resource groups."""
    ids = bill["ResourceId"].fillna("").astype(str).str.lower()
    names = bill["ResourceName"].fillna("").astype(str).str.lower()
    wanted = {resource.strip().lower() for resource in resources}
    selected = ids.isin(wanted) | names.isin(wanted)
    groups = {group.strip().lower() for group in resource_groups or []}
    if groups:
        rg = ids.str.extract(r"/resourcegroups/([^/]+)/", expand=False).fillna("")
        if "x_ResourceGroupName" in bill:
            rg = rg.where(rg != "", bill["x_ResourceGroupName"].fillna("").astype(str).str.lower())
        selected |= rg.isin(groups) & ids.str.contains(AI_PROVIDER, regex=False)
    return selected


def cost_columns(bill: pd.DataFrame) -> list[str]:
    return [c for c in bill.columns if re.fullmatch(r"(x_)?\w*Cost(In\w+)?", c)]


# --------------------------------------------------------------------------- gateway tokens
def gateway_tokens(usage: pd.DataFrame, meter_map: pd.DataFrame, dims: list[str]) -> pd.DataFrame:
    """Long table of billable gateway tokens: date, model, token_type, dims, tokens.

    Prompt tokens include cached and cache-creation tokens. Cache creation is only split
    out for models whose meter map uses ``cache_write``; otherwise it stays in ``input``.
    """
    df = usage.copy()
    for column in ["model", *dims]:
        df[column] = df[column].fillna("").astype(str).replace("", MISSING)
    for column in (
        "prompt_tokens",
        "cached_prompt_tokens",
        "cache_creation_tokens",
        "completion_tokens",
    ):
        df[column] = pd.to_numeric(df.get(column, 0), errors="coerce").fillna(0)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    cache_write_models = set(meter_map.loc[meter_map["token_type"] == "cache_write", "model"])
    split = df["model"].isin(cache_write_models)
    uncached = (df["prompt_tokens"] - df["cached_prompt_tokens"]).clip(lower=0)
    quantities = {
        "input": uncached - df["cache_creation_tokens"].where(split, 0),
        "cached_input": df["cached_prompt_tokens"],
        "cache_write": df["cache_creation_tokens"].where(split, 0),
        "output": df["completion_tokens"],
    }
    parts = []
    for token_type, tokens in quantities.items():
        part = df[["date", "model", *dims]].copy()
        part["token_type"] = token_type
        part["tokens"] = tokens.clip(lower=0)
        parts.append(part)
    long = pd.concat(parts, ignore_index=True)
    long = long[long["tokens"] > 0]
    return long.groupby(["date", "model", "token_type", *dims], as_index=False)["tokens"].sum()


# --------------------------------------------------------------------------- allocation
def scale(rows: pd.DataFrame, ratio: pd.Series, costs: list[str]) -> pd.DataFrame:
    rows = rows.copy()
    for column in [*costs, *QUANTITY_COLUMNS]:
        if column in rows:
            rows[column] = pd.to_numeric(rows[column], errors="coerce") * ratio.to_numpy()
    return rows


def allocation_details(ratio: pd.Series, quantity: pd.Series) -> list[str]:
    return [
        json.dumps(
            {
                "Elements": [
                    {
                        "AllocatedRatio": round(float(r), 10),
                        "UsageUnit": "Tokens",
                        "UsageQuantity": float(q),
                    }
                ]
            }
        )
        for r, q in zip(ratio, quantity, strict=True)
    ]


def allocate(
    bill: pd.DataFrame,
    usage: pd.DataFrame,
    meter_map: pd.DataFrame,
    meter_col: str,
    resources: list[str],
    dims: list[str] | None = None,
    resource_groups: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (allocated FOCUS rows, reconciliation summary).

    ``resources`` (ARM IDs or names) and ``resource_groups`` select the AI accounts behind
    the gateway. Only their charges are reconciled: gateway usage cannot tell which account
    served a call. A resource group contributes only its Microsoft.CognitiveServices accounts.
    """
    if not resources and not resource_groups:
        raise ValueError("at least one gateway backend resource or resource group is required")
    dims = list(dims or DEFAULT_DIMENSIONS)
    costs = cost_columns(bill)
    original_columns = list(bill.columns)

    bill = bill.copy()
    for column in costs:
        bill[column] = pd.to_numeric(bill[column], errors="coerce")
    bill["_start"] = pd.to_datetime(bill["ChargePeriodStart"], utc=True)
    bill["_end"] = pd.to_datetime(bill["ChargePeriodEnd"], utc=True)
    bill["_meter"] = bill[meter_col].fillna("").astype(str).str.strip()
    bill = bill.merge(meter_map.rename(columns={"meter": "_meter"}), on="_meter", how="left")

    bill = bill[in_scope(bill, resources, resource_groups)]
    if bill.empty:
        raise ValueError(f"no bill rows for resources {resources} or groups {resource_groups}")
    mapped = bill[bill["model"].notna()]
    unmapped = bill[bill["model"].isna()]

    tokens = gateway_tokens(usage, meter_map, dims)
    groups = mapped[GROUP_KEYS].drop_duplicates()
    consumers = groups.merge(tokens, on=["model", "token_type"], how="inner")
    consumers = consumers[
        (consumers["date"] >= consumers["_start"]) & (consumers["date"] < consumers["_end"])
    ]
    consumers = consumers.groupby([*GROUP_KEYS, *dims], as_index=False)["tokens"].sum()

    summary = mapped.groupby(GROUP_KEYS, as_index=False).agg(
        billed_tokens=("ConsumedQuantity", lambda s: pd.to_numeric(s, errors="coerce").sum()),
        **{f"billed_{c}": (c, "sum") for c in costs if c in ("BilledCost", "EffectiveCost")},
    )
    gateway_totals = consumers.groupby(GROUP_KEYS, as_index=False)["tokens"].sum()
    summary = summary.merge(
        gateway_totals.rename(columns={"tokens": "gateway_tokens"}), on=GROUP_KEYS, how="left"
    )
    summary["gateway_tokens"] = summary["gateway_tokens"].fillna(0)
    summary["denominator"] = summary[["billed_tokens", "gateway_tokens"]].max(axis=1)
    summary["allocated_ratio"] = (
        summary["gateway_tokens"] / summary["denominator"].where(summary["denominator"] > 0)
    ).fillna(0.0)

    # Allocated rows: each bill charge x each consumer of its group
    shares = consumers.merge(summary[[*GROUP_KEYS, "denominator"]], on=GROUP_KEYS)
    shares["ratio"] = shares["tokens"] / shares["denominator"]
    allocated = mapped.merge(shares, on=GROUP_KEYS, how="inner")
    allocated = scale(allocated, allocated["ratio"], costs)
    allocated["x_AllocationStatus"] = "Allocated"
    allocated["x_UnallocatedReason"] = None

    # Unallocated remainder per bill charge
    remainder = mapped.merge(summary[[*GROUP_KEYS, "allocated_ratio"]], on=GROUP_KEYS)
    remainder["ratio"] = 1.0 - remainder["allocated_ratio"]
    remainder = remainder[remainder["ratio"] > 1e-12]
    remainder = scale(remainder, remainder["ratio"], costs)
    remainder["tokens"] = remainder["ConsumedQuantity"]
    remainder["x_AllocationStatus"] = "Unallocated"
    remainder["x_UnallocatedReason"] = "Consumption not seen by the AI gateway"

    others = unmapped.copy()
    others["ratio"] = 1.0
    others["tokens"] = pd.to_numeric(others.get("ConsumedQuantity"), errors="coerce")
    others["x_AllocationStatus"] = "Unallocated"
    others["x_UnallocatedReason"] = "Meter not mapped to a gateway model"

    result = pd.concat([allocated, remainder, others], ignore_index=True)
    for dim in dims:
        result[dim] = result[dim].where(result["x_AllocationStatus"] == "Allocated")
    allocated_mask = result["x_AllocationStatus"] == "Allocated"
    consumer = result["agent"] if "agent" in result else pd.Series(MISSING, index=result.index)
    result["AllocatedMethodId"] = METHOD_ID
    result["AllocatedResourceId"] = consumer.where(allocated_mask, "unallocated")
    result["AllocatedResourceName"] = consumer.where(allocated_mask, "Unallocated")
    result["AllocatedTags"] = [
        json.dumps({dim: row[dim] for dim in dims}) if allocated else None
        for (_, row), allocated in zip(result[dims].iterrows(), allocated_mask, strict=True)
    ]
    result["AllocatedMethodDetails"] = allocation_details(result["ratio"], result["tokens"])
    result["x_AllocatedRatio"] = result["ratio"]
    result["x_GatewayModel"] = result["model"]
    result["x_GatewayTokenType"] = result["token_type"]
    for dim in dims:
        result[f"x_Allocated{''.join(p.title() for p in dim.split('_'))}"] = result[dim]

    extra = [
        "AllocatedMethodId",
        "AllocatedMethodDetails",
        "AllocatedResourceId",
        "AllocatedResourceName",
        "AllocatedTags",
        "x_AllocationStatus",
        "x_UnallocatedReason",
        "x_AllocatedRatio",
        "x_GatewayModel",
        "x_GatewayTokenType",
        *[f"x_Allocated{''.join(p.title() for p in dim.split('_'))}" for dim in dims],
    ]
    columns = [c for c in original_columns if c not in extra] + extra
    result = result[columns].reset_index(drop=True)

    summary = summary.drop(columns=["denominator"])
    summary["coverage"] = summary["gateway_tokens"] / summary["billed_tokens"].where(
        summary["billed_tokens"] > 0
    )
    for column in [c for c in summary if c.startswith("billed_") and c != "billed_tokens"]:
        summary[column.replace("billed_", "allocated_")] = (
            summary[column] * summary["allocated_ratio"]
        )
    summary["charge_period_start"] = summary["_start"].dt.strftime("%Y-%m-%d")
    summary["charge_period_end"] = summary["_end"].dt.strftime("%Y-%m-%d")
    summary = summary.drop(columns=["_start", "_end"])
    first = ["charge_period_start", "charge_period_end", "model", "token_type"]
    summary = summary[first + [c for c in summary if c not in first]]
    numeric = summary.select_dtypes("number").columns
    summary[numeric] = summary[numeric].round(8)

    check_totals(bill, result, costs)
    return result, summary.sort_values(first).reset_index(drop=True)


def check_totals(scope_bill: pd.DataFrame, result: pd.DataFrame, costs: list[str]) -> None:
    for column in costs:
        before = pd.to_numeric(scope_bill[column], errors="coerce").sum()
        after = pd.to_numeric(result[column], errors="coerce").sum()
        if abs(before - after) > TOLERANCE * max(1.0, abs(before)):
            raise AssertionError(f"{column} does not reconcile: bill {before} vs output {after}")


# --------------------------------------------------------------------------- meter map template
def normalise(text: str) -> str:
    return re.sub(r"[\s_\-]+", " ", text.lower().replace("gpt-", "").replace("gpt ", "")).strip()


def guess_token_type(meter: str) -> str:
    name = f" {meter.lower()} "
    if re.search(r"\b(cd|cached|cache)\s*(wr|write)", name):
        return "input"
    if re.search(r"\b(cd|cached)\s*(inp|input)", name):
        return "cached_input"
    if re.search(r"\b(opt|outp|output)\b", name):
        return "output"
    if re.search(r"\b(inp|input)\b", name):
        return "input"
    return ""


def guess_model(meter: str, models: list[str]) -> str:
    name = normalise(meter)
    matches = [m for m in models if normalise(m) and normalise(m) in name]
    return max(matches, key=len) if matches else ""


def meter_map_template(
    bill: pd.DataFrame,
    meter_col: str,
    models: list[str],
    resources: list[str],
    resource_groups: list[str] | None = None,
) -> pd.DataFrame:
    ai = bill[in_scope(bill, resources, resource_groups)] if resources or resource_groups else bill
    ids = ai["ResourceId"].fillna("").astype(str)
    ai = ai[ids.str.contains("/microsoft.cognitiveservices/", case=False)]
    if ai.empty:
        return pd.DataFrame(columns=["meter", "model", "token_type"])
    grouped = (
        ai.groupby(meter_col, dropna=True)
        .agg(
            resource_name=(
                "ResourceName",
                lambda s: ", ".join(sorted(set(s.dropna().astype(str)))),
            ),
            billed_quantity=("ConsumedQuantity", lambda s: pd.to_numeric(s, errors="coerce").sum()),
            list_unit_price=("ListUnitPrice", "max"),
        )
        .reset_index()
        .rename(columns={meter_col: "meter"})
    )
    grouped["model"] = grouped["meter"].map(lambda m: guess_model(str(m), models))
    grouped["token_type"] = grouped["meter"].map(lambda m: guess_token_type(str(m)))
    grouped["note"] = grouped["meter"].map(
        lambda m: (
            "cache write: APIM counts it in Prompt Tokens; keep as input unless the "
            "gateway reports Cache Creation Tokens"
            if re.search(r"(cd|cache\w*)\s*(wr|write)", str(m).lower())
            else ""
        )
    )
    return grouped[
        [
            "meter",
            "model",
            "token_type",
            "resource_name",
            "billed_quantity",
            "list_unit_price",
            "note",
        ]
    ]


# --------------------------------------------------------------------------- main
def write_table(df: pd.DataFrame, path: str) -> None:
    if path.endswith(".parquet"):
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path, index=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--bill",
        required=True,
        action="append",
        help="FOCUS export file or folder (CSV/Parquet); repeatable",
    )
    parser.add_argument("--usage", required=True, help="Gateway usage from tools.gateway_usage")
    parser.add_argument("--meter-map", help="CSV with meter,model,token_type")
    parser.add_argument(
        "--meter-column", help="Bill meter column (default: SkuMeter or x_SkuMeterName)"
    )
    parser.add_argument(
        "--resource",
        action="append",
        default=[],
        help="AI account behind the gateway (ARM ID or resource name); repeatable. "
        "Required to allocate (or --resource-group); optional filter for --write-meter-map",
    )
    parser.add_argument(
        "--resource-group",
        action="append",
        default=[],
        help="Resource group whose AI accounts (Microsoft.CognitiveServices) are behind the "
        "gateway; repeatable",
    )
    parser.add_argument(
        "--dimensions",
        default=",".join(DEFAULT_DIMENSIONS),
        help="Gateway dimensions used as allocation keys",
    )
    parser.add_argument("--write-meter-map", help="Write a meter map template and exit")
    parser.add_argument("--out-dir", help="Output folder")
    parser.add_argument("--format", choices=["csv", "parquet"], default="csv")
    args = parser.parse_args(argv)

    bill = read_bill(args.bill)
    usage = read_usage(args.usage)
    meter_col = meter_column(bill, args.meter_column)

    if args.write_meter_map:
        models = sorted(m for m in usage["model"].dropna().astype(str).unique() if m != MISSING)
        template = meter_map_template(bill, meter_col, models, args.resource, args.resource_group)
        template.to_csv(args.write_meter_map, index=False)
        print(
            f"Wrote {len(template)} meters to {args.write_meter_map} (column {meter_col}). "
            "Review model and token_type before allocating.",
            file=sys.stderr,
        )
        return 0
    if not (args.meter_map and args.out_dir):
        parser.error("--meter-map and --out-dir are required (or use --write-meter-map)")
    if not (args.resource or args.resource_group):
        parser.error("set --resource or --resource-group: the AI accounts behind the gateway")

    dims = [d.strip() for d in args.dimensions.split(",") if d.strip()]
    missing = sorted(set(dims) - set(usage.columns))
    if missing:
        parser.error(f"dimensions not in usage file: {missing}")
    result, summary = allocate(
        bill,
        usage,
        load_meter_map(args.meter_map),
        meter_col,
        args.resource,
        dims,
        args.resource_group,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    allocated_path = os.path.join(args.out_dir, f"allocated_focus.{args.format}")
    summary_path = os.path.join(args.out_dir, "reconciliation_summary.csv")
    write_table(result, allocated_path)
    summary.to_csv(summary_path, index=False)

    cost = "EffectiveCost" if "EffectiveCost" in result else cost_columns(result)[0]
    status = result.groupby("x_AllocationStatus")[cost].sum()
    total = status.sum()
    print(f"Wrote {len(result)} rows to {allocated_path} and {summary_path}", file=sys.stderr)
    for name, value in status.items():
        share = value / total if total else 0
        print(f"  {name}: {value:,.4f} {cost} ({share:.1%})", file=sys.stderr)
    print(f"  Total: {total:,.4f} {cost} (matches the bill in scope)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
