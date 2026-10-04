"""Pure, offline request pricing. Catalog values are Decimal USD per native unit."""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from claude_metrics.domain import TOKEN_COLUMNS, TokenUsage, counter
from claude_metrics.pricing.catalog import Catalog
from claude_metrics.pricing.policy import AUTO_POLICY as AUTO_POLICY
from claude_metrics.pricing.policy import PricingPolicy as PricingPolicy
from claude_metrics.pricing.policy import canonical as canonical

AGENT_TYPE = "claude"
FORMULA_VERSION = "claude-cost/2"
SUPPORTED_FORMULAS = {"claude-cost/1", FORMULA_VERSION}
FIELDS = {
    "input_uncached_tokens": "input_cost_per_token",
    "cache_read_tokens": "cache_read_input_token_cost",
    "cache_write_5m_tokens": "cache_creation_input_token_cost",
    "cache_write_1h_tokens": "cache_creation_input_token_cost_above_1hr",
    "cache_write_unknown_tokens": "cache_creation_input_token_cost",
    "output_tokens": "output_cost_per_token",
}
# Prefix normalization only. No guessed family/date aliases or fuzzy matches.
PROVIDER_PREFIXES = {"anthropic": "anthropic/"}


def pricing_input(request: dict, units: dict[str, int]) -> dict:
    """Allowlist only; independent of mutable database rows and raw transcript content."""
    result = {
        key: request.get(key)
        for key in (
            "model_raw",
            "provider",
            "provider_region",
            "inference_geo",
            "speed",
            "service_tier",
            "occurred_at_us",
            "reported_cost_nanos",
            "reported_cost_original",
            "reported_cost_unit",
        )
    }
    result["tokens"] = {key: request.get(key) for key in (*TOKEN_COLUMNS, "reasoning_tokens")}
    result["billable_units"] = dict(sorted(units.items()))
    return result


def rate(value) -> Decimal | None:
    if type(value) not in (int, Decimal):
        return None
    number = Decimal(value)
    return number if number.is_finite() and number >= 0 else None


def resolve(inputs: dict, catalog: Catalog, policy: PricingPolicy) -> tuple:
    provider = inputs.get("provider") or "unknown"
    estimates = []
    if provider == "unknown" and policy.assume_provider:
        provider = policy.assume_provider
        estimates.append("ESTIMATED_ASSUMED_PROVIDER_ANTHROPIC")
    if provider != "anthropic":
        return None, None, "unresolved", estimates, "unknown_or_unsupported_provider"
    if inputs.get("provider_region") not in (None, "global"):
        return None, None, "unresolved", estimates, "unsupported_provider_region"
    raw = inputs["model_raw"]
    # Verify provider even on an exact raw-key match. Cloud/gateway models cannot fall through.
    for key, method in ((f"anthropic/{raw}", "provider_qualified"), (raw, "exact")):
        entry = catalog.models.get(key)
        if isinstance(entry, dict) and entry.get("litellm_provider") == provider:
            return key, entry, method, estimates, None
    prefix = PROVIDER_PREFIXES[provider]
    if raw.startswith(prefix):
        key = raw.removeprefix(prefix)
        entry = catalog.models.get(key)
        if isinstance(entry, dict) and entry.get("litellm_provider") == provider:
            return key, entry, "explicit_provider_prefix", estimates, None
    return None, None, "unresolved", estimates, "unknown_model"


