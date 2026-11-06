"""Preparation checks only: no SQLite ticket inserts and no external API calls."""
import asyncio
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from mcp.types import ListToolsResult, Tool

from agent import Agent
from config import AgentConfig
from fake_openai import FakeOpenAI, fake_response, tool_call
from mcp_server import DEFINITIONS
from mcp_transport import TrustedSession
import ticket_retest as retest

TICKET = "TKT-" + "a" * 32


class SimulatedMCP:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return ListToolsResult(tools=[Tool(name=n, description=d, inputSchema=m.model_json_schema())
                                     for n, (m, d) in DEFINITIONS.items()])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(isError=False, structuredContent={"ok": True, "data": {
            "ticket_id": TICKET, "order_id": "DEMO-1002", "status": "open"}, "error": None})


@pytest.mark.parametrize("argv", [[], ["--execute-live"], ["--approve-exact-ticket"]])
def test_no_execution_without_both_explicit_flags(monkeypatch, argv, capsys):
    monkeypatch.setattr(retest.AgentConfig, "from_env", lambda: pytest.fail("No config/DB/network access allowed"))
    retest.main(argv)
    assert "PREPARED ONLY" in capsys.readouterr().out


@pytest.mark.parametrize("last_response", [
    fake_response(f"تم إنشاء التذكرة {TICKET}"),
    fake_response(calls=[tool_call("create_ticket", retest.ARGUMENTS, "second")]),
])
def test_approval_resume_final_and_single_simulated_write(last_response):
    async def scenario():
        backend = SimulatedMCP()
        mcp = retest.SingleWriteMCP(backend)
        sdk = FakeOpenAI([fake_response(calls=[tool_call("create_ticket", retest.ARGUMENTS, "first")]), last_response])
        client = retest.TwoRequestClient(sdk)
        cfg = AgentConfig(model="fake-test-model", max_turns=2, max_tool_calls=1,
                          input_price=Decimal(".75"), output_price=Decimal("4.5"))
        agent = Agent(client, mcp, TrustedSession("demo_customer_001", "test-only"), cfg)
        pending = await agent.run("أنشئ تذكرة تجريبية")
        assert pending.stop_reason == "pending_approval" and retest.exact_pending(pending.pending_approval)
        assert backend.calls == []
        still_pending = await agent.resume(pending.task_id, pending.pending_approval["pending_id"], decision="approve")
        assert still_pending.stop_reason == "pending_approval" and backend.calls == []
        final = await agent.resume(pending.task_id, pending.pending_approval["pending_id"],
                                   decision="approve", approval_id="test-only-grant")
        assert final.stop_reason == "final_answer" and final.turns == 2
        assert len(backend.calls) == mcp.calls == 1
        assert sdk.requests[0]["tool_choice"] == {"type": "function", "name": "create_ticket"}
        assert sdk.requests[1]["tool_choice"] == "none"
        assert sdk.options["max_retries"] == 0
        report = retest.public_report(final, [(TICKET, "demo_customer_001", "DEMO-1002", retest.ARGUMENTS["description"])], True, mcp.calls)
        assert report["status"] == "PASS" and TICKET in report["answer"]
        serialized = json.dumps(report)
        for internal in ("write_receipts", "test-only-grant", "test-only", "result_json", "approval_id", "description"):
            assert internal not in serialized
        with pytest.raises(ValueError):
            await agent.resume(pending.task_id, pending.pending_approval["pending_id"], decision="approve", approval_id="test-only-grant")
        assert len(backend.calls) == 1
    asyncio.run(scenario())


def test_mismatched_operation_and_duplicate_rejected_before_mcp():
    async def scenario():
        backend = SimulatedMCP()
        guard = retest.SingleWriteMCP(backend)
        for name, arguments in [("save_memory", {"content": "x", "approval_id": "grant"}),
                                ("create_ticket", {**retest.ARGUMENTS, "description": "changed", "approval_id": "grant"}),
                                ("create_ticket", retest.ARGUMENTS)]:
            with pytest.raises(ValueError):
                await guard.call_tool(name, arguments)
        assert backend.calls == []
        args = {**retest.ARGUMENTS, "approval_id": "grant"}
        await guard.call_tool("create_ticket", args)
        with pytest.raises(ValueError):
            await guard.call_tool("create_ticket", args)
        assert len(backend.calls) == 1
    asyncio.run(scenario())


def test_only_exact_pending_can_receive_approval():
    assert retest.exact_pending({"tool": "create_ticket", "arguments": retest.ARGUMENTS})
    assert not retest.exact_pending({"tool": "create_ticket", "arguments": {**retest.ARGUMENTS, "order_id": "DEMO-1001"}})
    assert not retest.exact_pending({"tool": "save_memory", "arguments": retest.ARGUMENTS})


@pytest.mark.parametrize("usage", [True, False])
def test_server_count_guard_integrates_with_approval_and_stops_unknown_usage(usage):
    from cost_tracker import load_price
    from ticket_budget import BudgetedResponses, MODEL

    async def scenario():
        class CountableFake(FakeOpenAI):
            async def count(self, **kwargs):
                return SimpleNamespace(input_tokens=20)

        sdk = CountableFake([
            fake_response(calls=[tool_call("create_ticket", retest.ARGUMENTS)], model=MODEL, usage=usage).model_copy(update={"service_tier": "default"}),
            fake_response(f"تم إنشاء التذكرة {TICKET}", model=MODEL).model_copy(update={"service_tier": "default"}),
        ])
        sdk.input_tokens = SimpleNamespace(count=sdk.count)
        ledger_rows = {}
        def reserve(key, amount, limit):
            ledger_rows[key] = (amount, "reserved")
            return True
        def settle(key, actual):
            ledger_rows[key] = (actual, "settled" if actual is not None else "unknown")
        ledger = SimpleNamespace(reserve=reserve, settle=settle)
        price = load_price(MODEL)
        guard = BudgetedResponses(sdk, price, ledger)
        backend = SimulatedMCP()
        agent = Agent(retest.TwoRequestClient(guard), retest.SingleWriteMCP(backend),
                      TrustedSession("demo_customer_001", "mock-only"),
                      AgentConfig(model=MODEL, pricing=price, max_turns=2, max_output_tokens=96,
                                  max_input_tokens=2500, usage_source="api"))
        agent.daily = None  # Guard is the sole owner of daily accounting.
        pending = await agent.run("تذكرة اختبار")
        assert not backend.calls
        if not usage:
            assert pending.stop_reason == "openai_error" and guard.halted
            assert guard.calls == 1
            assert [v[1] for v in ledger_rows.values()] == ["unknown"]
            return
        assert pending.stop_reason == "pending_approval"
        result = await agent.resume(pending.task_id, pending.pending_approval["pending_id"],
                                    decision="approve", approval_id="mock-only-grant")
        assert result.stop_reason == "final_answer" and TICKET in result.message
        assert len(backend.calls) == 1 and guard.calls == 2
        assert len(ledger_rows) == 2
        assert all(v[1] == "settled" for v in ledger_rows.values())
    asyncio.run(scenario())
