"""Every tool assertion crosses real MCP stdio into an independent process.

Direct Python calls are used only to prepare temporary data or trusted grants.
No test writes to the demo database or represents production user consent.
"""

import asyncio
import json
import sqlite3
import subprocess
import sys

import pytest

from approval_store import issue_approval
import database
from mcp_client_test import EXPECTED_TOOLS, ROOT, mcp_connection, smoke_test

C1 = "demo_customer_001"
C2 = "demo_customer_002"


def grant(path, tool, arguments, *, customer_id=C1, session_id="local-demo-session"):
    # Trusted test-harness consent simulation, never an MCP tool.
    return issue_approval(path, customer_id=customer_id, session_id=session_id,
                          tool=tool, arguments=arguments, confirmed=True)


def error(result, code):
    assert result.isError is True
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["data"] is None
    assert result.structuredContent["error"]["code"] == code


def data(result):
    assert result.isError is False
    assert result.structuredContent["ok"] is True
    assert result.structuredContent["error"] is None
    return result.structuredContent["data"]


def test_real_mcp_smoke_client(db_path):
    report = asyncio.run(smoke_test(db_path))
    assert report["mcp_version"] == "1.27.2"
    assert set(report["tools"]) == EXPECTED_TOOLS


def test_discovery_schemas_and_identity_boundary(db_path):
    async def scenario():
        async with mcp_connection(db_path) as session:
            tools = (await session.list_tools()).tools
            assert len(tools) == 6
            for tool in tools:
                assert tool.description
                assert tool.outputSchema["type"] == "object"
                assert tool.inputSchema["additionalProperties"] is False
                assert not {"customer_id", "session_id", "approved", "db_path"} & set(tool.inputSchema["properties"])
                assert tool.annotations.readOnlyHint == (tool.name not in {"create_ticket", "save_memory"})
    asyncio.run(scenario())


def test_reads_and_access_denials_over_protocol(db_path):
    async def scenario():
        async with mcp_connection(db_path) as session:
            assert data(await session.call_tool("get_ticket_status", {"ticket_id": "DEMO-TKT-001"}))["status"] == "open"
            for ticket_id in ["DEMO-TKT-002", "unknown", "' OR 1=1 --"]:
                error(await session.call_tool("get_ticket_status", {"ticket_id": ticket_id}), "ACCESS_DENIED")
            for order_id in ["DEMO-2001", "unknown", "' OR 1=1 --"]:
                error(await session.call_tool("get_order_status", {"order_id": order_id}), "ACCESS_DENIED")
            assert data(await session.call_tool("search_faq", {"query": "xyzunknown987"})) == []
            assert data(await session.call_tool("load_memory", {})) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("tool,arguments", [
    ("search_faq", {"query": ""}),
    ("search_faq", {"query": "   "}),
    ("search_faq", {"query": 42}),
    ("search_faq", {"query": "x" * 1001}),
    ("get_order_status", {}),
    ("get_order_status", {"order_id": None}),
    ("get_order_status", {"order_id": ["DEMO-1001"]}),
    ("get_order_status", {"order_id": "bad\x00id"}),
    ("get_order_status", {"order_id": "DEMO-2001", "customer_id": C2}),
    ("create_ticket", {"order_id": "DEMO-1002", "description": "a", "approved": True}),
    ("create_ticket", {"order_id": "DEMO-1002", "description": "x" * 4001}),
    ("save_memory", {"content": "summary", "session_id": "another-session"}),
    ("save_memory", {"content": "summary", "customer_id": C2}),
    ("save_memory", {"content": "summary", "approved": True}),
    ("save_memory", {"content": "x" * 8001}),
    ("load_memory", {"session_id": "another-session"}),
    ("load_memory", {"customer_id": C2}),
])
def test_invalid_inputs_are_structured_errors(db_path, tool, arguments):
    async def scenario():
        async with mcp_connection(db_path) as session:
            error(await session.call_tool(tool, arguments), "INVALID_INPUT")
            # Invalid input must not kill the process/connection.
            assert data(await session.call_tool("get_order_status", {"order_id": "DEMO-1001"}))["status"] == "shipped"
    asyncio.run(scenario())


def test_missing_and_forged_approval_denied(db_path):
    async def scenario():
        async with mcp_connection(db_path) as session:
            for extra in [{}, {"approval_id": "invented-by-model"}]:
                error(await session.call_tool("create_ticket", {
                    "order_id": "DEMO-1002", "description": "unapproved", **extra,
                }), "APPROVAL_REQUIRED")
                error(await session.call_tool("save_memory", {"content": "unapproved", **extra}), "APPROVAL_REQUIRED")
            error(await session.call_tool("issue_approval", {}), "UNKNOWN_TOOL")
    asyncio.run(scenario())
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM conversation_memory").fetchone()[0] == 0


def test_ticket_approval_bound_to_exact_operation_and_persistent_single_use(db_path, tmp_path):
    approval_db = tmp_path / "approvals.sqlite3"
    arguments = {"order_id": "DEMO-1002", "description": "exact confirmed description"}
    approval_id = grant(approval_db, "create_ticket", arguments)

    async def scenario():
        async with mcp_connection(db_path, approval_db=approval_db) as session:
            error(await session.call_tool("create_ticket", {**arguments, "description": "changed", "approval_id": approval_id}), "APPROVAL_REQUIRED")
            error(await session.call_tool("create_ticket", {**arguments, "order_id": "DEMO-1001", "approval_id": approval_id}), "APPROVAL_REQUIRED")
            error(await session.call_tool("save_memory", {"content": "other operation", "approval_id": approval_id}), "APPROVAL_REQUIRED")
            result = data(await session.call_tool("create_ticket", {**arguments, "approval_id": approval_id}))
            assert result["status"] == "open"
            assert data(await session.call_tool("get_ticket_status", {"ticket_id": result["ticket_id"]})) == result
        # Server restart must not resurrect a consumed approval.
        async with mcp_connection(db_path, approval_db=approval_db) as session:
            replay = data(await session.call_tool("create_ticket", {**arguments, "approval_id": approval_id}))
            assert replay == result
    asyncio.run(scenario())
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 4