def _token_rules(
    inputs: dict, usage: TokenUsage, entry: dict, formula_version: str
) -> tuple[dict, list, list]:
    """Select whole-request tiers before pricing. Missing alternate fields stay missing."""
    rules, errors, estimates = {}, [], []
    speed = inputs.get("speed")
    tier = inputs.get("service_tier")
    geo = inputs.get("inference_geo")
    if formula_version != "claude-cost/1" and geo == "not_available":
        # This is unavailable geography, not a region named "not_available".
        # Retain the original input in the receipt and label standard-rate estimation.
        geo = None
        estimates.append("ESTIMATED_UNAVAILABLE_INFERENCE_GEO")
    modifiers = entry.get("provider_specific_entry", {})
    if not isinstance(modifiers, dict):
        modifiers = {}
    multiplier = Decimal(1)
    if speed not in (None, "standard", "fast"):
        errors.append("unsupported_speed")
    if speed == "fast":
        fast = rate(modifiers.get("fast"))
        if fast is None:
            errors.append("missing_fast_rate")
        else:
            multiplier *= fast
            rules["fast_multiplier"] = str(fast)
    if tier not in (None, "standard", "batch"):
        errors.append("unsupported_service_tier")
    if tier == "batch" and speed == "fast":
        errors.append("unsupported_fast_batch_combination")
    if geo not in (None, "global"):
        geo_rate = rate(modifiers.get(geo)) if geo == "us" else None
        if geo_rate is None:
            errors.append("unsupported_inference_geo")
        else:
            multiplier *= geo_rate
            rules["geo_multiplier"] = str(geo_rate)
    if speed is None or tier is None or geo is None:
        estimates.append("ESTIMATED_UNOBSERVED_STANDARD_MODIFIERS")
    tiered = any("above_200k_tokens" in field for field in entry)
    high = False
    if tiered:
        if usage.input_total is None:
            errors.append("unknown_context_threshold_basis")
        else:
            high = usage.input_total > 200_000
    rules.update(
        {
            "context_input_tokens": usage.input_total,
            "threshold_tokens": 200_000 if tiered else None,
            "above_threshold": high,
            "threshold_operator": ">",
            "scope": "entire_request",
            "service_tier": tier or "standard",
            "token_multiplier": str(multiplier),
            "modifier_order": ["tier", "fast", "geo"],
        }
    )
    selected = {}
    for bucket, field in FIELDS.items():
        selected[bucket] = (
            field + ("_above_200k_tokens" if high else "") + ("_batches" if tier == "batch" else "")
        )
    rules["catalog_fields"] = selected
    return rules, errors, estimates


