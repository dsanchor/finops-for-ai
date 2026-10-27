import json

import pandas as pd
import pytest

from tools import gateway_usage, reconcile_allocate
from tools.reconcile_allocate import allocate, gateway_tokens, guess_model, guess_token_type

RESOURCE = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/ai"
OTHER = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st"


def bill_row(meter, quantity, cost, day="2026-10-26", resource=RESOURCE, unit_price=None):
    start = pd.Timestamp(day, tz="UTC")
    return {
        "BilledCost": cost,
        "EffectiveCost": cost,
        "ListCost": cost * 1.1,
        "ContractedCost": cost,
        "x_BilledCostInUsd": cost,
        "BillingCurrency": "USD",
        "ChargePeriodStart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ChargePeriodEnd": (start + pd.Timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ResourceId": resource,
        "ResourceName": resource.rsplit("/", 1)[-1],
        "x_SkuMeterName": meter,
        "ConsumedQuantity": quantity,
        "PricingQuantity": quantity / 1e6,
        "ListUnitPrice": unit_price,
    }


def usage_row(model, agent, user, prompt=0, cached=0, completion=0, day="2026-10-26", creation=0):
    return {
        "date": day,
        "service": "apim",
        "region": "Sweden Central",
        "model": model,
        "agent": agent,
        "product": "free",
        "subscription_id": f"sub-{user}",
        "department": "finance",
        "team": "risk",
        "user": user,
        "requests": 1,
        "prompt_tokens": prompt,
        "cached_prompt_tokens": cached,
        "cache_creation_tokens": creation,
        "completion_tokens": completion,
        "reasoning_tokens": 0,
        "total_tokens": prompt + completion,
    }


METER_MAP = pd.DataFrame(
    [
        ("sol Inp", "gpt-sol", "input"),
        ("sol Cd Wr", "gpt-sol", "input"),
        ("sol Cd Inp", "gpt-sol", "cached_input"),
        ("sol Opt", "gpt-sol", "output"),
    ],
    columns=["meter", "model", "token_type"],
)


def run(bill_rows, usage_rows, meter_map=METER_MAP):
    return allocate(
        pd.DataFrame(bill_rows), pd.DataFrame(usage_rows), meter_map, "x_SkuMeterName", [RESOURCE]
    )


def test_gateway_usage_summary_pivots_hashes_and_counts_requests():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-10-26T00:00:00Z",
                "name": name,
                "service": "apim",
                "region": "r",
                "model": "gpt-sol",
                "agent": "risk",
                "product": "free",
                "subscription_id": "s1",
                "department": "finance",
                "team": "risk",
                "user": "Ana@Contoso.com",
                "value": value,
                "samples": samples,
            }
            for name, value, samples in [
                ("Prompt Tokens", 100, 2),
                ("Prompt Cached Tokens", 40, 2),
                ("Completion Tokens", 30, 2),
                ("Total Tokens", 130, 2),
            ]
        ]
        + [
            {
                "date": "2026-10-26T05:00:00Z",
                "name": "Total Tokens",
                "service": "apim",
                "region": "r",
                "model": "",
                "agent": "",
                "product": "free",
                "subscription_id": "s1",
                "department": "",
                "team": "",
                "user": "",
                "value": 5,
                "samples": 1,
            }
        ]
    )
    usage = gateway_usage.summarize(raw, b"key")
    assert list(usage.columns) == gateway_usage.COLUMNS
    known = usage[usage["model"] == "gpt-sol"].iloc[0]
    assert known["date"] == "2026-10-26"
    assert (known["prompt_tokens"], known["cached_prompt_tokens"]) == (100, 40)
    assert (known["completion_tokens"], known["total_tokens"], known["requests"]) == (30, 130, 2)
    assert known["user"] == gateway_usage.hash_user("ana@contoso.com", b"key")
    assert known["user"].startswith("u_") and "contoso" not in known["user"]
    unknown = usage[usage["model"] == "N/A"].iloc[0]
    assert (unknown["agent"], unknown["user"], unknown["total_tokens"]) == ("N/A", "N/A", 5)


