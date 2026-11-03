"""Reproducible offline audit: synthetic temporary stores, actual Agent/MCP/SQLite.

Run with .venv/Scripts/python.exe reports/evidence/performance_harness.py.
Never constructs a paid OpenAI client; does not use the original store or traces.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from openai import APIConnectionError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from agent import Agent
from approval_store import issue_approval
from audit_logger import AuditLogger
from config import AgentConfig
from database import init_database, seed_demo_data
from fake_openai import FakeOpenAI, fake_response, tool_call
from mcp_transport import TrustedSession, mcp_connection
from web_controls import WebError
import web_runtime
from web_runtime import WebBackend


CONTEXT = TrustedSession("demo_customer_001", "offline-reliability-audit")

def config(**overrides):
    return AgentConfig(**{"model": "fake-test-model", "input_price": Decimal("0.1"),
                          "cached_input_price": Decimal("0.05"), "output_price": Decimal("0.2"),
                          "max_retries": 0, **overrides})

def fixture(directory):
    db = Path(directory) / "store.sqlite3"
    init_database(db)
    seed_demo_data(db)
    return db

def scalar(db, sql):
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute(sql).fetchone()[0]

@asynccontextmanager
async def connection(db, mode=None, approval_db=None):
    if mode is None:
        async with mcp_connection(db, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id,
                                  approval_db=approval_db) as session:
            yield session
    else:
        args = [str(Path(__file__).with_name("reliability_fault_mcp.py")), mode,
                "--db", str(db), "--demo-customer-id", CONTEXT.customer_id,
                "--demo-session-id", CONTEXT.session_id]
        params = StdioServerParameters(command=sys.executable, args=args, cwd=str(ROOT),
                                       env={"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        with (Path(db).parent / "fault_mcp_stderr.log").open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                    await session.initialize()
                    yield session

async def agent_scenario(directory, kind):
    db = fixture(directory)
    audit = AuditLogger(Path(directory) / "audit.jsonl")
    options = {}
    if kind == "openai_connection_error":
        client = FakeOpenAI([APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))])
    elif kind == "openai_timeout":
        class SlowFake(FakeOpenAI):
            async def create(self, **kwargs):
                self.requests.append(kwargs)
                await asyncio.sleep(1)
        client = SlowFake([])
        options["timeout_seconds"] = 0.02
    elif kind == "budget":
        client = FakeOpenAI([])
        options["budget_usd"] = Decimal("0.000001")
    else:
        client = FakeOpenAI([fake_response(calls=[tool_call("search_faq", {"query": "مدة الشحن"})])])
    mode = {"mcp_timeout": "hang", "mcp_crash": "crash"}.get(kind)
    holder = {}
    try:
        async with connection(db, mode=mode) as session:
            holder["result"] = await Agent(client, session, CONTEXT, config(**options), audit=audit).run("كم مدة الشحن؟")
    except BaseException as exc:
        holder["transport_shutdown_error"] = type(exc).__name__
    result = holder.get("result")
    assert result is not None, holder
    expected = "openai_error" if kind.startswith("openai_") else "budget" if kind == "budget" else "mcp_error"
    assert result.stop_reason == expected, result.stop_reason
    expected_requests = 0 if kind == "budget" else 1
    assert len(client.requests) == expected_requests
    assert scalar(db, "SELECT count(*) FROM tickets") == 3
    return {"stop_reason": result.canonical_stop_reason, "model_requests": len(client.requests),
            "mcp_tool_attempts": result.tools_used,
            "audit_errors": [e["error_code"] for e in audit.events if e["error_code"]],
            "usage_complete": result.usage_complete, "tickets_unchanged": True,
            "transport_shutdown_error": holder.get("transport_shutdown_error")}

async def unavailable_database(directory):
    db = fixture(directory)
    async with connection(db) as session:
        # Rename this test's own file only, after server initialization succeeds.
        moved = db.with_name("offline.sqlite3")
        db.rename(moved)
        response = await session.call_tool("get_order_status", arguments={"order_id": "DEMO-1001"})
        payload = response.structuredContent
        assert payload["ok"] is False and payload["error"]["code"] == "STORAGE_ERROR", payload
        assert not db.exists()
    return {"mcp_error_code": payload["error"]["code"], "database_recreated": False,
            "original_test_file_preserved": moved.exists()}

async def failed_write_after_grant(directory):
    db = fixture(directory)
    approval_db = Path(directory) / "approvals.sqlite3"
    with closing(sqlite3.connect(db)) as connection_db:
        with connection_db:
            connection_db.execute("CREATE TRIGGER audit_fail_write BEFORE INSERT ON tickets BEGIN SELECT RAISE(ABORT, 'audit injected failure'); END")
    arguments = {"order_id": "DEMO-1002", "description": "عطل اصطناعي لاختبار الفشل"}
    grant = issue_approval(approval_db, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id,
                           tool="create_ticket", arguments=arguments, confirmed=True)
    async with connection(db, approval_db=approval_db) as session:
        first = (await session.call_tool("create_ticket", arguments={**arguments, "approval_id": grant})).structuredContent
        second = (await session.call_tool("create_ticket", arguments={**arguments, "approval_id": grant})).structuredContent
    assert first["ok"] is False and first["error"]["code"] == "STORAGE_ERROR"
    assert second["ok"] is False and second["error"]["code"] == "APPROVAL_REQUIRED"
    assert scalar(db, "SELECT count(*) FROM tickets") == 3
    assert scalar(approval_db, "SELECT count(*) FROM write_approvals WHERE consumed_at IS NOT NULL") == 1
    return {"first_error": first["error"]["code"], "replay_error": second["error"]["code"],
            "grant_consumed": True, "tickets_before": 3, "tickets_after": 3,
            "atomicity_gap_reproduced": True}

def lost_pending(directory):
    backend = WebBackend(directory)
    try:
        visitor = backend.new_visitor()
        cid = backend.new_conversation(visitor, "عميل تجريبي 1")
        pending = backend.send(visitor, cid, "إنشاء شكوى للطلب DEMO-1002")
        conversation = backend.conversations[cid]
        conversation.worker.close()
        try:
            backend.decide(visitor, cid, pending["pending_approval"]["pending_id"], "approve")
        except WebError as exc:
            assert exc.code == "session_lost"
        else:
            raise AssertionError("Lost pending approval was accepted")
        assert scalar(conversation.directory / "store.sqlite3", "SELECT count(*) FROM tickets") == 3
        assert not (conversation.directory / "approvals.sqlite3").exists()
        return {"error_code": "session_lost", "tickets_unchanged": True, "grant_issued": False}
    finally:
        backend.close()

def app_restart(directory):
    backend = WebBackend(directory)
    visitor = backend.new_visitor()
    cid = backend.new_conversation(visitor, "عميل تجريبي 1")
    pending = backend.send(visitor, cid, "إنشاء شكوى للطلب DEMO-1002")
    db = backend.conversations[cid].directory / "store.sqlite3"
    backend.close()
    restarted = WebBackend(directory)
    try:
        try:
            restarted.view(visitor, cid)
        except WebError as exc:
            assert exc.code == "session_lost"
        else:
            raise AssertionError("In-memory registry unexpectedly restored")
        assert scalar(db, "SELECT count(*) FROM tickets") == 3
        return {"old_conversation_access": "session_lost", "pending_recovered": False,
                "database_still_exists": db.exists(), "tickets_unchanged": True}
    finally:
        restarted.close()

def percentile(values, fraction):
    # Explicit nearest-rank; with these small samples p95 is often the maximum.
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]

def summarize(samples):
    values = [row["elapsed_ms"] for row in samples]
    return {"n": len(samples), "p50_ms": percentile(values, 0.5), "p95_ms": percentile(values, 0.95),
            "min_ms": min(values), "max_ms": max(values),
            "failures": sum(not row["success"] for row in samples),
            "database_errors": sum(row.get("error_type") in {"OperationalError", "DatabaseError", "IntegrityError"} for row in samples)}

def measured_send(backend, visitor, cid, stage, wave, sequence):
    start = perf_counter()
    row = {"stage_concurrent_sessions": stage, "wave": wave, "sequence": sequence}
    try:
        result = backend.send(visitor, cid, "كم تستغرق مدة الشحن؟")
        row.update(success=result["stop_reason"] == "final_answer" and result["tools_used"] == ["search_faq"],
                   stop_reason=result["stop_reason"], tools=result["tools_used"],
                   runtime_execution_ms=result["execution_ms"], usage_source=result["cost_summary"]["usage_source"])
    except Exception as exc:
        row.update(success=False, error_type=type(exc).__name__, error_code=getattr(exc, "code", None))
    row["elapsed_ms"] = round((perf_counter() - start) * 1000, 3)
    return row

def bounded_load(directory):
    all_samples, stages = [], []
    for level in (1, 2, 4):
        backend = WebBackend(Path(directory) / f"stage_{level}")
        sessions = []
        try:
            for index in range(level):
                visitor = backend.new_visitor()
                sessions.append((visitor, backend.new_conversation(visitor, f"عميل تجريبي {index + 1}")))
            stage_samples = []
            for wave in ("cold", "warm"):
                with ThreadPoolExecutor(max_workers=level) as executor:
                    jobs = [executor.submit(measured_send, backend, visitor, cid, level, wave, index)
                            for index, (visitor, cid) in enumerate(sessions)]
                    stage_samples.extend(job.result() for job in jobs)
            integrity = []
            for _, cid in sessions:
                db = backend.conversations[cid].directory / "store.sqlite3"
                with closing(sqlite3.connect(db)) as connection_db:
                    integrity.append({"integrity_check": connection_db.execute("PRAGMA integrity_check").fetchone()[0],
                                      "foreign_key_errors": len(connection_db.execute("PRAGMA foreign_key_check").fetchall()),
                                      "ticket_count": connection_db.execute("SELECT count(*) FROM tickets").fetchone()[0]})
            assert all(row["success"] for row in stage_samples), stage_samples
            assert all(row == {"integrity_check": "ok", "foreign_key_errors": 0, "ticket_count": 3} for row in integrity)
            stages.append({"concurrent_sessions": level, "summary": summarize(stage_samples), "sqlite_checks": integrity})
            all_samples.extend(stage_samples)
        finally:
            backend.close()
    return {"samples": all_samples, "stages": stages, "overall": summarize(all_samples),
            "cold": summarize([x for x in all_samples if x["wave"] == "cold"]),
            "warm": summarize([x for x in all_samples if x["wave"] == "warm"])}

def rapid_same_session(directory):
    backend = WebBackend(directory)
    try:
        visitor = backend.new_visitor()
        cid = backend.new_conversation(visitor, "عميل تجريبي 1")
        backend.send(visitor, cid, "كم مدة الشحن؟")  # Warm the worker before queue test.
        with ThreadPoolExecutor(max_workers=3) as executor:
            samples = list(executor.map(lambda i: measured_send(backend, visitor, cid, 1, "rapid_parallel_same_session", i), range(3)))
        assert all(row["success"] for row in samples)
        messages = backend.view(visitor, cid)["messages"]
        assert len(messages) == 8  # Four user + four assistant messages.
        # Creation + four sends used five permits; remaining five complete, next rejects.
        for _ in range(5):
            backend.send(visitor, cid, "مرحبا")
        try:
            backend.send(visitor, cid, "مرحبا")
        except WebError as exc:
            assert exc.code == "rate_limit"
        else:
            raise AssertionError("Per-minute limiter not enforced")
        return {"summary": summarize(samples), "samples": samples, "messages_after_parallel": len(messages),
                "per_minute_limit_rejection": "rate_limit"}
    finally:
        backend.close()

def main():
    os.environ["RUN_LIVE_OPENAI"] = "0"
    os.environ["SMARTSUPPORT_ENABLE_LOCAL_LIVE"] = "0"
    os.environ["SMARTSUPPORT_MAX_WORKERS"] = "4"
    def forbidden_paid_client(**kwargs):
        raise AssertionError("Paid client construction forbidden in offline audit")
    web_runtime.AsyncOpenAI = forbidden_paid_client
    results = {"run_started_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
               "platform": sys.platform, "paid_api_requests": 0, "original_store_used": False,
               "load_warning": "Small shared-host Windows sample; includes synthetic model and real local MCP, not production capacity.",
               "percentile_method": "nearest-rank", "cases": []}
    with TemporaryDirectory(prefix="smartsupport-sre-audit-") as temporary:
        root = Path(temporary)
        scenarios = [(kind, lambda path, name=kind: asyncio.run(agent_scenario(path, name)))
                     for kind in ("openai_connection_error", "openai_timeout", "mcp_timeout", "mcp_crash", "budget")]
        scenarios += [("database_unavailable", lambda path: asyncio.run(unavailable_database(path))),
                      ("write_failure_after_consumed_approval", lambda path: asyncio.run(failed_write_after_grant(path))),
                      ("lost_pending_state", lost_pending), ("full_app_restart", app_restart)]
        for number, (name, function) in enumerate(scenarios, 1):
            path = root / name
            path.mkdir()
            start = perf_counter()
            case = {"id": f"REL-{number:02d}", "scenario": name}
            try:
                case.update(verdict="PASS", evidence=function(path))
            except BaseException as exc:
                case.update(verdict="FAIL", error_type=type(exc).__name__, diagnostic=str(exc)[:800])
            case["elapsed_ms"] = round((perf_counter() - start) * 1000, 3)
            results["cases"].append(case)
        for name, function in (("progressive_load", bounded_load), ("rapid_messages", rapid_same_session)):
            try:
                results[name] = function(root / name)
            except BaseException as exc:
                results[name] = {"failed": True, "error_type": type(exc).__name__, "diagnostic": str(exc)[:800]}
    results["run_finished_utc"] = datetime.now(timezone.utc).isoformat()
    results["case_totals"] = {"passed": sum(c["verdict"] == "PASS" for c in results["cases"]),
                              "failed": sum(c["verdict"] == "FAIL" for c in results["cases"])}
    output = Path(__file__).with_name("performance_results.json")
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "case_totals": results["case_totals"],
                      "load": results.get("progressive_load", {}).get("overall"),
                      "rapid": results.get("rapid_messages", {}).get("summary")}, ensure_ascii=True, indent=2))

if __name__ == "__main__":
    main()
