"""Daily summary of AI gateway token usage, read from Application Insights.

APIM's ``llm-emit-token-metric`` policy writes token metrics to the ``customMetrics`` table
with business dimensions (model, agent, product, subscription, department, team, user).
This script summarises them into one row per UTC day and dimension combination: the
"gateway usage" input of the reconcile-and-allocate step.

User identities are pseudonymised with a keyed hash (HMAC-SHA256). The key comes from
``GATEWAY_USAGE_HASH_KEY`` and must stay stable, so the same user always gets the same hash.

Usage:
  export GATEWAY_USAGE_HASH_KEY="<secret>"
  python -m tools.gateway_usage \\
      --app-insights-resource-id <APP_INSIGHTS_ARM_ID> \\
      --start 2026-10-01 --end 2026-11-01 --out gateway_usage.csv

Dates are UTC; --end is exclusive.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
from datetime import UTC, datetime, timedelta

import pandas as pd

METRICS = {
    "Prompt Tokens": "prompt_tokens",
    "Prompt Cached Tokens": "cached_prompt_tokens",
    "Cache Creation Tokens": "cache_creation_tokens",
    "Completion Tokens": "completion_tokens",
    "Completion Reasoning Tokens": "reasoning_tokens",
    "Total Tokens": "total_tokens",
}
DIMENSIONS = [
    "service",
    "region",
    "model",
    "agent",
    "product",
    "subscription_id",
    "department",
    "team",
    "user",
]
TOKEN_COLUMNS = list(METRICS.values())
COLUMNS = ["date", *DIMENSIONS, "requests", *TOKEN_COLUMNS]
MISSING = "N/A"

KQL = """
customMetrics
| where timestamp >= datetime({start}) and timestamp < datetime({end})
| where name in ({names})
| extend service = tostring(customDimensions["Service Name"]),
         region = tostring(customDimensions["Region"]),
         model = tostring(customDimensions["model"]),
         agent = tostring(customDimensions["agent"]),
         product = tostring(customDimensions["product"]),
         subscription_id = tostring(customDimensions["subscriptionId"]),
         department = tostring(customDimensions["department"]),
         team = tostring(customDimensions["team"]),
         user = tostring(customDimensions["user"])
| summarize value = sum(valueSum), samples = sum(valueCount)
    by day = startofday(timestamp), name, service, region, model, agent, product,
       subscription_id, department, team, user
"""


def build_query(start: datetime, end: datetime) -> str:
    names = ", ".join(f'"{name}"' for name in METRICS)
    return KQL.format(start=start.isoformat(), end=end.isoformat(), names=names)


def hash_user(user: str, key: bytes) -> str:
    if user == MISSING:
        return MISSING
    digest = hmac.new(key, user.strip().lower().encode(), hashlib.sha256).hexdigest()
    return f"u_{digest[:16]}"


def summarize(raw: pd.DataFrame, hash_key: bytes | None) -> pd.DataFrame:
    """Pivot raw (date, name, dimensions, value, samples) rows into the daily usage schema."""
    if raw.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.floor("D")
    for column in DIMENSIONS:
        values = df[column] if column in df else pd.Series("", index=df.index)
        df[column] = values.fillna("").astype(str).str.strip().replace("", MISSING)
    if hash_key is not None:
        df["user"] = df["user"].map(lambda user: hash_user(user, hash_key))
    df = df[df["name"].isin(METRICS)]
    df["metric"] = df["name"].map(METRICS)
    keys = ["date", *DIMENSIONS]
    tokens = df.pivot_table(
        index=keys, columns="metric", values="value", aggfunc="sum", fill_value=0
    )
    requests = df[df["metric"] == "total_tokens"].groupby(keys)["samples"].sum().rename("requests")
    out = tokens.join(requests, how="left").reset_index()
    for column in ["requests", *TOKEN_COLUMNS]:
        if column not in out:
            out[column] = 0
        out[column] = pd.to_numeric(out[column]).fillna(0).astype("int64")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out[COLUMNS].sort_values(keys).reset_index(drop=True)


def query_day(client, resource_id: str, day: datetime) -> pd.DataFrame:
    from azure.monitor.query import LogsQueryStatus

    end = day + timedelta(days=1)
    response = client.query_resource(resource_id, build_query(day, end), timespan=(day, end))
    if response.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(f"Partial or failed query for {day:%Y-%m-%d}: {response.partial_error}")
    table = response.tables[0]
    columns = [getattr(column, "name", column) for column in table.columns]
    return pd.DataFrame(table.rows, columns=columns).rename(columns={"day": "date"})


def load_live(resource_id: str, start: datetime, end: datetime) -> pd.DataFrame:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient

    client = LogsQueryClient(DefaultAzureCredential())
    frames = []
    day = start
    while day < end:
        frames.append(query_day(client, resource_id, day))
        day += timedelta(days=1)
    frames = [frame for frame in frames if not frame.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def write_table(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if path.endswith(".parquet"):
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path, index=False)


def parse_day(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed != parsed.replace(hour=0, minute=0, second=0, microsecond=0):
        raise argparse.ArgumentTypeError(f"{value}: use whole UTC days (YYYY-MM-DD)")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--app-insights-resource-id", required=True, help="Application Insights ARM ID"
    )
    parser.add_argument("--start", required=True, type=parse_day, help="UTC day, inclusive")
    parser.add_argument("--end", required=True, type=parse_day, help="UTC day, exclusive")
    parser.add_argument("--out", required=True, help="Output file (.csv or .parquet)")
    parser.add_argument(
        "--keep-user-ids",
        action="store_true",
        help="Write user identities in clear text instead of a keyed hash",
    )
    args = parser.parse_args(argv)
    if args.end <= args.start:
        parser.error("--end must be after --start")

    hash_key = None
    if not args.keep_user_ids:
        secret = os.environ.get("GATEWAY_USAGE_HASH_KEY", "")
        if not secret:
            parser.error("set GATEWAY_USAGE_HASH_KEY, or pass --keep-user-ids explicitly")
        hash_key = secret.encode()

    usage = summarize(load_live(args.app_insights_resource_id, args.start, args.end), hash_key)
    write_table(usage, args.out)
    unknown_model = usage.loc[usage["model"] == MISSING, "total_tokens"].sum()
    print(
        f"Wrote {len(usage)} rows to {args.out}: {usage['total_tokens'].sum():,} tokens, "
        f"{usage['requests'].sum():,} requests; {unknown_model:,} tokens without model",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