def test_gateway_usage_keeps_user_ids_without_hash_key():
    raw = pd.DataFrame(
        [
            {
                "date": "2026-10-26",
                "name": "Total Tokens",
                "user": "ana@x.com",
                "value": 1,
                "samples": 1,
            }
        ]
    )
    assert gateway_usage.summarize(raw, None)["user"].iloc[0] == "ana@x.com"


def test_gateway_usage_requires_hash_key_unless_explicit(monkeypatch):
    monkeypatch.delenv("GATEWAY_USAGE_HASH_KEY", raising=False)
    with pytest.raises(SystemExit):
        gateway_usage.main(
            [
                "--app-insights-resource-id",
                "x",
                "--start",
                "2026-10-01",
                "--end",
                "2026-10-02",
                "--out",
                "o.csv",
            ]
        )


def test_gateway_tokens_folds_cache_creation_into_input_unless_mapped():
    usage = pd.DataFrame(
        [usage_row("gpt-sol", "risk", "a", prompt=100, cached=30, completion=10, creation=20)]
    )
    folded = gateway_tokens(usage, METER_MAP, ["agent"]).set_index("token_type")["tokens"]
    assert folded.to_dict() == {"cached_input": 30, "input": 70, "output": 10}
    split_map = pd.concat(
        [
            METER_MAP,
            pd.DataFrame(
                [("sol Wr", "gpt-sol", "cache_write")], columns=["meter", "model", "token_type"]
            ),
        ]
    )
    split = gateway_tokens(usage, split_map, ["agent"]).set_index("token_type")["tokens"]
    assert split.to_dict() == {"cache_write": 20, "cached_input": 30, "input": 50, "output": 10}


def test_allocation_splits_real_cost_by_token_share_and_reconciles():
    bill = [
        bill_row("sol Inp", 100, 0.4),
        bill_row("sol Cd Wr", 900, 4.5),
        bill_row("sol Cd Inp", 200, 0.08),
        bill_row("sol Opt", 500, 10.0),
    ]
    usage = [
        usage_row("gpt-sol", "risk", "a", prompt=900, cached=150, completion=300),
        usage_row("gpt-sol", "risk", "b", prompt=300, cached=50, completion=200),
    ]
    result, summary = run(bill, usage)

    assert result["EffectiveCost"].sum() == pytest.approx(15.0 * 1.0 - 0.02)
    assert result["x_BilledCostInUsd"].sum() == pytest.approx(14.98)
    assert set(result["x_AllocationStatus"]) == {"Allocated"}
    by_user = result.groupby("x_AllocatedUser")["EffectiveCost"].sum()
    # input: 1000 billed (Inp + Cd Wr) split 750/250; cached 150/50; output 300/200
    assert by_user["a"] == pytest.approx(4.9 * 0.75 + 0.08 * 0.75 + 10 * 0.6)
    assert by_user["b"] == pytest.approx(4.9 * 0.25 + 0.08 * 0.25 + 10 * 0.4)
    cache_write = result[result["x_SkuMeterName"] == "sol Cd Wr"]
    assert cache_write["ConsumedQuantity"].sum() == pytest.approx(900)
    assert set(result["AllocatedResourceId"]) == {"risk"}
    tags = json.loads(result["AllocatedTags"].iloc[0])
    assert set(tags) == {"agent", "department", "team", "user", "product", "subscription_id"}
    details = json.loads(result["AllocatedMethodDetails"].iloc[0])["Elements"][0]
    assert details["UsageUnit"] == "Tokens" and 0 < details["AllocatedRatio"] <= 1
    assert summary["coverage"].tolist() == pytest.approx([1.0, 1.0, 1.0])


