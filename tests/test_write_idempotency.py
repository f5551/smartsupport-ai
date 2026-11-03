"""Crash and concurrency checks for the separate approval/store ledgers."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import subprocess
import sys
import threading

import pytest

from approval_store import (
    WriteOperationPending, issue_approval, reconcile_operation,
)
import database
import mcp_server


CUSTOMER = "demo_customer_001"
SESSION = "idempotency-test-session"
ARGS = {"order_id": "DEMO-1002", "description": "synthetic idempotency test"}


def setup_context(db_path, tmp_path):
    approvals = tmp_path / "approvals.sqlite3"
    return mcp_server.DemoSession(CUSTOMER, SESSION, db_path, approvals), approvals


def issue(approvals, args=ARGS):
    return issue_approval(approvals, customer_id=CUSTOMER, session_id=SESSION,
                          tool="create_ticket", arguments=args, confirmed=True)


def ticket_count(db_path):
    with database.connect(db_path) as connection:
        return connection.execute("SELECT count(*) FROM tickets").fetchone()[0]


def test_same_approval_replays_committed_result_once(db_path, tmp_path):
    context, approvals = setup_context(db_path, tmp_path)
    approval = issue(approvals)
    result = mcp_server.execute_approved_write(context, "create_ticket", ARGS, approval)
    assert mcp_server.execute_approved_write(context, "create_ticket", ARGS, approval) == result
    assert ticket_count(db_path) == 4
    with database.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM write_operations").fetchone()[0] == 1


def test_concurrent_use_of_same_approval_has_one_writer(db_path, tmp_path):
    context, approvals = setup_context(db_path, tmp_path)
    approval = issue(approvals)
    entered, release = threading.Event(), threading.Event()
    original = database.create_ticket

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    database.create_ticket = blocked_write
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(mcp_server.execute_approved_write, context, "create_ticket", ARGS, approval)
            assert entered.wait(timeout=10)
            second = pool.submit(mcp_server.execute_approved_write, context, "create_ticket", ARGS, approval)
            with pytest.raises(WriteOperationPending):
                second.result(timeout=10)
            release.set()
            result = first.result(timeout=10)
    finally:
        release.set()
        database.create_ticket = original
    assert mcp_server.execute_approved_write(context, "create_ticket", ARGS, approval) == result
    assert ticket_count(db_path) == 4


def test_crash_before_store_commit_requires_new_approval(db_path, tmp_path, monkeypatch):
    context, approvals = setup_context(db_path, tmp_path)
    approval = issue(approvals)
    original = database.create_ticket

    def fail_before_write(*args, **kwargs):
        raise ValueError("isolated pre-write failure")

    monkeypatch.setattr(database, "create_ticket", fail_before_write)
    with pytest.raises(ValueError):
        mcp_server.execute_approved_write(context, "create_ticket", ARGS, approval)
    assert ticket_count(db_path) == 3
    with sqlite3.connect(approvals) as connection:
        assert connection.execute("SELECT status FROM write_operations").fetchone()[0] == "failed"

    monkeypatch.setattr(database, "create_ticket", original)
    retry_approval = issue(approvals)
    result = mcp_server.execute_approved_write(context, "create_ticket", ARGS, retry_approval)
    assert result["status"] == "open"
    assert ticket_count(db_path) == 4


def test_process_crash_after_claim_before_store_write_is_reconciled(db_path, tmp_path):
    context, approvals = setup_context(db_path, tmp_path)
    approval = issue(approvals)
    script = r'''
import os, sys
from approval_store import consume_approval
consume_approval(sys.argv[1], approval_id=sys.argv[3], customer_id=sys.argv[4],
                 session_id=sys.argv[5], tool="create_ticket",
                 arguments={"order_id":sys.argv[6], "description":sys.argv[7]},
                 store_db=sys.argv[2])
os._exit(23)
'''
    child = subprocess.run(
        [sys.executable, "-c", script, str(approvals), str(db_path), approval,
         CUSTOMER, SESSION, ARGS["order_id"], ARGS["description"]],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        capture_output=True, timeout=15,
    )
    assert child.returncode == 23
    assert ticket_count(db_path) == 3

    # Startup reconciliation observes an active lease and does not replay it.
    restarted = mcp_server.DemoSession(CUSTOMER, SESSION, db_path, approvals)
    with sqlite3.connect(approvals) as connection:
        assert connection.execute("SELECT status FROM write_operations").fetchone()[0] == "pending"
        connection.execute("UPDATE write_operations SET lease_until=0")

    from approval_store import WriteOperationFailed
    with pytest.raises(WriteOperationFailed):
        mcp_server.execute_approved_write(restarted, "create_ticket", ARGS, approval)
    assert ticket_count(db_path) == 3
    fresh = issue(approvals)
    assert mcp_server.execute_approved_write(restarted, "create_ticket", ARGS, fresh)["status"] == "open"
    assert ticket_count(db_path) == 4


def test_crash_after_store_commit_reconciles_without_duplicate(db_path, tmp_path, monkeypatch):
    context, approvals = setup_context(db_path, tmp_path)
    approval = issue(approvals)
    original = mcp_server.settle_operation

    def lose_approval_ack(*args, **kwargs):
        raise sqlite3.OperationalError("simulated process interruption")

    monkeypatch.setattr(mcp_server, "settle_operation", lose_approval_ack)
    with pytest.raises(Exception, match="Operation outcome requires reconciliation"):
        mcp_server.execute_approved_write(context, "create_ticket", ARGS, approval)
    assert ticket_count(db_path) == 4
    monkeypatch.setattr(mcp_server, "settle_operation", original)

    replay = mcp_server.execute_approved_write(context, "create_ticket", ARGS, approval)
    assert replay["status"] == "open"
    assert reconcile_operation(approvals, db_path,
                               __import__("approval_store")._binding(CUSTOMER, SESSION, "create_ticket", ARGS)[1])["status"] == "committed"
    assert ticket_count(db_path) == 4


def test_approval_and_store_bindings_prevent_customer_session_or_content_reuse(db_path, tmp_path):
    context, approvals = setup_context(db_path, tmp_path)
    approval = issue(approvals)
    for changed in [dict(ARGS, description="changed after consent")]:
        with pytest.raises(Exception):
            mcp_server.execute_approved_write(context, "create_ticket", changed, approval)
    assert ticket_count(db_path) == 3
