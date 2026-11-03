from decimal import Decimal
import json

import pytest

from cost_tracker import CostTracker, DEFAULT_PRICING, Price, PricingError, load_price


def usage(inp=1000, out=100, cached=0, writes=0):
    return {"input_tokens": inp, "output_tokens": out,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": writes}}


def tracker(price=None, source="simulation"):
    return CostTracker(price or load_price("gpt-4.1-mini"), source=source)


def settle(t, u, request="1", **fields):
    t.reserve(request, Decimal("0.001"))
    return t.record(request, u, **fields)


def test_normal_usage_formula_and_api_measurement_label():
    t = tracker(source="api")
    assert settle(t, usage())["cost_usd"] == "0.00056"
    assert t.snapshot()["api_measured_cost_usd"] == "0.00056"
    assert t.snapshot()["simulated_cost_usd"] is None


def test_cached_input_is_subtracted_not_counted_twice():
    t = tracker()
    record = settle(t, usage(cached=500))
    assert Decimal(record["cost_usd"]) == Decimal("0.00041")
    assert t.input_tokens == 1000 and record["cached_tokens"] == 500
    assert t.snapshot()["api_measured_cost_usd"] is None


def test_totals_across_all_calls():
    t = tracker()
    settle(t, usage(), "1")
    settle(t, usage(cached=500), "2")
    assert t.known_cost == Decimal("0.00097")
    assert t.input_tokens == 2000 and t.output_tokens == 200
    with pytest.raises(ValueError):
        t.record("2", usage())


def test_distinct_models_have_distinct_prices():
    first, second = tracker(), tracker(load_price("gpt-4o-mini"))
    assert Decimal(settle(first, usage())["cost_usd"]) > Decimal(settle(second, usage())["cost_usd"])
    assert second.price.cached_input == Decimal("0.075")


@pytest.mark.parametrize("value", [None, {}, usage(-1), usage(cached=1001), usage(inp=True), usage(cached=-1)])
def test_missing_or_invalid_usage_is_unknown_not_zero(value):
    t = tracker()
    result = settle(t, value)
    assert result["cost_usd"] is None
    assert t.snapshot()["total_cost_usd"] is None
    assert t.committed == Decimal("0.001")
    assert t.snapshot()["usage_complete"] is False


def test_missing_cache_details_is_estimate_only():
    t = tracker()
    record = settle(t, {"input_tokens": 100, "output_tokens": 5})
    assert record["error_code"] == "CACHE_USAGE_UNAVAILABLE"
    assert record["cost_usd"] is None


@pytest.mark.parametrize("fields,code", [
    ({"model": "another-model"}, "MODEL_PRICE_MISMATCH"),
    ({"service_tier": "priority"}, "TIER_PRICE_MISMATCH"),
])
def test_unpriced_response_variants_fail_closed(fields, code):
    assert settle(tracker(), usage(), **fields)["error_code"] == code


def test_nonzero_unpriced_cache_write_does_not_get_free_pricing():
    assert settle(tracker(), usage(writes=5))["error_code"] == "UNPRICED_CACHE_WRITE"


def test_explicit_additional_charges_and_tier_are_supported(tmp_path):
    data = json.loads(DEFAULT_PRICING.read_text(encoding="utf-8"))
    data["models"]["gpt-4.1-mini-2025-04-14"]["tiers"]["priority"] = {
        "input": "0.8", "cached_input": "0.2", "output": "3.2",
        "fixed_request_usd": "0.001", "multiplier": "1.1", "cache_write_surcharge": "0.1"}
    file = tmp_path / "prices.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    price = load_price("gpt-4.1-mini", file, tier="priority")
    t = tracker(price)
    record = settle(t, usage(cached=500, writes=10), service_tier="priority")
    expected = ((Decimal(500)*Decimal("0.8") + Decimal(500)*Decimal("0.2") + Decimal(100)*Decimal("3.2") + Decimal(10)*Decimal("0.1"))/1_000_000 + Decimal("0.001"))*Decimal("1.1")
    assert Decimal(record["cost_usd"]) == expected
    assert t.estimate(1000, 100) >= expected


def test_unknown_model_and_missing_tier_have_no_fallback():
    with pytest.raises(PricingError):
        load_price("unconfigured-model")
    with pytest.raises(PricingError):
        load_price("gpt-4.1-mini", tier="priority")


def test_price_metadata_and_input_validation():
    p = load_price("gpt-4.1-mini")
    assert p.provider == "OpenAI" and p.verified_at == "2026-11-03"
    assert p.source_url.startswith("https://developers.openai.com/")
    with pytest.raises(PricingError):
        Price("x", Decimal("1"), Decimal("2"), Decimal("1"))


def test_live_configuration_loads_versioned_catalog_without_api_calls(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    for key, value in {"OPENAI_MODEL": "gpt-4.1-mini", "OPENAI_PRICING_FILE": str(DEFAULT_PRICING),
                       "OPENAI_SERVICE_TIER": "default", "AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
                       "MAX_TURNS": "8", "BUDGET_USD": "0.02", "PERMISSION_POLICY": "ask_on_write",
                       "MAX_OUTPUT_TOKENS": "512", "OPENAI_TIMEOUT_SECONDS": "30", "OPENAI_MAX_RETRIES": "1"}.items():
        monkeypatch.setenv(key, value)
    result = config.AgentConfig.from_env()
    assert result.input_price == Decimal("0.40") and result.cached_input_price == Decimal("0.10")
    assert result.pricing.verified_at == "2026-11-03" and result.usage_source == "api"
    assert result.audit_path == tmp_path / "audit.jsonl"
    assert not result.audit_path.exists()