def test_unseen_consumption_stays_unallocated_and_totals_match():
    bill = [bill_row("sol Opt", 1000, 20.0)]
    usage = [
        usage_row("gpt-sol", "risk", "a", completion=600),
        usage_row("N/A", "N/A", "N/A", completion=300),
    ]
    result, summary = run(bill, usage)
    status = result.groupby("x_AllocationStatus")["EffectiveCost"].sum()
    assert status["Allocated"] == pytest.approx(12.0)
    assert status["Unallocated"] == pytest.approx(8.0)
    unallocated = result[result["x_AllocationStatus"] == "Unallocated"].iloc[0]
    assert unallocated["AllocatedResourceId"] == "unallocated"
    assert unallocated["ConsumedQuantity"] == pytest.approx(400)
    assert summary.iloc[0]["coverage"] == pytest.approx(0.6)
    assert summary.iloc[0]["allocated_EffectiveCost"] == pytest.approx(12.0)


def test_gateway_above_bill_allocates_everything_without_negative_remainder():
    result, _ = run(
        [bill_row("sol Opt", 100, 2.0)],
        [
            usage_row("gpt-sol", "risk", "a", completion=90),
            usage_row("gpt-sol", "risk", "b", completion=60),
        ],
    )
    assert set(result["x_AllocationStatus"]) == {"Allocated"}
    assert result["EffectiveCost"].sum() == pytest.approx(2.0)
    assert result.groupby("x_AllocatedUser")["EffectiveCost"].sum()["a"] == pytest.approx(1.2)


def test_charge_periods_only_use_usage_of_the_same_day():
    bill = [
        bill_row("sol Opt", 100, 1.0, day="2026-10-25"),
        bill_row("sol Opt", 100, 1.0, day="2026-10-26"),
    ]
    usage = [
        usage_row("gpt-sol", "risk", "a", completion=100, day="2026-10-25"),
        usage_row("gpt-sol", "risk", "b", completion=100, day="2026-10-26"),
    ]
    result, _ = run(bill, usage)
    pairs = set(zip(result["ChargePeriodStart"].str[:10], result["x_AllocatedUser"], strict=True))
    assert pairs == {("2026-10-25", "a"), ("2026-10-26", "b")}


def test_unmapped_meters_of_scope_resources_are_kept_and_other_resources_dropped():
    bill = [
        bill_row("sol Opt", 100, 1.0),
        bill_row("Speech", 5, 0.5),
        bill_row("Blob", 1, 3.0, resource=OTHER),
    ]
    result, _ = run(bill, [usage_row("gpt-sol", "risk", "a", completion=100)])
    assert result["EffectiveCost"].sum() == pytest.approx(1.5)
    speech = result[result["x_SkuMeterName"] == "Speech"].iloc[0]
    assert speech["x_UnallocatedReason"] == "Meter not mapped to a gateway model"
    assert OTHER not in set(result["ResourceId"])


def test_meter_map_validation(tmp_path):
    path = tmp_path / "map.csv"
    path.write_text("meter,model,token_type\nA,m,input\nB,m,bogus\n")
    with pytest.raises(ValueError, match="invalid token_type"):
        reconcile_allocate.load_meter_map(str(path))
    path.write_text("meter,model,token_type\nA,m,input\nA,m,output\nC,,\n")
    with pytest.raises(ValueError, match="more than once"):
        reconcile_allocate.load_meter_map(str(path))


@pytest.mark.parametrize(
    ("meter", "token_type"),
    [
        ("5.4 mini Inp Gl 1M Tokens", "input"),
        ("5.4 mini Opt Gl 1M Tokens", "output"),
        ("5.6 sol ShortCo Cd Inp Std Gl 1M Tokens", "cached_input"),
        ("5.6 sol ShortCo Cd Wr Std Gl 1M Tokens", "input"),
        ("gpt-4o-0806 Input global Tokens", "input"),
        ("gpt-4o-0806 cached Input global Tokens", "cached_input"),
        ("Standard Tokens", ""),
    ],
)
def test_meter_token_type_guesses(meter, token_type):
    assert guess_token_type(meter) == token_type


def test_meter_model_guess_prefers_longest_match():
    models = ["gpt-5.4-mini", "gpt-5.6-sol", "gpt-5.4"]
    assert guess_model("5.4 mini Inp Gl 1M Tokens", models) == "gpt-5.4-mini"
    assert guess_model("5.6 sol ShortCo Opt Std Gl 1M Tokens", models) == "gpt-5.6-sol"
    assert guess_model("Standard Tokens", models) == ""


