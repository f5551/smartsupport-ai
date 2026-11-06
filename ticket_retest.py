"""Prepare by default. Live execution requires two explicit CLI authorization flags."""
import argparse
import asyncio
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from agent import Agent
from approval_store import issue_approval
from audit_logger import AuditLogger
from config import AgentConfig, ROOT
from daily_cost import DailyCostLedger
from database import init_database, seed_demo_data
from mcp_transport import TrustedSession, mcp_connection
from openai_transport import create_async_openai_client
from ticket_budget import BudgetedResponses, generation_bound

ARGUMENTS = {"order_id": "DEMO-1002", "description": "تلف تجريبي لاختبار القبول النهائي"}


def exact_pending(pending):
    return (isinstance(pending, dict) and pending.get("tool") == "create_ticket"
            and pending.get("arguments") == ARGUMENTS)


class SingleWriteMCP:
    """Only forward one exact, approved create_ticket call, even after failure."""
    def __init__(self, session):
        self.session, self.calls = session, 0

    async def list_tools(self):
        return await self.session.list_tools()  # Preserve the six-tool discovery contract.

    async def call_tool(self, name, arguments):
        content = {k: v for k, v in arguments.items() if k != "approval_id"}
        if (name != "create_ticket" or content != ARGUMENTS or not arguments.get("approval_id")
                or self.calls):
            raise ValueError("Write not authorized for this test")
        self.calls += 1  # A timeout/unknown result never permits another attempt.
        return await self.session.call_tool(name, arguments=arguments)


class TwoRequestClient:
    def __init__(self, client):
        self.client, self.calls, self.responses = client, 0, self
        self.usage_source = getattr(client, "usage_source", "api")

    def with_options(self, **options):
        self.client = self.client.with_options(**options)
        return self

    async def create(self, **kwargs):
        if self.calls >= 2:
            raise ValueError("Two-request limit reached")
        self.calls += 1
        kwargs["tools"] = [t for t in kwargs["tools"] if t["name"] == "create_ticket"]
        kwargs["tool_choice"] = {"type": "function", "name": "create_ticket"} if self.calls == 1 else "none"
        kwargs["reasoning"] = {"effort": "none"}
        return await self.client.responses.create(**kwargs)


def public_report(result, new_rows, committed, calls):
    """Export booleans/counters and a verified ticket number, never internal receipts."""
    ticket = new_rows[0][0] if len(new_rows) == 1 else None
    safe_ticket = ticket if isinstance(ticket, str) and re.fullmatch(r"TKT-[0-9a-f]{32}", ticket) else None
    checks = {
        "one_ticket": len(new_rows) == 1,
        "owner_and_order": len(new_rows) == 1 and new_rows[0][1:] == ("demo_customer_001", ARGUMENTS["order_id"], ARGUMENTS["description"]),
        "durable_result": bool(committed),
        "one_mcp_write": calls == 1,
        "final_answer": result.stop_reason == "final_answer",
        "ticket_in_answer": bool(safe_ticket and safe_ticket in result.message),
        "usage_settled": result.usage_complete and result.cost_summary.get("total_cost_usd") is not None,
        "turns_within_limit": result.turns <= 2,
    }
    cost = result.cost_summary.get("total_cost_usd")
    checks["budget"] = cost is not None and Decimal(cost) <= Decimal("0.005")
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "ticket_id": safe_ticket, "stop_reason": result.stop_reason,
            "answer": f"تم إنشاء التذكرة {safe_ticket}." if all(checks.values()) else "لم يكتمل التحقق؛ لا تعِد الكتابة قبل مراجعة النتيجة.",
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "cost_usd": cost}


