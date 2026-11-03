import json
import sqlite3

import pytest

from database import (
    DataAccessError, DEFAULT_SEED_PATH, connect, create_ticket,
    get_order_status, get_ticket_status, init_database, load_memory,
    save_memory, seed_demo_data,
)
from security import WriteApprovalRequired

C1 = "demo_customer_001"
C2 = "demo_customer_002"


def counts(path):
    with connect(path) as connection:
        # Table names are hard-coded, never user inputs.
        return (
            connection.execute("SELECT count(*) FROM customers").fetchone()[0],
            connection.execute("SELECT count(*) FROM orders").fetchone()[0],
            connection.execute("SELECT count(*) FROM tickets").fetchone()[0],
            connection.execute("SELECT count(*) FROM conversation_memory").fetchone()[0],
        )


def test_seed_counts_and_ownership_distribution(db_path):
    assert counts(db_path) == (5, 10, 3, 0)
    with connect(db_path) as connection:
        assert [r[0] for r in connection.execute(
            "SELECT count(*) FROM orders GROUP BY customer_id"
        )] == [2] * 5
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_owned_order(db_path):
    result = get_order_status(C1, "DEMO-1001", db_path=db_path)
    assert result["order_id"] == "DEMO-1001"
    assert result["status"] == "shipped"


@pytest.mark.parametrize("order_id", ["DEMO-2001", "NOT-FOUND", "' OR 1=1 --"])
def test_unavailable_orders_have_same_error(db_path, order_id):
    with pytest.raises(DataAccessError, match="^Resource unavailable or access denied$"):
        get_order_status(C1, order_id, db_path=db_path)


def test_injected_customer_id_cannot_bypass_ownership(db_path):
    with pytest.raises(DataAccessError):
        get_order_status("' OR 1=1 --", "DEMO-1001", db_path=db_path)


def test_create_and_fetch_ticket(db_path):
    ticket = create_ticket(C1, "DEMO-1002", "تلف في الشاشة", approved=True, db_path=db_path)
    assert ticket["ticket_id"].startswith("TKT-")
    assert ticket["status"] == "open"
    assert ticket["created_at"].endswith("Z")
    assert get_ticket_status(C1, ticket["ticket_id"], db_path=db_path) == ticket


@pytest.mark.parametrize("ticket_id", ["DEMO-TKT-002", "NOT-FOUND", "' OR 1=1 --"])
def test_unavailable_tickets_have_same_error(db_path, ticket_id):
    with pytest.raises(DataAccessError, match="^Resource unavailable or access denied$"):
        get_ticket_status(C1, ticket_id, db_path=db_path)


def test_new_ticket_not_visible_to_other_customer(db_path):
    ticket = create_ticket(C1, "DEMO-1002", "خلل تجريبي", approved=True, db_path=db_path)
    with pytest.raises(DataAccessError):
        get_ticket_status(C2, ticket["ticket_id"], db_path=db_path)


@pytest.mark.parametrize("order_id", ["DEMO-2001", "MISSING", "' OR 1=1 --"])
def test_cannot_create_ticket_for_unowned_or_missing_order(db_path, order_id):
    before = counts(db_path)
    with pytest.raises(DataAccessError):
        create_ticket(C1, order_id, "شكوى", approved=True, db_path=db_path)
    assert counts(db_path) == before


@pytest.mark.parametrize("approved", [False, None, 1, "true"])
def test_write_approval_must_be_explicit_boolean(db_path, approved):
    before = counts(db_path)
    with pytest.raises(WriteApprovalRequired):
        create_ticket(C1, "DEMO-1002", "شكوى", approved=approved, db_path=db_path)
    with pytest.raises(WriteApprovalRequired):
        save_memory(C1, "session", "ملخص", approved=approved, db_path=db_path)
    assert counts(db_path) == before


def test_omitted_approval_rejects_writes(db_path):
    with pytest.raises(WriteApprovalRequired):
        create_ticket(C1, "DEMO-1002", "شكوى", db_path=db_path)
    with pytest.raises(WriteApprovalRequired):
        save_memory(C1, "session", "ملخص", db_path=db_path)


def test_memory_survives_closed_connections(db_path):
    save_memory(C1, "session-1", "يفضل العميل التواصل بالعربية", approved=True, db_path=db_path)
    # save_memory has already committed and closed its connection.
    with connect(db_path) as reopened:
        assert reopened.execute(
            "SELECT content FROM conversation_memory WHERE customer_id=? AND session_id=?",
            (C1, "session-1"),
        ).fetchone()[0] == "يفضل العميل التواصل بالعربية"
    assert load_memory(C1, "session-1", db_path=db_path)["content"] == "يفضل العميل التواصل بالعربية"


