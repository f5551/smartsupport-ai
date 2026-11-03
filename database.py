"""SQLite persistence; imports never create a database.

Identity must come from a trusted application session, not model claims.
Write approval is a trusted-caller contract, not an authentication system.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from security import require_write_approval, validate_text
from write_idempotency import payload_hash, valid_idempotency_key

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "smartsupport.db"
DEFAULT_SEED_PATH = PROJECT_ROOT / "data" / "seed_orders.json"
SCHEMA_VERSION = 1


class DataAccessError(PermissionError):
    """Same public error for missing and unauthorized resources."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    status TEXT NOT NULL CHECK (status IN
        ('processing', 'shipped', 'delivered', 'cancelled')),
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (order_id, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    order_id TEXT NOT NULL,
    description TEXT NOT NULL CHECK (length(trim(description)) BETWEEN 1 AND 4000),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (order_id, customer_id) REFERENCES orders(order_id, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_tickets_customer_created
    ON tickets(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_order_owner
    ON tickets(order_id, customer_id);
CREATE TABLE IF NOT EXISTS conversation_memory (
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    session_id TEXT NOT NULL CHECK (length(trim(session_id)) BETWEEN 1 AND 128),
    content TEXT NOT NULL CHECK (length(trim(content)) BETWEEN 1 AND 8000),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (customer_id, session_id)
);
"""

WRITE_OPERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS write_operations (
    idempotency_key TEXT PRIMARY KEY NOT NULL CHECK(length(idempotency_key) = 64),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    session_id TEXT NOT NULL CHECK(length(trim(session_id)) BETWEEN 1 AND 128),
    action TEXT NOT NULL CHECK(action IN ('create_ticket', 'save_memory')),
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    state TEXT NOT NULL CHECK(state IN ('committed')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


def _ensure_write_operations(connection):
    connection.execute(WRITE_OPERATIONS_SCHEMA)


@contextmanager
def connect(db_path: str | Path = DEFAULT_DB_PATH, *, immediate=False):
    """FK enforcement, transaction commit/rollback and guaranteed close.

    Opens existing databases only; initialize explicitly first. Pass a file path,
    not :memory:, because every operation uses a separate connection.
    """
    path = Path(db_path).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if immediate:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_write_operations(connection)
        with connection:
            yield connection
    finally:
        connection.close()


def init_database(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create versioned schema without loading demo data or resetting records."""
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise ValueError("Unsupported database schema version")
        # Static SQL only; future schema versions require explicit migrations.
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def seed_demo_data(
    db_path: str | Path = DEFAULT_DB_PATH,
    *, seed_path: str | Path = DEFAULT_SEED_PATH,
) -> None:
    """Atomic demo import: stable IDs avoid duplicates and preserve changes.

    Does not clear data. Only conflicts on the record's primary key are ignored;
    invalid ownership or other constraints roll back the entire import.
    """
    payload = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    if payload.get("synthetic") is not True or payload.get("schema_version") != 1:
        raise ValueError("Expected synthetic seed data with schema_version=1")
    with connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO customers(customer_id, display_name, email) VALUES (?, ?, ?) "
            "ON CONFLICT(customer_id) DO NOTHING",
            [(c["customer_id"], c["display_name"], c["email"])
             for c in payload["customers"]],
        )
        connection.executemany(
            "INSERT INTO orders(order_id, customer_id, status, items_json) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(order_id) DO NOTHING",
            [(o["order_id"], o["customer_id"], o["status"],
              json.dumps(o["items"], ensure_ascii=False)) for o in payload["orders"]],
        )
        connection.executemany(
            "INSERT INTO tickets(ticket_id, customer_id, order_id, description, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticket_id) DO NOTHING",
            [(t["ticket_id"], t["customer_id"], t["order_id"], t["description"],
              t["status"], t["created_at"]) for t in payload["tickets"]],
        )


def _identity(customer_id: str) -> str:
    return validate_text(customer_id, "customer_id", 128)


def _deny() -> None:
    raise DataAccessError("Resource unavailable or access denied")


def get_order_status(customer_id: str, order_id: str, *, db_path=DEFAULT_DB_PATH) -> dict:
    customer_id = _identity(customer_id)
    order_id = validate_text(order_id, "order_id", 128)
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT order_id, status, created_at FROM orders "
            "WHERE customer_id = ? AND order_id = ?", (customer_id, order_id),
        ).fetchone()
        if row is None:
            _deny()
        return dict(row)


def create_ticket(
    customer_id: str, order_id: str, description: str,
    *, approved: bool = False, db_path=DEFAULT_DB_PATH,
    idempotency_key: str | None = None, session_id: str | None = None,
) -> dict:
    """Trusted MCP path: a consumed application grant is required.

    With an idempotency key, the ticket and committed result share one SQLite
    transaction. A retry returns the recorded result and cannot insert again.
    """
    require_write_approval(approved)
    customer_id = _identity(customer_id)
    order_id = validate_text(order_id, "order_id", 128)
    description = validate_text(description, "description", 4000)
    if idempotency_key is not None:
        if not valid_idempotency_key(idempotency_key) or session_id is None:
            raise ValueError("Invalid write operation identity")
        session_id = validate_text(session_id, "session_id", 128)
        fingerprint = payload_hash({"order_id": order_id, "description": description})
    ticket_id = "TKT-" + uuid4().hex
    with connect(db_path, immediate=idempotency_key is not None) as connection:
        if idempotency_key is not None:
            prior = connection.execute(
                "SELECT customer_id, session_id, action, payload_hash, state, result_json "
                "FROM write_operations WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
            if prior:
                if (prior["customer_id"] != customer_id or prior["session_id"] != session_id
                        or prior["action"] != "create_ticket" or prior["payload_hash"] != fingerprint):
                    _deny()
                if prior["state"] == "committed":
                    return json.loads(prior["result_json"])
        # Ownership check and insertion are one atomic statement; the composite
        # FK independently prevents tickets from belonging to another owner.
        cursor = connection.execute(
            "INSERT INTO tickets(ticket_id, customer_id, order_id, description) "
            "SELECT ?, customer_id, order_id, ? FROM orders "
            "WHERE customer_id = ? AND order_id = ?",
            (ticket_id, description, customer_id, order_id),
        )
        if cursor.rowcount != 1:
            _deny()
        row = connection.execute(
            "SELECT ticket_id, order_id, description, status, created_at "
            "FROM tickets WHERE customer_id = ? AND ticket_id = ?",
            (customer_id, ticket_id),
        ).fetchone()
        result = dict(row)
        if idempotency_key is not None:
            connection.execute(
                "INSERT INTO write_operations(idempotency_key,customer_id,session_id,action,payload_hash,state,result_json) "
                "VALUES(?,?,?,?,?,'committed',?)",
                (idempotency_key, customer_id, session_id, "create_ticket", fingerprint,
                 json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
        return result


def get_ticket_status(customer_id: str, ticket_id: str, *, db_path=DEFAULT_DB_PATH) -> dict:
    customer_id = _identity(customer_id)
    ticket_id = validate_text(ticket_id, "ticket_id", 128)
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT ticket_id, order_id, description, status, created_at FROM tickets "
            "WHERE customer_id = ? AND ticket_id = ?", (customer_id, ticket_id),
        ).fetchone()
        if row is None:
            _deny()
        return dict(row)


def get_write_operation(idempotency_key: str, *, db_path=DEFAULT_DB_PATH) -> dict | None:
    """Read the durable result used to reconcile a separately stored approval."""
    if not valid_idempotency_key(idempotency_key):
        raise ValueError("Invalid write operation identity")
    with connect(db_path, immediate=True) as connection:
        row = connection.execute(
            "SELECT idempotency_key,customer_id,session_id,action,payload_hash,state,result_json "
            "FROM write_operations WHERE idempotency_key=?", (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        return result


def save_memory(
    customer_id: str, session_id: str, content: str,
    *, approved: bool = False, db_path=DEFAULT_DB_PATH,
    idempotency_key: str | None = None,
) -> dict:
    """Save the latest approved summary per customer-scoped session."""
    require_write_approval(approved)
    customer_id = _identity(customer_id)
    session_id = validate_text(session_id, "session_id", 128)
    content = validate_text(content, "content", 8000)
    if idempotency_key is not None and not valid_idempotency_key(idempotency_key):
        raise ValueError("Invalid write operation identity")
    fingerprint = payload_hash({"content": content}) if idempotency_key is not None else None
    with connect(db_path, immediate=idempotency_key is not None) as connection:
        if idempotency_key is not None:
            prior = connection.execute(
                "SELECT customer_id,session_id,action,payload_hash,state,result_json "
                "FROM write_operations WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if prior:
                if (prior["customer_id"] != customer_id or prior["session_id"] != session_id
                        or prior["action"] != "save_memory" or prior["payload_hash"] != fingerprint):
                    _deny()
                if prior["state"] == "committed":
                    return json.loads(prior["result_json"])
        cursor = connection.execute(
            "INSERT INTO conversation_memory(customer_id, session_id, content) "
            "SELECT customer_id, ?, ? FROM customers WHERE customer_id = ? "
            "ON CONFLICT(customer_id, session_id) DO UPDATE SET "
            "content = excluded.content, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (session_id, content, customer_id),
        )
        if cursor.rowcount != 1:
            _deny()
        result = {"saved": True, "session_id": session_id}
        if idempotency_key is not None:
            connection.execute(
                "INSERT INTO write_operations(idempotency_key,customer_id,session_id,action,payload_hash,state,result_json) "
                "VALUES(?,?,?,'save_memory',?,'committed',?)",
                (idempotency_key, customer_id, session_id, fingerprint,
                 json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
        return result
def load_memory(customer_id: str, session_id: str, *, db_path=DEFAULT_DB_PATH) -> dict | None:
    customer_id = _identity(customer_id)
    session_id = validate_text(session_id, "session_id", 128)
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT session_id, content, created_at, updated_at FROM conversation_memory "
            "WHERE customer_id = ? AND session_id = ?", (customer_id, session_id),
        ).fetchone()
        return dict(row) if row is not None else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize SmartSupport SQLite explicitly")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--seed", action="store_true", help="Load synthetic demo records")
    args = parser.parse_args()
    init_database(args.db)
    if args.seed:
        seed_demo_data(args.db)
    print(f"Database initialized: {args.db.resolve()} (demo seed: {args.seed})")


if __name__ == "__main__":
    main()
