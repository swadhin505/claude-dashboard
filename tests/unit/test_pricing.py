import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from claude_metrics.pricing.calculator import (
    AUTO_POLICY,
    PricingPolicy,
    calculate,
    canonical,
    pricing_input,
    recompute_nanos,
)
from claude_metrics.pricing.catalog import Catalog, load_catalog

GOLDEN = json.loads((Path(__file__).parents[1] / "golden/cost-examples.json").read_text())


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def inputs(**changes):
    row = {
        "model_raw": "claude-sonnet-4-6",
        "provider": "anthropic",
        "speed": "standard",
        "service_tier": "standard",
        "inference_geo": "global",
        "occurred_at_us": 1,
        **GOLDEN["cases"][0]["tokens"],
        **changes,
    }
    return pricing_input(row, {})


@pytest.mark.parametrize("example", GOLDEN["cases"], ids=lambda x: x["name"])
def test_hand_calculated_exclusive_buckets(catalog, example):
    result = calculate(inputs(**example["tokens"]), catalog)
    assert result["calculated_cost_nanos"] == example["expected_cost_nanos"]
    assert result["status"] == "COMPLETE"
    assert recompute_nanos(result) == example["expected_cost_nanos"]
    assert result["coverage"]["unpriced_tokens"] == 0
    assert "ESTIMATED_CURRENT_CATALOG" in result["estimates"]


def test_web_search_uses_per_query_rate_not_token_denominator(catalog):
    value = inputs()
    value["billable_units"] = {"web_search_requests": 2}
    result = calculate(value, catalog)
    assert (
        result["calculated_cost_nanos"]
        == 1_620_000 + GOLDEN["web_search_example"]["expected_cost_nanos"]
    )
    assert result["rates"]["web_search_requests"]["basis"] == 1
    assert recompute_nanos(result) == result["calculated_cost_nanos"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"provider": "unknown"}, "unknown_or_unsupported_provider"),
        ({"provider": "bedrock"}, "unknown_or_unsupported_provider"),
        ({"model_raw": "claude-sonnet-made-up"}, "unknown_model"),
        ({"model_raw": "anthropic.claude-sonnet-4-20250514-v1:0"}, "unknown_model"),
        ({"provider_region": "us-east-1"}, "unsupported_provider_region"),
        ({"speed": "turbo"}, "unsupported_speed"),
        ({"speed": "fast"}, "missing_fast_rate"),
        ({"service_tier": "priority"}, "unsupported_service_tier"),
        ({"inference_geo": "eu"}, "unsupported_inference_geo"),
    ],
)
def test_unknown_provider_model_or_modifier_never_falls_back(catalog, change, reason):
    result = calculate(inputs(**change), catalog)
    assert result["status"] == "UNPRICED" and result["effective_cost_nanos"] is None
    assert result["coverage"]["unpriced_tokens"] == 450
    assert reason in result["missing"]


def test_explicit_provider_estimate_is_labelled_and_never_overrides_evidence(catalog):
    policy = PricingPolicy(assume_provider="anthropic")
    result = calculate(inputs(provider="unknown"), catalog, policy)
    assert result["effective_cost_nanos"] == 1_620_000
    assert "ESTIMATED_ASSUMED_PROVIDER_ANTHROPIC" in result["estimates"]
    assert calculate(inputs(provider="bedrock"), catalog, policy)["effective_cost_nanos"] is None


def test_unavailable_geography_is_unknown_not_an_unsupported_region(catalog):
    value = inputs(inference_geo="not_available")
    result = calculate(value, catalog)
    assert result["status"] == "COMPLETE"
    assert result["effective_cost_nanos"] == 1_620_000
    assert "ESTIMATED_UNAVAILABLE_INFERENCE_GEO" in result["estimates"]
    assert value["inference_geo"] == "not_available"  # original evidence remains unchanged
    old = calculate(value, catalog, formula_version="claude-cost/1")
    assert old["status"] == "UNPRICED" and "unsupported_inference_geo" in old["missing"]
    assert (
        calculate(inputs(provider="unknown", inference_geo="not_available"), catalog)["status"]
        == "UNPRICED"
    )
    with pytest.raises(ValueError, match="formula"):
        calculate(value, catalog, formula_version="unknown")


