"""Server-counted admission for the isolated ticket retest; no I/O on import.

The generation bound assumes the documented count/output contract and configured
standard USD rates. It is not an account billing limit or a guarantee of service.
"""
import asyncio
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

from cost_tracker import CostTracker

INPUT_LIMIT = 2500
OUTPUT_LIMIT = 96
REQUEST_LIMIT = 2
TASK_LIMIT = Decimal("0.005")
MODEL = "gpt-5.4-mini-2026-03-17"
COUNT_FIELDS = {"model", "instructions", "input", "tools", "tool_choice", "reasoning",
                "parallel_tool_calls", "truncation"}
GENERATE_FIELDS = COUNT_FIELDS | {"max_output_tokens", "service_tier", "include", "store"}


class BudgetBlocked(ValueError):
    pass


def generation_bound(price, input_limit=INPUT_LIMIT, output_limit=OUTPUT_LIMIT):
    # Only the reviewed standard text price is supported by this test harness.
    if (price is None or price.model != MODEL or price.provider != "OpenAI"
            or price.tier != "default" or not price.verified_at
            or price.input != Decimal("0.75") or price.output != Decimal("4.50")
            or price.multiplier != 1 or price.fixed_request_usd != 0
            or price.cache_write_surcharge is not None):
        raise BudgetBlocked("Unsupported test pricing")
    if (type(input_limit) is not int or not 1 <= input_limit <= INPUT_LIMIT
            or type(output_limit) is not int or not 16 <= output_limit <= OUTPUT_LIMIT):
        raise BudgetBlocked("Unsupported test limits")
    return REQUEST_LIMIT * CostTracker(price).estimate(input_limit, output_limit)


class BudgetedResponses:
    """Own exact daily reservations. The Agent must not reserve a second time.

    Count and generate use immutable copies of identical count-affecting fields.
    A failed/unknown paid attempt remains reserved and closes this client forever.
    """
    usage_source = "api"

    def __init__(self, client, price, ledger, *, task_limit=TASK_LIMIT,
                 daily_limit=Decimal("0.10"), input_limit=INPUT_LIMIT, output_limit=OUTPUT_LIMIT):
        self.bound = generation_bound(price, input_limit, output_limit)
        if (not Decimal(0) < task_limit <= TASK_LIMIT or self.bound > task_limit
                or not Decimal(0) < daily_limit <= Decimal("0.10")):
            raise BudgetBlocked("Budget cannot cover test envelope")
        self.client, self.ledger = client.with_options(max_retries=0), ledger
        self.task_limit, self.daily_limit = task_limit, daily_limit
        self.input_limit, self.output_limit = input_limit, output_limit
        self.tracker = CostTracker(price, source="api")
        self.calls = self.count_calls = 0
        self.halted = False
        self.responses = self
        self._prefix = uuid4().hex
        self._lock = asyncio.Lock()

    def with_options(self, **options):
        if options.get("max_retries", 0) != 0:
            raise BudgetBlocked("Retries prohibited")
        self.client = self.client.with_options(**{**options, "max_retries": 0})
        return self

    async def create(self, **kwargs):
        async with self._lock:
            if self.halted or self.calls >= REQUEST_LIMIT:
                raise BudgetBlocked("Closed test or request limit reached")
            payload = deepcopy(kwargs)
            try:
                if (set(payload) - GENERATE_FIELDS or payload.get("model") != MODEL
                        or payload.get("service_tier") != "default"
                        or payload.get("store") is not False
                        or payload.get("parallel_tool_calls") is not False
                        or type(payload.get("max_output_tokens")) is not int
                        or not 16 <= payload["max_output_tokens"] <= self.output_limit
                        or payload.get("reasoning") != {"effort": "none"}
                        or not isinstance(payload.get("tools"), list)
                        or len(payload["tools"]) != 1
                        or payload["tools"][0].get("type") != "function"
                        or payload["tools"][0].get("name") != "create_ticket"):
                    raise BudgetBlocked("Unsupported generation request")
                payload["truncation"] = "disabled"
                count_payload = {k: deepcopy(v) for k, v in payload.items() if k in COUNT_FIELDS}
                self.count_calls += 1
                count = await self.client.responses.input_tokens.count(**count_payload)
                tokens = getattr(count, "input_tokens", None)
                if type(tokens) is not int or not 0 <= tokens <= self.input_limit:
                    raise BudgetBlocked("Server input count exceeds limit or is unavailable")
                upper = self.tracker.estimate(tokens, payload["max_output_tokens"])
                if self.tracker.committed + upper > self.task_limit:
                    raise BudgetBlocked("Task budget admission refused")
                request_id = f"{self._prefix}:{self.calls + 1}"
                if not self.ledger.reserve(request_id, upper, self.daily_limit):
                    raise BudgetBlocked("Daily budget admission refused")
            except BaseException:
                self.halted = True
                raise
            self.calls += 1
            self.tracker.reserve(request_id, upper)
            try:
                response = await self.client.responses.create(**payload)
            except BaseException:
                self.halted = True
                self.tracker.record(request_id, None)
                self.ledger.settle(request_id, None)
                raise
            try:
                record = self.tracker.record(request_id, getattr(response, "usage", None),
                                             model=getattr(response, "model", None),
                                             service_tier=getattr(response, "service_tier", None))
                actual = record["cost_usd"]
                self.ledger.settle(request_id, Decimal(actual) if actual is not None else None)
                if (actual is None or getattr(response, "model", None) != MODEL
                        or getattr(response, "service_tier", None) != "default"
                        or record["input_tokens"] != tokens
                        or record["output_tokens"] > payload["max_output_tokens"]
                        or Decimal(actual) > upper):
                    raise BudgetBlocked("Usage unknown or provider contract mismatch; stop")
            except BaseException:
                self.halted = True
                raise
            return response