def calculate(
    inputs: dict,
    catalog: Catalog,
    policy: PricingPolicy | None = None,
    *,
    formula_version: str = FORMULA_VERSION,
) -> dict:
    """Return a JSON-safe receipt payload; only round once, at the request boundary."""
    policy = PricingPolicy() if policy is None else policy
    if formula_version not in SUPPORTED_FORMULAS:
        raise ValueError("unsupported cost formula")
    usage = TokenUsage(**inputs["tokens"])
    for unit, quantity in inputs["billable_units"].items():
        if not isinstance(unit, str) or not unit:
            raise ValueError("invalid billable unit type")
        counter(quantity, "unit_quantity")
        if quantity is None:
            raise ValueError("unit quantity must be known")
    reported = inputs.get("reported_cost_nanos")
    counter(reported, "reported_cost_nanos")
    key, entry, method, estimates, resolution_error = resolve(inputs, catalog, policy)
    rates, components, missing, rules = {}, [], [], {}
    known_tokens = sum(inputs["tokens"][k] or 0 for k in TOKEN_COLUMNS)
    priced_tokens, unknown_counters = 0, []
    unpriced_units = {}
    with localcontext() as context:
        context.prec = 80  # > int64 quantities multiplied by all bundled catalog rate digits
        token_errors = []
        if entry:
            rules, token_errors, mode_estimates = _token_rules(
                inputs, usage, entry, formula_version
            )
            estimates.extend(mode_estimates)
            estimates.append("ESTIMATED_CURRENT_CATALOG")  # no effective-dated schedule in v1
        if resolution_error:
            missing.append(resolution_error)
        missing.extend(token_errors)
        for bucket in TOKEN_COLUMNS:
            quantity = inputs["tokens"][bucket]
            if quantity is None:
                unknown_counters.append(bucket)
                missing.append(f"unknown_counter:{bucket}")
                continue
            if quantity == 0:
                continue  # known zero needs no rate, unlike a missing counter
            field = rules.get("catalog_fields", {}).get(bucket)
            selected = rate(entry.get(field)) if entry and field and not token_errors else None
            if bucket == "cache_write_unknown_tokens":
                if not policy.assume_cache_5m:
                    selected = None
                    missing.append("unknown_cache_ttl")
                elif selected is not None:
                    estimates.append("ESTIMATED_ASSUMED_5M")
            if selected is None:
                missing.append(f"unpriced_bucket:{bucket}")
                continue
            effective = selected * Decimal(rules["token_multiplier"])
            rates[bucket] = {"catalog_field": field, "usd_per_unit": str(effective), "basis": 1}
            exact = effective * quantity
            components.append(
                {"kind": "token", "name": bucket, "quantity": quantity, "usd_exact": str(exact)}
            )
            priced_tokens += quantity
        for unit, quantity in inputs["billable_units"].items():
            if quantity == 0:
                continue
            selected = None
            if entry and unit == "web_search_requests":
                search = entry.get("search_context_cost_per_query", {})
                # No observed search-context size: require an equal complete rate card.
                values = (
                    [
                        rate(search.get(f"search_context_size_{size}"))
                        for size in ("low", "medium", "high")
                    ]
                    if isinstance(search, dict)
                    else []
                )
                if len(values) == 3 and None not in values and len(set(values)) == 1:
                    selected = values[0]
            if selected is None:
                missing.append(f"unpriced_unit:{unit}")
                unpriced_units[unit] = quantity
                continue
            rates[unit] = {
                "catalog_field": "search_context_cost_per_query",
                "usd_per_unit": str(selected),
                "basis": 1,
            }
            components.append(
                {
                    "kind": "unit",
                    "name": unit,
                    "quantity": quantity,
                    "usd_exact": str(selected * quantity),
                }
            )
        # A known model with complete zero usage is genuinely zero; unknown models are not.
        calculable = bool(components) or (entry is not None and not missing)
        exact_total = sum((Decimal(c["usd_exact"]) for c in components), Decimal(0))
        calculated = (
            int((exact_total * 1_000_000_000).to_integral_value(rounding=ROUND_HALF_EVEN))
            if calculable
            else None
        )
        counter(calculated, "calculated_cost_nanos")
        if calculated is not None and Decimal(calculated) != exact_total * 1_000_000_000:
            estimates.append("ROUNDED_TO_NANODOLLAR_HALF_EVEN")
    status = "UNPRICED" if calculated is None else "PARTIAL" if missing else "COMPLETE"
    reported_accepted = reported is not None and inputs.get("reported_cost_unit") == "USD/request"
    warnings = []
    if reported is not None and not reported_accepted:
        warnings.append("reported_cost_scope_not_verified")
    if reported_accepted and calculated is not None and status == "COMPLETE":
        if abs(reported - calculated) > policy.discrepancy_tolerance_nanos:
            warnings.append("reported_calculated_discrepancy")
    effective = reported if reported_accepted else calculated
    effective_source = "reported" if reported_accepted else "calculated" if calculable else None
    if reported_accepted:
        status = "COMPLETE"
    return {
        "price_key": key,
        "resolution_method": method,
        "rates": rates,
        "rules": rules,
        "calculated_cost_nanos": calculated,
        "reported_cost_nanos": reported,
        "effective_cost_nanos": effective,
        "effective_source": effective_source,
        "status": status,
        "estimates": sorted(set(estimates)),
        "missing": sorted(set(missing)),
        "warnings": warnings,
        "components": components,
        "calculated_usd_exact": str(exact_total) if calculable else None,
        "coverage": {
            "known_tokens": known_tokens,
            "priced_tokens": priced_tokens,
            "unpriced_tokens": known_tokens - priced_tokens,
            "unknown_counters": unknown_counters,
            "unpriced_units": unpriced_units,
            "reported_scope_complete": reported_accepted,
        },
        "rounding": "sum exact USD components; half-even once to integer nanodollars",
    }


def recompute_nanos(payload: dict) -> int | None:
    """Independently recompute a receipt's calculation using its frozen rates and quantities."""
    if payload["calculated_cost_nanos"] is None:
        return None
    with localcontext() as context:
        context.prec = 80
        total = sum(
            (
                Decimal(payload["rates"][c["name"]]["usd_per_unit"])
                * c["quantity"]
                / payload["rates"][c["name"]]["basis"]
                for c in payload["components"]
            ),
            Decimal(0),
        )
        result = int((total * 1_000_000_000).to_integral_value(rounding=ROUND_HALF_EVEN))
        counter(result, "cost_nanos")
        return result