def test_cli_end_to_end(tmp_path):
    bill = tmp_path / "bill"
    bill.mkdir()
    pd.DataFrame([bill_row("sol Opt", 100, 2.0), bill_row("sol Inp", 100, 0.5)]).to_csv(
        bill / "part_0.csv", index=False
    )
    usage = tmp_path / "usage.csv"
    pd.DataFrame([usage_row("gpt-sol", "risk", "a", prompt=100, completion=50)]).to_csv(
        usage, index=False
    )
    template = tmp_path / "map.csv"
    assert (
        reconcile_allocate.main(
            ["--bill", str(bill), "--usage", str(usage), "--write-meter-map", str(template)]
        )
        == 0
    )
    written = pd.read_csv(template).set_index("meter")
    assert written.loc["sol Opt", "token_type"] == "output"
    assert written.loc["sol Opt", "model"] == "gpt-sol"
    out = tmp_path / "out"
    assert (
        reconcile_allocate.main(
            [
                "--bill",
                str(bill),
                "--usage",
                str(usage),
                "--meter-map",
                str(template),
                "--out-dir",
                str(out),
                "--resource",
                "ai",
            ]
        )
        == 0
    )
    allocated = pd.read_csv(out / "allocated_focus.csv")
    assert allocated["EffectiveCost"].sum() == pytest.approx(2.5)
    assert (out / "reconciliation_summary.csv").exists()


def test_same_model_billed_in_another_account_is_out_of_scope():
    other_ai = RESOURCE.replace("/accounts/ai", "/accounts/other")
    bill = [bill_row("sol Opt", 100, 1.0), bill_row("sol Opt", 900, 9.0, resource=other_ai)]
    result, summary = run(bill, [usage_row("gpt-sol", "risk", "a", completion=100)])
    assert result["EffectiveCost"].sum() == pytest.approx(1.0)
    assert set(result["x_AllocationStatus"]) == {"Allocated"}
    assert summary.iloc[0]["billed_tokens"] == 100


def test_allocation_requires_backend_resources():
    with pytest.raises(ValueError, match="resource"):
        allocate(
            pd.DataFrame([bill_row("sol Opt", 1, 1.0)]),
            pd.DataFrame([]),
            METER_MAP,
            "x_SkuMeterName",
            [],
        )


def test_resource_group_scope_keeps_only_its_ai_accounts():
    in_rg = RESOURCE
    other_rg = RESOURCE.replace("/resourceGroups/rg/", "/resourceGroups/elsewhere/")
    bill = [
        bill_row("sol Opt", 100, 1.0, resource=in_rg),
        bill_row("sol Opt", 900, 9.0, resource=other_rg),
        bill_row("Blob", 1, 3.0, resource=OTHER),
    ]
    result, _ = allocate(
        pd.DataFrame(bill),
        pd.DataFrame([usage_row("gpt-sol", "risk", "a", completion=100)]),
        METER_MAP,
        "x_SkuMeterName",
        [],
        resource_groups=["RG"],
    )
    assert result["EffectiveCost"].sum() == pytest.approx(1.0)
    assert set(result["ResourceId"]) == {in_rg}


def test_cli_accepts_resource_group(tmp_path):
    bill = tmp_path / "bill.csv"
    pd.DataFrame([bill_row("sol Opt", 100, 2.0)]).to_csv(bill, index=False)
    usage = tmp_path / "usage.csv"
    pd.DataFrame([usage_row("gpt-sol", "risk", "a", completion=100)]).to_csv(usage, index=False)
    meter_map = tmp_path / "map.csv"
    METER_MAP.to_csv(meter_map, index=False)
    out = tmp_path / "out"
    args = [
        "--bill",
        str(bill),
        "--usage",
        str(usage),
        "--meter-map",
        str(meter_map),
        "--out-dir",
        str(out),
    ]
    with pytest.raises(SystemExit):
        reconcile_allocate.main(args)
    assert reconcile_allocate.main([*args, "--resource-group", "rg"]) == 0
    assert pd.read_csv(out / "allocated_focus.csv")["EffectiveCost"].sum() == pytest.approx(2.0)