def test_exact_provider_prefix_alias_only(catalog):
    result = calculate(inputs(model_raw="anthropic/claude-sonnet-4-6"), catalog)
    assert result["price_key"] == "claude-sonnet-4-6"
    assert result["resolution_method"] == "explicit_provider_prefix"
    assert calculate(inputs(model_raw="some/claude-sonnet-4-6"), catalog)["status"] == "UNPRICED"


def test_fast_and_us_multipliers_are_catalog_values_and_stack(catalog):
    base = calculate(inputs(model_raw="claude-opus-4-8"), catalog)
    fast_geo = calculate(
        inputs(model_raw="claude-opus-4-8", speed="fast", inference_geo="us"), catalog
    )
    assert fast_geo["effective_cost_nanos"] == base["effective_cost_nanos"] * 22 // 10
    assert fast_geo["rules"]["fast_multiplier"] == "2.0"
    assert fast_geo["rules"]["geo_multiplier"] == "1.1"
    assert recompute_nanos(fast_geo) == fast_geo["effective_cost_nanos"]


def test_fast_batch_combination_is_not_silently_discounted(catalog):
    result = calculate(
        inputs(model_raw="claude-opus-4-8", speed="fast", service_tier="batch"), catalog
    )
    assert result["status"] == "UNPRICED"


def test_batch_uses_alternate_rates_and_missing_one_hour_rate_is_partial(catalog):
    result = calculate(inputs(service_tier="batch"), catalog)
    assert result["status"] == "PARTIAL"
    assert result["calculated_cost_nanos"] == (1_620_000 - 360_000) // 2
    assert result["coverage"]["unpriced_tokens"] == 60
    assert result["rates"]["input_uncached_tokens"]["catalog_field"].endswith("_batches")


@pytest.mark.parametrize(
    "count,high,expected", [(200_000, False, 600_015_000), (200_001, True, 1_200_028_500)]
)
def test_context_boundary_prices_entire_request_not_only_excess(catalog, count, high, expected):
    result = calculate(
        inputs(
            model_raw="claude-sonnet-4-5",
            input_uncached_tokens=count,
            cache_read_tokens=0,
            cache_write_5m_tokens=0,
            cache_write_1h_tokens=0,
            cache_write_unknown_tokens=0,
            output_tokens=1,
        ),
        catalog,
    )
    assert result["rules"]["above_threshold"] is high
    assert result["calculated_cost_nanos"] == expected


def test_context_basis_includes_all_cache_buckets(catalog):
    result = calculate(
        inputs(model_raw="claude-sonnet-4-5", input_uncached_tokens=1, cache_read_tokens=200_000),
        catalog,
    )
    assert result["rules"]["above_threshold"]
    assert result["rates"]["cache_write_1h_tokens"]["usd_per_unit"] == "0.000012"
    value = inputs(model_raw="claude-sonnet-4-5", cache_read_tokens=None)
    assert calculate(value, catalog)["status"] == "UNPRICED"


def test_current_model_without_premium_does_not_inherit_old_context_rule(catalog):
    result = calculate(inputs(input_uncached_tokens=250_000), catalog)
    assert result["rules"]["threshold_tokens"] is None
    assert result["rates"]["input_uncached_tokens"]["usd_per_unit"] == "0.000003"


def test_legacy_ttl_unknown_default_partial_explicit_estimate_complete(catalog):
    value = inputs(cache_write_5m_tokens=0, cache_write_1h_tokens=0, cache_write_unknown_tokens=100)
    partial = calculate(value, catalog)
    estimated = calculate(value, catalog, PricingPolicy(assume_cache_5m=True))
    assert partial["status"] == "PARTIAL" and partial["coverage"]["unpriced_tokens"] == 100
    assert estimated["status"] == "COMPLETE"
    assert estimated["calculated_cost_nanos"] - partial["calculated_cost_nanos"] == 375_000
    assert "ESTIMATED_ASSUMED_5M" in estimated["estimates"]


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "bedrock"},
        {"provider": "unknown", "model_raw": "claude-opus-not-in-catalog"},
        {"provider": "unknown", "speed": "unsupported"},
        {"provider": "unknown", "inference_geo": "unsupported"},
    ],
)
def test_auto_policy_does_not_fabricate_unsupported_rates(catalog, change):
    result = calculate(inputs(**change), catalog, AUTO_POLICY)
    assert result["status"] == "UNPRICED"
    assert result["effective_cost_nanos"] is None


