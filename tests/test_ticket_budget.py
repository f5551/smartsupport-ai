"""No network, SQLite, or ticket writes: test admission against scripted SDKs."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from cost_tracker import load_price
from fake_openai import fake_response
from ticket_budget import BudgetBlocked, BudgetedResponses, MODEL, generation_bound


class Ledger:
    def __init__(self, existing=Decimal("0.002445")):
        self.existing, self.rows = existing, {}

    def reserve(self, key, amount, limit):
        if self.existing + sum(v[0] for v in self.rows.values()) + amount > limit:
            return False
        self.rows[key] = (amount, "reserved")
        return True

    def settle(self, key, actual):
        reserve, _ = self.rows[key]
        self.rows[key] = (actual if actual is not None else reserve,
                          "settled" if actual is not None else "unknown")


class SDK:
    def __init__(self, count=2500, response=None, error=None):
        self.responses = self
        self.input_tokens = SimpleNamespace(count=self.count)
        self.count_value, self.response, self.error = count, response, error
        self.requests, self.counts, self.options = [], [], {}

    def with_options(self, **options):
        self.options.update(options)
        return self

    async def count(self, **kwargs):
        self.counts.append(deepcopy(kwargs))
        if isinstance(self.count_value, Exception):
            raise self.count_value
        return SimpleNamespace(input_tokens=self.count_value)

    async def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        if self.error:
            raise self.error
        return self.response or fake_response("ok", input_tokens=self.count_value, output_tokens=96,
                                             model=MODEL).model_copy(update={"service_tier": "default"})


def payload(**overrides):
    return {"model": MODEL, "input": [{"role": "user", "content": "تذكرة تجريبية"}],
            "instructions": "Test", "tools": [{"type": "function", "name": "create_ticket"}],
            "tool_choice": "none", "reasoning": {"effort": "none"}, "store": False,
            "service_tier": "default", "max_output_tokens": 96, "parallel_tool_calls": False,
            **overrides}


def guard(sdk=None, ledger=None):
    return BudgetedResponses(sdk or SDK(), load_price(MODEL), ledger or Ledger())


def test_maximum_two_request_generation_cost_and_original_unknown():
    async def scenario():
        ledger, sdk = Ledger(), SDK()
        client = guard(sdk, ledger)
        original = payload()
        for _ in range(2):
            await client.create(**original)
        assert client.bound == generation_bound(load_price(MODEL)) == Decimal("0.004614")
        assert client.tracker.known_cost == client.bound
        assert ledger.existing == Decimal("0.002445")
        assert sum(v[0] for v in ledger.rows.values()) == client.bound
        assert all(v[1] == "settled" for v in ledger.rows.values())
        assert sdk.options["max_retries"] == 0
        with pytest.raises(BudgetBlocked):
            await client.create(**original)
        assert len(sdk.requests) == len(sdk.counts) == 2
        assert "truncation" not in original
        for counted, sent in zip(sdk.counts, sdk.requests):
            assert all(sent[k] == v for k, v in counted.items())
            assert counted["truncation"] == sent["truncation"] == "disabled"
    asyncio.run(scenario())


@pytest.mark.parametrize("tokens", [2501, -1, True, None, "2500", OSError("private-detail")])
def test_bad_or_failed_count_never_sends_or_reserves(tokens):
    async def scenario():
        sdk, ledger = SDK(count=tokens), Ledger()
        client = guard(sdk, ledger)
        with pytest.raises((BudgetBlocked, OSError)):
            await client.create(**payload())
        with pytest.raises(BudgetBlocked):
            await client.create(**payload())
        assert client.halted and not sdk.requests and not ledger.rows
        assert len(sdk.counts) == 1
    asyncio.run(scenario())


def test_unknown_paid_outcome_retains_reservation_and_never_retries():
    async def scenario():
        sdk, ledger = SDK(error=TimeoutError("private-detail")), Ledger()
        client = guard(sdk, ledger)
        with pytest.raises(TimeoutError):
            await client.create(**payload())
        with pytest.raises(BudgetBlocked):
            await client.create(**payload())
        assert len(sdk.requests) == len(sdk.counts) == 1
        assert list(ledger.rows.values()) == [(Decimal("0.002307"), "unknown")]
        assert ledger.existing == Decimal("0.002445")
    asyncio.run(scenario())


def test_daily_budget_refusal_includes_existing_unknown():
    async def scenario():
        sdk, ledger = SDK(), Ledger(existing=Decimal("0.099"))
        client = guard(sdk, ledger)
        with pytest.raises(BudgetBlocked):
            await client.create(**payload())
        assert not sdk.requests and not ledger.rows
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"max_output_tokens": 97}, {"max_output_tokens": True}, {"model": "gpt-5.4-mini"},
    {"service_tier": "priority"}, {"previous_response_id": "hidden-context"},
    {"conversation": "hidden-context"}, {"tools": [{"type": "web_search"}]},
    {"reasoning": {"effort": "high"}},
])
def test_unpriced_or_unbounded_request_is_rejected_before_count(overrides):
    async def scenario():
        sdk = SDK()
        client = guard(sdk)
        with pytest.raises(BudgetBlocked):
            await client.create(**payload(**overrides))
        assert not sdk.counts and not sdk.requests
    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    fake_response(usage=False, model=MODEL),
    fake_response(input_tokens=2499, model=MODEL),
    fake_response(input_tokens=2500, output_tokens=97, model=MODEL),
    fake_response(input_tokens=2500, model="wrong-model"),
])
def test_usage_missing_or_contract_mismatch_stops_subsequent_calls(response):
    async def scenario():
        client = guard(SDK(response=response.model_copy(update={"service_tier": "default"})))
        with pytest.raises(BudgetBlocked):
            await client.create(**payload())
        assert client.halted
        with pytest.raises(BudgetBlocked):
            await client.create(**payload())
        assert client.calls == 1
    asyncio.run(scenario())


def test_concurrent_attempts_cannot_exceed_two_generation_requests():
    async def scenario():
        sdk = SDK()
        client = guard(sdk)
        outcomes = await asyncio.gather(*(client.create(**payload()) for _ in range(3)), return_exceptions=True)
        assert sum(isinstance(item, BudgetBlocked) for item in outcomes) == 1
        assert len(sdk.requests) == 2
        assert client.tracker.known_cost == Decimal("0.004614")
    asyncio.run(scenario())


def test_configuration_cannot_increase_price_budget_or_retries():
    price = load_price(MODEL)
    for bad_price in [replace(price, multiplier=Decimal("1.1")), replace(price, output=Decimal("5")),
                      replace(price, cache_write_surcharge=Decimal(".1"))]:
        with pytest.raises(BudgetBlocked):
            generation_bound(bad_price)
    for config in [{"task_limit": Decimal(".006")}, {"daily_limit": Decimal(".11")},
                   {"task_limit": Decimal(".004")}, {"input_limit": 3000}]:
        with pytest.raises(BudgetBlocked):
            BudgetedResponses(SDK(), price, Ledger(), **config)
    with pytest.raises(BudgetBlocked):
        guard().with_options(max_retries=1)


def test_installed_sdk_count_then_generate_contract_uses_http_mock_only():
    import httpx
    from openai import AsyncOpenAI

    async def scenario():
        requests = []
        def transport(request):
            import json
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 2500})
            response = fake_response("ok", input_tokens=2500, output_tokens=96, model=MODEL)
            return httpx.Response(200, json=response.model_copy(update={"service_tier": "default"}).model_dump(mode="json"))
        async with AsyncOpenAI(api_key="test-placeholder", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))) as sdk:
            client = guard(sdk)
            await client.create(**payload())
            assert [path for path, _ in requests] == ["/v1/responses/input_tokens", "/v1/responses"]
            counted, generated = requests[0][1], requests[1][1]
            assert all(generated[k] == v for k, v in counted.items())
            assert "max_output_tokens" not in counted
            assert generated["max_output_tokens"] == 96
    asyncio.run(scenario())
