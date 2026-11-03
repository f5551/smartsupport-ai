"""Validated phase-4 configuration. No secrets are loaded at import time."""

from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path

from dotenv import load_dotenv
from cost_tracker import DEFAULT_PRICING, Price, load_price

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class AgentConfig:
    model: str
    max_turns: int = 4
    budget_usd: Decimal = Decimal("0.005")
    permission_policy: str = "ask_on_write"
    max_output_tokens: int = 350
    timeout_seconds: float = 30.0
    max_retries: int = 0
    max_input_tokens: int = 3000
    max_tool_calls: int = 3
    max_history_messages: int = 6
    max_tool_result_chars: int = 2000
    max_daily_cost_usd: Decimal = Decimal("0.10")
    daily_cost_path: Path | None = None
    input_price: Decimal | None = None
    output_price: Decimal | None = None
    cached_input_price: Decimal | None = None
    pricing: Price | None = None
    service_tier: str = "default"
    usage_source: str = "unverified"
    audit_path: Path | None = None

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("OPENAI_MODEL must be selected explicitly")
        if type(self.max_turns) is not int or not 1 <= self.max_turns <= 4:
            raise ValueError("MAX_AGENT_TURNS must be 1..4")
        if not isinstance(self.budget_usd, Decimal) or not self.budget_usd.is_finite() or not Decimal(0) < self.budget_usd <= Decimal("0.005"):
            raise ValueError("MAX_TASK_COST_USD must be positive and at most 0.005")
        if self.permission_policy != "ask_on_write":
            raise ValueError("Only ask_on_write is supported")
        if type(self.max_output_tokens) is not int or not 16 <= self.max_output_tokens <= 350:
            raise ValueError("MAX_OUTPUT_TOKENS must be 16..350")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("OPENAI_TIMEOUT_SECONDS must be positive and at most 60")
        if type(self.max_retries) is not int or self.max_retries != 0:
            raise ValueError("MAX_API_RETRIES must be 0")
        for name, maximum, minimum in (("max_input_tokens",3000,1), ("max_tool_calls",3,1),
                                       ("max_history_messages",6,1), ("max_tool_result_chars",2000,256)):
            if type(getattr(self,name)) is not int or not minimum <= getattr(self,name) <= maximum:
                raise ValueError(f"Invalid {name}")
        if not isinstance(self.max_daily_cost_usd, Decimal) or not self.max_daily_cost_usd.is_finite() or not Decimal(0) < self.max_daily_cost_usd <= Decimal("0.10"):
            raise ValueError("MAX_DAILY_COST_USD must be positive and at most 0.10")
        for price in (self.input_price, self.output_price):
            if price is not None and (not isinstance(price, Decimal) or not price.is_finite() or price <= 0):
                raise ValueError("Token prices must be positive finite Decimal values")
        if self.service_tier not in {"default", "flex", "priority"}:
            raise ValueError("Unsupported Responses service tier")
        if self.usage_source not in {"api", "simulation", "unverified"}:
            raise ValueError("Invalid usage source")
        if self.pricing is not None and (self.model not in (self.pricing.model, *self.pricing.aliases) or self.service_tier != self.pricing.tier):
            raise ValueError("Configuration does not match price model/tier")

    @classmethod
    def from_env(cls):
        # Existing process environment wins; never return/repr API credentials.
        load_dotenv(ROOT / ".env", override=False)
        def project_path(value):
            path = Path(value)
            return path if path.is_absolute() else ROOT / path
        def limit_value(current, legacy, default, maximum, parse):
            if current in os.environ:
                return parse(os.environ[current])
            return min(parse(os.environ.get(legacy, str(default))), maximum)
        model = os.environ.get("OPENAI_MODEL", "")
        tier = os.environ.get("OPENAI_SERVICE_TIER", "default")
        # Live setup must use a versioned pricing file, not silent inline fallback.
        pricing = load_price(model, project_path(os.environ.get("OPENAI_PRICING_FILE", str(DEFAULT_PRICING))), tier=tier)
        return cls(
            model=model, pricing=pricing, service_tier=tier, usage_source="api",
            audit_path=project_path(os.environ.get("AUDIT_LOG_PATH", str(ROOT / "logs" / "audit.jsonl"))),
            max_turns=limit_value("MAX_AGENT_TURNS", "MAX_TURNS", 4, 4, int),
            budget_usd=limit_value("MAX_TASK_COST_USD", "BUDGET_USD", Decimal("0.005"), Decimal("0.005"), Decimal),
            permission_policy=os.environ.get("PERMISSION_POLICY", "ask_on_write"),
            max_output_tokens=min(int(os.environ.get("MAX_OUTPUT_TOKENS", "350")),350),
            timeout_seconds=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "30")),
            max_retries=int(os.environ.get("MAX_API_RETRIES", "0")),
            max_input_tokens=int(os.environ.get("MAX_INPUT_TOKENS","3000")),
            max_tool_calls=int(os.environ.get("MAX_TOOL_CALLS","3")),
            max_history_messages=int(os.environ.get("MAX_HISTORY_MESSAGES","6")),
            max_tool_result_chars=int(os.environ.get("MAX_TOOL_RESULT_CHARS","2000")),
            max_daily_cost_usd=Decimal(os.environ.get("MAX_DAILY_COST_USD","0.10")),
            daily_cost_path=project_path(os.environ.get("DAILY_COST_DB_PATH", "data/daily_api_cost.sqlite3")),
            input_price=pricing.input, cached_input_price=pricing.cached_input, output_price=pricing.output,
        )
