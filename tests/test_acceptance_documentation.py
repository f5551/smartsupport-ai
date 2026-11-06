"""Validate closeout calculations and docs offline, using preserved evidence."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from daily_cost import DailyCostLedger

ROOT = Path(__file__).resolve().parents[1]
MUTABLE_RUNTIME_FILES = {"data/daily_api_cost.sqlite3"}


def evidence():
    return json.loads((ROOT / "reports/evidence/live_acceptance_closeout.json").read_text(encoding="utf-8"))


def assert_cost_ledger_integrity(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute(
            "SELECT COUNT(*) FROM api_cost_requests WHERE reserved < 0 "
            "OR (actual IS NOT NULL AND actual < 0) "
            "OR (status='settled' AND actual IS NULL) "
            "OR (status IN ('reserved','unknown') AND actual IS NOT NULL)"
        ).fetchone() == (0,)


def test_latest_success_selection_uses_timestamp_including_later_ambiguous_and_ticket():
    record = evidence()
    latest = {}
    for attempt in sorted(record["attempts"], key=lambda a: a["timestamp"]):
        if attempt["status"] == "PASS":
            assert attempt["stop_reason"] == "final_answer"
            latest[attempt["scenario"]] = attempt
    assert set(latest) == {"shipping", "order_status", "create_ticket", "memory_followup", "ambiguous"}
    assert {a["scenario"]: a for a in record["latest_successes"]} == latest
    assert latest["create_ticket"]["source"].startswith("logs/ticket_retest_")
    assert any(a["scenario"] == "create_ticket" and a["status"] == "FAIL" for a in record["attempts"])
    assert len([a for a in record["attempts"] if a["scenario"] == "ambiguous" and a["status"] == "PASS"]) == 2


def test_actual_token_costs_and_success_failure_totals_reconcile():
    record = evidence()
    summary = record["summary"]
    total = success = failure = Decimal(0)
    for attempt in record["attempts"]:
        calculated = ((Decimal(attempt["input_tokens"] - attempt["cached_tokens"]) * Decimal(".75")
                       + Decimal(attempt["cached_tokens"]) * Decimal(".075")
                       + Decimal(attempt["output_tokens"]) * Decimal("4.50")) / Decimal(1000000))
        assert calculated == Decimal(attempt["cost_usd"])
        total += calculated
        if attempt["status"] == "PASS":
            success += calculated
        else:
            failure += calculated
    selected = sum((Decimal(a["cost_usd"]) for a in record["latest_successes"]), Decimal(0))
    assert total == success + failure == Decimal(summary["all_settled_cost_usd"])
    assert success == Decimal(summary["all_success_cost_usd"])
    assert failure == Decimal(summary["failed_cost_usd"])
    assert selected == Decimal(summary["selected_success_cost_usd"])
    assert selected / len(record["latest_successes"]) == Decimal(summary["selected_success_average_usd"])
    assert summary["selected_count"] == 5
    assert sum(a["status"] == "FAIL" for a in record["attempts"]) == summary["failed_count"] == 7
    assert len(record["attempts"]) == summary["all_settled_attempt_count"] == 13
    assert Decimal(summary["prior_unknown_reserved_usd"]) == Decimal(".002445")


def test_both_documents_show_latest_successes_and_separate_failed_costs():
    record = evidence()
    summary = record["summary"]
    for relative in ("reports/live_acceptance.md", "cost_analysis.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        for key in ("selected_success_cost_usd", "selected_success_average_usd", "all_success_cost_usd",
                    "failed_cost_usd", "all_settled_cost_usd", "prior_unknown_reserved_usd"):
            assert summary[key] in text
        for attempt in record["latest_successes"]:
            amount = f"{Decimal(attempt['cost_usd']):.8f}"
            rows = [line for line in text.splitlines() if line.startswith("|") and amount in line]
            assert any("PASS" in row and str(attempt["input_tokens"]) in row
                       and str(attempt["output_tokens"]) in row for row in rows)
        assert "إعادة اختبار مستقلة" in text
        assert "PermissionError" in text
        assert "0.00129600" not in text  # Superseded mixed success/failure average.


def test_existing_evidence_and_failure_logs_retain_pre_edit_fingerprints():
    # Frozen evidence uses byte fingerprints. The runtime cost ledger is mutable:
    # validate SQLite/accounting invariants instead of pinning it to an old digest.
    for relative, expected in evidence()["source_sha256"].items():
        source = ROOT / relative
        if source.exists() and relative not in MUTABLE_RUNTIME_FILES:
            assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, relative
    runtime_ledger = ROOT / "data/daily_api_cost.sqlite3"
    if runtime_ledger.exists():
        assert_cost_ledger_integrity(runtime_ledger)
    original = json.loads((ROOT / "reports/live_acceptance_raw.json").read_text(encoding="utf-8"))
    assert next(a for a in original["results"] if a["id"] == "create_ticket")["status"] == "FAIL"
    retest = json.loads((ROOT / "reports/ticket_retest_dd43cfb703db46edba7be2c308887753.json").read_text(encoding="utf-8"))
    assert retest["status"] == "PASS" and all(retest["checks"].values())


def test_normal_cost_ledger_writes_change_digest_but_preserve_unknown_reservation(tmp_path):
    path = tmp_path / "daily_api_cost.sqlite3"
    ledger = DailyCostLedger(path, clock=lambda: "2026-11-06")
    assert ledger.reserve("uncertain", Decimal("0.002445"), Decimal("0.10"))
    ledger.settle("uncertain", None)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    assert ledger.reserve("settled", Decimal("0.001"), Decimal("0.10"))
    ledger.settle("settled", Decimal("0.0005"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() != before

    assert_cost_ledger_integrity(path)
    with sqlite3.connect(path) as db:
        unknown = db.execute(
            "SELECT COUNT(*), SUM(reserved), SUM(actual) FROM api_cost_requests WHERE status='unknown'"
        ).fetchone()
        assert unknown == (1, 2_445_000_000, None)
