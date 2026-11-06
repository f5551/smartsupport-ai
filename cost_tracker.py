"""Decimal usage accounting, explicit price provenance and conservative reserves.

Only text Responses with the six local tools are supported. Unknown models,
tiers or nonzero unpriced cache-write counters fail closed, never cost zero.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

MILLION = Decimal(1_000_000)
DEFAULT_PRICING = Path(__file__).resolve().parent / "pricing.json"


class PricingError(ValueError):
    pass


def _decimal(value, *, positive=False):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise PricingError("Invalid price")
    return result


@dataclass(frozen=True)
class Price:
    model: str
    input: Decimal
    cached_input: Decimal | None
    output: Decimal
    provider: str = "OpenAI"
    tier: str = "default"
    source_url: str | None = None
    verified_at: str | None = None
    aliases: tuple = ()
    fixed_request_usd: Decimal = Decimal(0)
    multiplier: Decimal = Decimal(1)
    cache_write_surcharge: Decimal | None = None

    def __post_init__(self):
        for name in ("input", "output", "fixed_request_usd", "multiplier", "cached_input", "cache_write_surcharge"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, positive=name == "multiplier"))
        if self.cached_input is not None and self.cached_input > self.input:
            raise PricingError("Cached input price cannot exceed uncached input price")

    def metadata(self):
        return {"provider": self.provider, "model": self.model, "service_tier": self.tier,
                "input_price_per_million": str(self.input),
                "cached_price_per_million": str(self.cached_input) if self.cached_input is not None else None,
                "output_price_per_million": str(self.output), "verified_at": self.verified_at,
                "source_url": self.source_url, "fixed_request_usd": str(self.fixed_request_usd),
                "multiplier": str(self.multiplier),
                "cache_write_surcharge_per_million": str(self.cache_write_surcharge) if self.cache_write_surcharge is not None else None}


def load_price(model, path=DEFAULT_PRICING, *, tier="default") -> Price:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (data.get("schema_version"), data.get("currency"), data.get("unit")) != (1, "USD", "per_1000000_tokens"):
        raise PricingError("Unsupported pricing file")
    matches = [(name, entry) for name, entry in data["models"].items()
               if model == name or model in entry.get("aliases", [])]
    if len(matches) != 1:
        raise PricingError("Model price not configured unambiguously")
    name, entry = matches[0]
    if entry.get("provider") != "OpenAI" or tier not in entry["tiers"]:
        raise PricingError("Provider or service tier not configured")
    date.fromisoformat(entry["verified_at"])
    if not entry["source_url"].startswith("https://developers.openai.com/"):
        raise PricingError("Expected official price source")
    rate = entry["tiers"][tier]
    allowed = {"input", "cached_input", "output", "fixed_request_usd", "multiplier", "cache_write_surcharge"}
    if set(rate) - allowed:
        raise PricingError("Unsupported additional pricing rule")
    return Price(name, _decimal(rate["input"]), _decimal(rate["cached_input"]) if rate.get("cached_input") is not None else None,
                 _decimal(rate["output"]), tier=tier, source_url=entry["source_url"], verified_at=entry["verified_at"],
                 aliases=tuple(entry.get("aliases", [])), fixed_request_usd=_decimal(rate.get("fixed_request_usd", 0)),
                 multiplier=_decimal(rate.get("multiplier", 1), positive=True),
                 cache_write_surcharge=_decimal(rate["cache_write_surcharge"]) if rate.get("cache_write_surcharge") is not None else None)


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def parse_usage(usage):
    if usage is None:
        return None
    inp, out = _field(usage, "input_tokens"), _field(usage, "output_tokens")
    details = _field(usage, "input_tokens_details")
    cached = _field(details, "cached_tokens")
    writes = _field(details, "cache_write_tokens", 0)
    if writes is None:
        writes = 0
    values = (inp, out, writes)
    if any(type(n) is not int or n < 0 for n in values):
        return None
    if cached is not None and (type(cached) is not int or not 0 <= cached <= inp):
        return None
    return {"input_tokens": inp, "output_tokens": out, "cached_tokens": cached, "cache_write_tokens": writes}


class CostTracker:
    def __init__(self, price: Price | None, *, source="unverified"):
        if source not in {"api", "simulation", "unverified"}:
            raise ValueError("Invalid usage source")
        self.price, self.source = price, source
        self.records: list[dict[str, Any]] = []
        self.reservations: dict[str, Decimal] = {}
        self.input_tokens = self.output_tokens = 0

    @property
    def known_cost(self):
        return sum((Decimal(r["cost_usd"]) for r in self.records if r["cost_usd"] is not None), Decimal(0))

    @property
    def committed(self):
        return self.known_cost + sum(self.reservations.values(), Decimal(0))

    def estimate(self, input_token_estimate, max_output_tokens):
        if self.price is None:
            return None
        p = self.price
        # No cache discount is assumed. Special cache-write surcharge, if
        # configured, is reserved conservatively on all estimated input tokens.
        return ((input_token_estimate * (p.input + (p.cache_write_surcharge or 0))
                 + max_output_tokens * p.output) / MILLION + p.fixed_request_usd) * p.multiplier

    def reserve(self, request_id, amount):
        if request_id in self.reservations or any(r["request_id"] == request_id for r in self.records):
            raise ValueError("Request already tracked")
        self.reservations[request_id] = _decimal(amount)

    def record(self, request_id, usage, *, model=None, service_tier=None):
        if request_id not in self.reservations or any(r["request_id"] == request_id for r in self.records):
            raise ValueError("Missing or already settled reservation")
        counters = parse_usage(usage)
        p = self.price
        reason, amount = None, None
        if counters is None:
            reason = "USAGE_UNAVAILABLE"
        else:
            self.input_tokens += counters["input_tokens"]
            self.output_tokens += counters["output_tokens"]
            if p is None:
                reason = "PRICE_UNAVAILABLE"
            elif model is not None and model not in (p.model, *p.aliases):
                reason = "MODEL_PRICE_MISMATCH"
            elif service_tier is not None and service_tier != p.tier:
                reason = "TIER_PRICE_MISMATCH"
            elif counters["cache_write_tokens"] and p.cache_write_surcharge is None:
                reason = "UNPRICED_CACHE_WRITE"
            elif counters["cached_tokens"] is None or (counters["cached_tokens"] and p.cached_input is None):
                # Missing cache details: retain reservation, do not invent exact cost.
                reason = "CACHE_USAGE_UNAVAILABLE"
            else:
                cached = counters["cached_tokens"]
                amount = (((counters["input_tokens"] - cached) * p.input + cached * (p.cached_input or 0)
                           + counters["output_tokens"] * p.output
                           + counters["cache_write_tokens"] * (p.cache_write_surcharge or 0)) / MILLION
                          + p.fixed_request_usd) * p.multiplier
                self.reservations.pop(request_id)
        record = {"request_id": request_id, **(counters or {"input_tokens": None, "output_tokens": None, "cached_tokens": None}),
                  "cost_usd": str(amount) if amount is not None else None, "usage_source": self.source,
                  "error_code": reason, "cost_kind": {"api": "api_usage", "simulation": "simulated_usage", "unverified": "unverified_usage"}[self.source]
                  if amount is not None else "unknown"}
        self.records.append(record)
        return record

    def snapshot(self):
        complete = not self.reservations
        return {"provider": "OpenAI", "usage_source": self.source,
                "pricing": self.price.metadata() if self.price else None,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "known_cost_usd": str(self.known_cost),
                "total_cost_usd": str(self.known_cost) if complete else None,
                "api_measured_cost_usd": str(self.known_cost) if complete and self.source == "api" else None,
                "simulated_cost_usd": str(self.known_cost) if complete and self.source == "simulation" else None,
                "estimated_cost_usd": str(self.committed),
                "unresolved_reservations": len(self.reservations), "usage_complete": complete,
                "cost_kind": "unknown" if not complete else {"api": "api_usage", "simulation": "simulated_usage", "unverified": "unverified_usage"}[self.source]}