def test_auto_policy_prefers_only_verified_request_scoped_cost(catalog):
    value = inputs(provider="unknown", reported_cost_nanos=0, reported_cost_unit="USD/request")
    result = calculate(value, catalog, AUTO_POLICY)
    assert result["effective_source"] == "reported" and result["effective_cost_nanos"] == 0
    assert result["calculated_cost_nanos"] == 1_620_000
    value["reported_cost_unit"] = "USD/session"
    result = calculate(value, catalog, AUTO_POLICY)
    assert result["effective_source"] == "calculated"
    assert "reported_cost_scope_not_verified" in result["warnings"]


def test_unknown_units_partial_and_client_tools_not_added(catalog):
    value = inputs()
    value["billable_units"] = {"unknown_unit": 2, "code_execution_requests": 1}
    result = calculate(value, catalog)
    assert result["status"] == "PARTIAL" and result["calculated_cost_nanos"] == 1_620_000
    assert result["coverage"]["unpriced_units"] == value["billable_units"]


def test_reasoning_is_not_added_to_output(catalog):
    assert calculate(inputs(reasoning_tokens=20), catalog)["calculated_cost_nanos"] == 1_620_000


def test_unknown_counter_vs_known_zero(catalog):
    zero = inputs(**dict.fromkeys(GOLDEN["cases"][0]["tokens"], 0))
    assert calculate(zero, catalog)["effective_cost_nanos"] == 0
    assert calculate(zero, catalog)["status"] == "COMPLETE"
    zero["tokens"]["input_uncached_tokens"] = None
    assert calculate(zero, catalog)["effective_cost_nanos"] is None
    assert calculate(zero, catalog)["status"] == "UNPRICED"
    zero["model_raw"] = "unknown-model"
    zero["tokens"]["input_uncached_tokens"] = 0
    assert calculate(zero, catalog)["effective_cost_nanos"] is None


def test_reported_cost_requires_request_scope_and_discrepancy_is_visible(catalog):
    result = calculate(
        inputs(reported_cost_nanos=2_000_000, reported_cost_unit="USD/request"), catalog
    )
    assert result["calculated_cost_nanos"] == 1_620_000
    assert result["effective_cost_nanos"] == 2_000_000 and result["effective_source"] == "reported"
    assert result["warnings"] == ["reported_calculated_discrepancy"]
    cumulative = calculate(
        inputs(reported_cost_nanos=2_000_000, reported_cost_unit="USD/session"), catalog
    )
    assert cumulative["effective_cost_nanos"] == 1_620_000
    zero = calculate(
        inputs(provider="unknown", reported_cost_nanos=0, reported_cost_unit="USD/request"), catalog
    )
    assert zero["effective_cost_nanos"] == 0 and zero["status"] == "COMPLETE"


def test_decimal_precision_rounds_once_at_request_boundary(catalog):
    altered = copy.deepcopy(catalog.models["claude-sonnet-4-6"])
    altered["input_cost_per_token"] = Decimal("0.00000000025")
    altered["output_cost_per_token"] = Decimal("0.00000000025")
    fixture = Catalog(catalog.metadata, {"claude-sonnet-4-6": altered})
    value = inputs(
        input_uncached_tokens=3,
        output_tokens=3,
        cache_read_tokens=0,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
        cache_write_unknown_tokens=0,
    )
    result = calculate(value, fixture)
    assert result["calculated_cost_nanos"] == 2  # 1.5 nano total, half-even once
    assert result["calculated_usd_exact"] == "1.50E-9"
    assert "ROUNDED_TO_NANODOLLAR_HALF_EVEN" in result["estimates"]
    assert recompute_nanos(result) == 2
    assert canonical(result) == canonical(calculate(value, fixture))


def test_cost_overflow_is_rejected_not_wrapped(catalog):
    with pytest.raises(ValueError, match="signed-64-bit"):
        calculate(inputs(input_uncached_tokens=2**63 - 1), catalog)


@pytest.mark.parametrize("bad", [True, -1, 1.5])
def test_invalid_reported_values_rejected(catalog, bad):
    with pytest.raises(ValueError):
        calculate(inputs(reported_cost_nanos=bad), catalog)