async def execute(config):
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Credential unavailable")
    if config.max_turns < 2 or config.pricing is None:
        raise ValueError("Two turns and verified model pricing are required")
    config = replace(config, max_turns=2, max_tool_calls=1, max_input_tokens=min(config.max_input_tokens, 2500),
                     max_output_tokens=min(config.max_output_tokens, 96), max_retries=0,
                     budget_usd=min(config.budget_usd, Decimal("0.005")), model=config.pricing.model)
    envelope = generation_bound(config.pricing, config.max_input_tokens, config.max_output_tokens)
    ledger = DailyCostLedger(config.daily_cost_path)
    if envelope > config.budget_usd or Decimal(ledger.snapshot()["committed_usd"]) + envelope > config.max_daily_cost_usd:
        raise ValueError("Budget admission refused")
    run_id = uuid4().hex
    audit = AuditLogger(ROOT / "logs" / f"ticket_retest_{run_id}.jsonl", source="isolated_integration")
    context = TrustedSession("demo_customer_001", "ticket-retest-" + run_id)
    with TemporaryDirectory(prefix="smartsupport-ticket-retest-") as td:
        store, approvals = Path(td) / "store.sqlite3", Path(td) / "approvals.sqlite3"
        init_database(store)
        seed_demo_data(store)
        with closing(sqlite3.connect(store)) as connection:
            before = {r[0] for r in connection.execute("SELECT ticket_id FROM tickets")}
        async with create_async_openai_client(max_retries=0, timeout=config.timeout_seconds,
                                               base_url="https://api.openai.com/v1") as client:
            guarded = BudgetedResponses(client, config.pricing, ledger, task_limit=config.budget_usd,
                                       daily_limit=config.max_daily_cost_usd, input_limit=config.max_input_tokens,
                                       output_limit=config.max_output_tokens)
            async with mcp_connection(store, customer_id=context.customer_id, session_id=context.session_id,
                                      approval_db=approvals) as session:
                mcp = SingleWriteMCP(session)
                agent = Agent(TwoRequestClient(guarded), mcp, context, config, audit=audit)
                # The guard owns server-counted atomic daily reservations;
                # avoid charging the same request a second estimated reservation.
                agent.daily = None
                result = await agent.run("أنشئ تذكرة للطلب DEMO-1002 بالوصف الحرفي: " + ARGUMENTS["description"])
                if (result.stop_reason == "pending_approval" and exact_pending(result.pending_approval)
                        and result.usage_complete and not guarded.halted):
                    pending = result.pending_approval
                    grant = issue_approval(approvals, customer_id=context.customer_id, session_id=context.session_id,
                                           tool="create_ticket", arguments=ARGUMENTS, confirmed=True)
                    result = await agent.resume(result.task_id, pending["pending_id"], decision="approve", approval_id=grant)
        with closing(sqlite3.connect(store)) as connection:
            rows = [r for r in connection.execute("SELECT ticket_id,customer_id,order_id,description FROM tickets") if r[0] not in before]
            committed = False
            if len(rows) == 1:
                operations = connection.execute("SELECT result_json FROM write_operations WHERE action='create_ticket' AND state='committed'").fetchall()
                committed = len(operations) == 1 and json.loads(operations[0][0]).get("ticket_id") == rows[0][0]
        report = public_report(result, rows, committed, mcp.calls)
        report["generation_upper_bound_usd"] = str(guarded.bound)
        report["count_requests"] = guarded.count_calls
        report["generation_requests"] = guarded.calls
        if guarded.halted:
            report["status"] = "FAIL"
        report["cached_tokens"] = sum(e["cached_tokens"] or 0 for e in audit.events if e["event_type"] == "model_result")
        # Persist only the allowlisted result before the temporary store disappears.
        path = ROOT / "reports" / f"ticket_retest_{run_id}.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=True, indent=2)
        print(json.dumps(report, ensure_ascii=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--approve-exact-ticket", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute_live or not args.approve_exact_ticket:
        print("PREPARED ONLY: new explicit consent to cost and exact ticket is required. No execution.")
        return
    try:
        asyncio.run(execute(AgentConfig.from_env()))
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}, ensure_ascii=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
