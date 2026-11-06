"""Durable, scoped approvals and reconciliation for the separate store DB.

Approvals and customer data deliberately remain in separate SQLite files, so a
commit spanning them is NOT atomic. This ledger records pending/committed/
failed/unknown states; the business DB atomically commits each write with its
idempotency result. Reconciliation compares those durable records before retry.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from security import WriteApprovalRequired, require_write_approval, validate_text
from write_idempotency import canonical_payload, idempotency_key as stable_key, payload_hash

LEASE_SECONDS = 120


class WriteOperationPending(Exception):
    pass


class WriteOperationUnknown(Exception):
    pass


class WriteOperationFailed(Exception):
    pass


@contextmanager
def _connect(path, *, create=False):
    path = Path(path).resolve()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
        uri=True, timeout=10, isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _payload(tool: str, arguments: dict) -> str:
    expected = {"create_ticket": {"order_id", "description"}, "save_memory": {"content"}}
    if tool not in expected or set(arguments) != expected[tool]:
        raise ValueError("Approval must match an allowed write operation exactly")
    for key, value in arguments.items():
        validate_text(value, key, {"order_id": 128, "description": 4000, "content": 8000}[key])
    return canonical_payload(arguments)


def _ensure_schema(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS write_approvals ("
        "approval_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, session_id TEXT NOT NULL, "
        "tool TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at REAL, "
        "idempotency_key TEXT)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(write_approvals)")}
    if "idempotency_key" not in columns:
        connection.execute("ALTER TABLE write_approvals ADD COLUMN idempotency_key TEXT")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS write_operations ("
        "idempotency_key TEXT PRIMARY KEY, customer_id TEXT NOT NULL, session_id TEXT NOT NULL, "
        "tool TEXT NOT NULL, payload_hash TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK(status IN ('pending','committed','failed','unknown')), "
        "lease_id TEXT, lease_until REAL, result_json TEXT, updated_at REAL NOT NULL)"
    )
    # Upgrade old, still-unconsumed grant rows without changing their approval.
    for row in connection.execute(
        "SELECT approval_id,customer_id,session_id,tool,payload FROM write_approvals "
        "WHERE idempotency_key IS NULL"
    ).fetchall():
        try:
            args = json.loads(row[4])
            key = stable_key(row[1], row[2], row[3], args)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        connection.execute("UPDATE write_approvals SET idempotency_key=? WHERE approval_id=?",
                            (key, row[0]))


def _binding(customer_id, session_id, tool, arguments):
    payload = _payload(tool, arguments)
    return payload, stable_key(customer_id, session_id, tool, arguments), payload_hash(arguments)


def issue_approval(
    approval_db, *, customer_id: str, session_id: str, tool: str,
    arguments: dict, confirmed: bool = False, ttl_seconds: int = 120,
) -> str:
    """Trusted application only; the grant covers exact identity/action/content."""
    require_write_approval(confirmed)
    validate_text(customer_id, "customer_id", 128)
    validate_text(session_id, "session_id", 128)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
        raise ValueError("Approval lifetime must be 1..300 seconds")
    payload, key, _ = _binding(customer_id, session_id, tool, arguments)
    approval_id = uuid4().hex
    with _connect(approval_db, create=True) as connection:
        _ensure_schema(connection)
        connection.execute(
            "INSERT INTO write_approvals(approval_id,customer_id,session_id,tool,payload,expires_at,consumed_at,idempotency_key) "
            "VALUES(?,?,?,?,?,?,NULL,?)",
            (approval_id, customer_id, session_id, tool, payload, time.time() + ttl_seconds, key),
        )
    return approval_id


def _read_operation(approval_db, key):
    with _connect(approval_db) as connection:
        _ensure_schema(connection)
        row = connection.execute("SELECT * FROM write_operations WHERE idempotency_key=?", (key,)).fetchone()
        return dict(row) if row else None


def _set_state(approval_db, key, status, *, result=None, lease_id=None, lease_until=None):
    with _connect(approval_db) as connection:
        _ensure_schema(connection)
        row = connection.execute("SELECT status FROM write_operations WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return
        if status == "committed":
            connection.execute(
                "UPDATE write_operations SET status='committed',result_json=?,lease_id=NULL,lease_until=NULL,updated_at=? "
                "WHERE idempotency_key=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), time.time(), key),
            )
        else:
            if status == "failed":
                connection.execute(
                    "UPDATE write_operations SET status='failed',lease_id=NULL,lease_until=NULL,updated_at=? "
                    "WHERE idempotency_key=? AND status!='committed' AND (? IS NULL OR lease_id=?)",
                    (time.time(), key, lease_id, lease_id),
                )
            else:
                connection.execute(
                    "UPDATE write_operations SET status=?,updated_at=? WHERE idempotency_key=? AND status!='committed' AND (? IS NULL OR lease_id=?)",
                    (status, time.time(), key, lease_id, lease_id),
                )


def reconcile_operation(approval_db, store_db, key):
    """Consult the business DB before changing a pending/unknown operation."""
    operation = _read_operation(approval_db, key)
    if operation is None:
        return {"status": "failed", "result": None}
    try:
        import database
        stored = database.get_write_operation(key, db_path=store_db)
    except Exception:
        _set_state(approval_db, key, "unknown", lease_id=operation["lease_id"])
        return {"status": "unknown", "result": None}
    if stored is not None:
        same = all(stored[name] == operation[source] for name, source in (
            ("customer_id", "customer_id"), ("session_id", "session_id"),
            ("action", "tool"), ("payload_hash", "payload_hash"),
        ))
        if not same or stored["state"] != "committed":
            _set_state(approval_db, key, "unknown", lease_id=operation["lease_id"])
            return {"status": "unknown", "result": None}
        _set_state(approval_db, key, "committed", result=stored["result"])
        return {"status": "committed", "result": stored["result"]}
    if operation["status"] == "committed":
        # Approval and store backups may be restored independently. Never claim
        # a committed write if its business-side result is absent.
        _set_state(approval_db, key, "unknown", lease_id=operation["lease_id"])
        return {"status": "unknown", "result": None}
    if operation["status"] in {"pending", "unknown"} and (operation["lease_until"] or 0) > time.time():
        return {"status": "pending", "result": None}
    if operation["status"] in {"pending", "unknown"}:
        _set_state(approval_db, key, "failed", lease_id=operation["lease_id"])
        return {"status": "failed", "result": None}
    return {"status": operation["status"], "result": json.loads(operation["result_json"]) if operation["result_json"] else None}


def reconcile_operations(approval_db, store_db):
    """Reconcile the durable approval ledger at MCP startup; never executes writes."""
    if approval_db is None or not Path(approval_db).exists():
        return {"committed": 0, "failed": 0, "pending": 0, "unknown": 0}
    with _connect(approval_db) as connection:
        _ensure_schema(connection)
        keys = [row[0] for row in connection.execute(
            "SELECT idempotency_key FROM write_operations WHERE status IN ('pending','unknown','committed')").fetchall()]
    counts = {"committed": 0, "failed": 0, "pending": 0, "unknown": 0}
    for key in keys:
        state = reconcile_operation(approval_db, store_db, key)["status"]
        counts[state] += 1
    return counts


def consume_approval(
    approval_db, *, approval_id: str | None, customer_id: str,
    session_id: str, tool: str, arguments: dict, store_db=None,
):
    """Consume a grant once and reserve its stable operation key.

    `store_db=None` retains the small legacy unit-test helper contract. The MCP
    write path always supplies store_db and receives an execute/replay permit.
    """
    if approval_db is None or approval_id is None:
        raise WriteApprovalRequired("A valid operation-bound approval is required")
    validate_text(approval_id, "approval_id", 128)
    validate_text(customer_id, "customer_id", 128)
    validate_text(session_id, "session_id", 128)
    payload, key, fingerprint = _binding(customer_id, session_id, tool, arguments)
    if store_db is None:
        with _connect(approval_db) as connection:
            _ensure_schema(connection)
            cursor = connection.execute(
                "UPDATE write_approvals SET consumed_at=? WHERE approval_id=? AND customer_id=? "
                "AND session_id=? AND tool=? AND payload=? AND consumed_at IS NULL AND expires_at>?",
                (time.time(), approval_id, customer_id, session_id, tool, payload, time.time()),
            )
            if cursor.rowcount != 1:
                raise WriteApprovalRequired("A valid operation-bound approval is required")
        return None

    for _ in range(3):
        now = time.time()
        needs_reconcile = False
        with _connect(approval_db) as connection:
            _ensure_schema(connection)
            grant = connection.execute("SELECT * FROM write_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if not grant or any(grant[k] != v for k, v in {
                "customer_id": customer_id, "session_id": session_id, "tool": tool,
                "payload": payload, "idempotency_key": key,
            }.items()):
                raise WriteApprovalRequired("A valid operation-bound approval is required")
            operation = connection.execute("SELECT * FROM write_operations WHERE idempotency_key=?", (key,)).fetchone()
            if operation:
                binding = (operation["customer_id"] == customer_id and operation["session_id"] == session_id
                           and operation["tool"] == tool and operation["payload_hash"] == fingerprint)
                if not binding:
                    raise WriteOperationUnknown("Stored operation binding does not match")
                if operation["status"] == "committed":
                    needs_reconcile = True
                if operation["status"] == "pending":
                    if (operation["lease_until"] or 0) > now:
                        raise WriteOperationPending("Operation is already in progress")
                    needs_reconcile = True
                elif operation["status"] == "unknown":
                    needs_reconcile = True
                elif operation["status"] == "failed" and grant["consumed_at"] is not None:
                    raise WriteOperationFailed("The consumed approval did not commit")
            if needs_reconcile:
                pass
            elif grant["consumed_at"] is not None:
                # A consumed legacy grant without an operation ledger is ambiguous;
                # never spend it again based only on missing Python state.
                raise WriteOperationUnknown("Consumed approval has no reconcilable operation")
            elif grant["expires_at"] <= now:
                raise WriteApprovalRequired("A valid operation-bound approval is required")
            else:
                lease_id = uuid4().hex
                lease_until = now + LEASE_SECONDS
                if operation is None:
                    connection.execute(
                        "INSERT INTO write_operations(idempotency_key,customer_id,session_id,tool,payload_hash,status,lease_id,lease_until,updated_at) "
                        "VALUES(?,?,?,?,?,'pending',?,?,?)",
                        (key, customer_id, session_id, tool, fingerprint, lease_id, lease_until, now),
                    )
                else:
                    connection.execute(
                        "UPDATE write_operations SET status='pending',lease_id=?,lease_until=?,result_json=NULL,updated_at=? "
                        "WHERE idempotency_key=? AND status='failed'",
                        (lease_id, lease_until, now, key),
                    )
                cursor = connection.execute(
                    "UPDATE write_approvals SET consumed_at=? WHERE approval_id=? AND consumed_at IS NULL AND expires_at>?",
                    (now, approval_id, now),
                )
                if cursor.rowcount != 1:
                    raise WriteApprovalRequired("A valid operation-bound approval is required")
                return {"state": "execute", "idempotency_key": key, "lease_id": lease_id,
                        "customer_id": customer_id, "session_id": session_id,
                        "tool": tool, "payload_hash": fingerprint}
        # Reconcile outside the approval transaction, then re-read both ledgers.
        reconciled = reconcile_operation(approval_db, store_db, key)
        state = reconciled["status"]
        if state == "committed":
            return {"state": "committed", "idempotency_key": key,
                    "result": reconciled["result"]}
        if state == "pending":
            raise WriteOperationPending("Operation is still in progress")
        if state == "unknown":
            raise WriteOperationUnknown("Operation result cannot yet be verified")
    raise WriteOperationUnknown("Operation reconciliation did not settle")


def settle_operation(approval_db, permit, result):
    _set_state(approval_db, permit["idempotency_key"], "committed", result=result)


def fail_operation(approval_db, permit):
    _set_state(approval_db, permit["idempotency_key"], "failed", lease_id=permit["lease_id"])


def mark_operation_unknown(approval_db, permit):
    _set_state(approval_db, permit["idempotency_key"], "unknown", lease_id=permit["lease_id"])