def test_memory_is_scoped_by_customer_and_session(db_path):
    save_memory(C1, "same-session", "ملخص خاص بالأول", approved=True, db_path=db_path)
    assert load_memory(C2, "same-session", db_path=db_path) is None
    assert load_memory(C1, "another-session", db_path=db_path) is None
    save_memory(C2, "same-session", "ملخص خاص بالثاني", approved=True, db_path=db_path)
    assert load_memory(C1, "same-session", db_path=db_path)["content"] == "ملخص خاص بالأول"
    assert load_memory(C2, "same-session", db_path=db_path)["content"] == "ملخص خاص بالثاني"


def test_memory_update_and_sql_payload_are_literal(db_path):
    payload = "'); DROP TABLE orders; --"
    save_memory(C1, "s", "قديم", approved=True, db_path=db_path)
    save_memory(C1, "s", payload, approved=True, db_path=db_path)
    assert load_memory(C1, "s", db_path=db_path)["content"] == payload
    ticket = create_ticket(C1, "DEMO-1002", payload, approved=True, db_path=db_path)
    assert get_ticket_status(C1, ticket["ticket_id"], db_path=db_path)["description"] == payload
    assert counts(db_path) == (5, 10, 4, 1)


def test_memory_requires_existing_customer(db_path):
    with pytest.raises(DataAccessError):
        save_memory("missing", "s", "ملخص", approved=True, db_path=db_path)
    assert counts(db_path)[3] == 0


def test_reinitialization_is_idempotent_and_preserves_changes(db_path):
    save_memory(C1, "s", "ملخص محفوظ", approved=True, db_path=db_path)
    ticket = create_ticket(C1, "DEMO-1002", "جديدة", approved=True, db_path=db_path)
    with connect(db_path) as connection:
        connection.execute("UPDATE orders SET status=? WHERE order_id=?", ("delivered", "DEMO-1001"))
        connection.execute("UPDATE tickets SET status=? WHERE ticket_id=?", ("closed", "DEMO-TKT-001"))
    before = counts(db_path)
    for _ in range(2):
        init_database(db_path)
        seed_demo_data(db_path)
    assert counts(db_path) == before == (5, 10, 4, 1)
    assert get_order_status(C1, "DEMO-1001", db_path=db_path)["status"] == "delivered"
    assert get_ticket_status(C1, "DEMO-TKT-001", db_path=db_path)["status"] == "closed"
    assert get_ticket_status(C1, ticket["ticket_id"], db_path=db_path)["description"] == "جديدة"
    assert load_memory(C1, "s", db_path=db_path)["content"] == "ملخص محفوظ"


def test_composite_foreign_key_blocks_wrong_ticket_owner(db_path):
    with connect(db_path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tickets(ticket_id, customer_id, order_id, description) VALUES(?, ?, ?, ?)",
                ("invalid", C1, "DEMO-2001", "wrong owner"),
            )


def test_customer_foreign_key_blocks_unknown_order_owner(db_path):
    with connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO orders(order_id, customer_id, status, items_json) VALUES(?, ?, ?, ?)",
                ("invalid", "missing", "processing", "[]"),
            )


def test_invalid_seed_rolls_back_entire_import(tmp_path):
    path = tmp_path / "rollback.sqlite3"
    init_database(path)
    payload = json.loads(DEFAULT_SEED_PATH.read_text(encoding="utf-8"))
    payload["tickets"][-1]["customer_id"] = C1  # Wrong owner, after earlier valid records.
    seed = tmp_path / "invalid-seed.json"
    seed.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(sqlite3.IntegrityError):
        seed_demo_data(path, seed_path=seed)
    assert counts(path) == (0, 0, 0, 0)


@pytest.mark.parametrize("description", ["", "   ", "x" * 4001, "nul\x00byte", None])
def test_invalid_ticket_description_does_not_write(db_path, description):
    with pytest.raises(ValueError):
        create_ticket(C1, "DEMO-1002", description, approved=True, db_path=db_path)
    assert counts(db_path)[2] == 3


def test_uninitialized_database_is_not_created_by_read(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        get_order_status(C1, "DEMO-1001", db_path=path)
    assert not path.exists()