def test_memory_approval_identity_binding_and_session_isolation(db_path, tmp_path):
    approval_db = tmp_path / "approvals.sqlite3"
    arguments = {"content": "private confirmed summary"}
    approval_id = grant(approval_db, "save_memory", arguments)

    async def scenario():
        # Same grant cannot be spent by another session or customer.
        for identity in [{"session_id": "other"}, {"customer_id": C2}]:
            async with mcp_connection(db_path, approval_db=approval_db, **identity) as session:
                error(await session.call_tool("save_memory", {**arguments, "approval_id": approval_id}), "APPROVAL_REQUIRED")
                assert data(await session.call_tool("load_memory", {})) is None
        async with mcp_connection(db_path, approval_db=approval_db) as session:
            assert data(await session.call_tool("save_memory", {**arguments, "approval_id": approval_id}))["saved"] is True
            assert data(await session.call_tool("load_memory", {}))["content"] == arguments["content"]
            replay = data(await session.call_tool("save_memory", {**arguments, "approval_id": approval_id}))
            assert replay == {"saved": True, "session_id": "local-demo-session"}
        # A fresh MCP subprocess sees the same SQLite-backed memory after restart.
        async with mcp_connection(db_path, approval_db=approval_db) as restarted:
            assert data(await restarted.call_tool("load_memory", {}))["content"] == arguments["content"]
        # Stored memory stays isolated after it exists, not only before saving.
        for identity in [{"session_id": "other"}, {"customer_id": C2}]:
            async with mcp_connection(db_path, **identity) as session:
                assert data(await session.call_tool("load_memory", {})) is None
    asyncio.run(scenario())


def test_expired_and_unknown_grants_are_denied(db_path, tmp_path):
    approval_db = tmp_path / "approvals.sqlite3"
    arguments = {"content": "expired content"}
    approval_id = grant(approval_db, "save_memory", arguments)
    with sqlite3.connect(approval_db) as connection:
        connection.execute("UPDATE write_approvals SET expires_at=?", (0,))

    async def scenario():
        async with mcp_connection(db_path, approval_db=approval_db) as session:
            for reference in [approval_id, "forged"]:
                error(await session.call_tool("save_memory", {**arguments, "approval_id": reference}), "APPROVAL_REQUIRED")
            assert data(await session.call_tool("load_memory", {})) is None
    asyncio.run(scenario())


def test_approval_does_not_bypass_order_ownership(db_path, tmp_path):
    approval_db = tmp_path / "approvals.sqlite3"
    arguments = {"order_id": "DEMO-2001", "description": "wrong owner"}
    approval_id = grant(approval_db, "create_ticket", arguments)

    async def scenario():
        async with mcp_connection(db_path, approval_db=approval_db) as session:
            error(await session.call_tool("create_ticket", {**arguments, "approval_id": approval_id}), "ACCESS_DENIED")
            # A definite failure burns the grant and asks for fresh confirmation.
            error(await session.call_tool("create_ticket", {**arguments, "approval_id": approval_id}), "WRITE_FAILED")
    asyncio.run(scenario())
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone()[0] == 3


def test_storage_errors_are_redacted_and_fail_closed(db_path, tmp_path):
    missing_approval_db = tmp_path / "private-missing.sqlite3"

    async def scenario():
        async with mcp_connection(db_path, approval_db=missing_approval_db) as session:
            result = await session.call_tool("save_memory", {"content": "summary", "approval_id": "fake"})
            error(result, "STORAGE_ERROR")
            assert str(missing_approval_db) not in result.model_dump_json()
            assert data(await session.call_tool("load_memory", {})) is None
    asyncio.run(scenario())
    assert not missing_approval_db.exists()


def test_standalone_start_rejects_missing_session_without_stdout(db_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "mcp_server.py"), "--db", str(db_path)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "--demo-customer-id" in result.stderr


def test_standalone_start_rejects_unknown_customer_without_stdout(db_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "mcp_server.py"), "--db", str(db_path),
         "--demo-customer-id", "unknown", "--demo-session-id", "s"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Cannot start" in result.stderr


def test_normal_stdio_contains_only_json_rpc_and_diagnostics_use_stderr(db_path, tmp_path):
    async def scenario():
        with (tmp_path / "diagnostics.txt").open("wb") as diagnostics:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "mcp_server.py"), "--db", str(db_path),
                "--demo-customer-id", C1, "--demo-session-id", "raw-protocol-test",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=diagnostics,
            )
            try:
                async def send(message):
                    process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                    await process.stdin.drain()

                await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "raw-stdio-test", "version": "1"},
                }})
                initialized = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
                assert initialized["jsonrpc"] == "2.0" and initialized["id"] == 1
                await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                tools = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
                assert tools["id"] == 2
                assert {t["name"] for t in tools["result"]["tools"]} == EXPECTED_TOOLS
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 15)
                assert process.returncode == 0
                assert await process.stdout.read() == b""
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        assert "Local demo identity" in (tmp_path / "diagnostics.txt").read_text()
    asyncio.run(scenario())
