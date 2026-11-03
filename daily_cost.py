"""Atomic, persistent global API admission ledger. No customer content/secrets.

Days follow Asia/Riyadh. Unknown requests retain their reservations, including
across midnight/restarts, until trusted operator reconciliation. Never expire an
uncertain bill automatically. This is an admission estimate, not a billing cap.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "daily_api_cost.sqlite3"
SCALE = Decimal(10**12)


def units(amount):
    value = Decimal(str(amount))
    if not value.is_finite() or value < 0:
        raise ValueError("Invalid cost")
    return int((value * SCALE).to_integral_value(rounding=ROUND_CEILING))


class DailyCostLedger:
    def __init__(self, path=None, *, clock=None):
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Riyadh")).date().isoformat())

    @contextmanager
    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS api_cost_requests ("
                       "request_id TEXT PRIMARY KEY, day TEXT NOT NULL, reserved INTEGER NOT NULL, "
                       "actual INTEGER, status TEXT NOT NULL CHECK(status IN ('reserved','unknown','settled')))")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _total(db, day):
        return db.execute("SELECT COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE actual END),0) "
                          "FROM api_cost_requests WHERE day=? OR status!='settled'", (day,)).fetchone()[0]

    def reserve(self, request_id, amount, limit):
        with self._connection() as db:
            day = self.clock()
            if self._total(db, day) + units(amount) > units(limit):
                return False
            db.execute("INSERT INTO api_cost_requests VALUES(?,?,?,NULL,'reserved')",
                       (request_id, day, units(amount)))
            return True

    def settle(self, request_id, actual):
        with self._connection() as db:
            cursor = db.execute("UPDATE api_cost_requests SET actual=?,status=? WHERE request_id=? AND status='reserved'",
                                (units(actual) if actual is not None else None,
                                 "settled" if actual is not None else "unknown", request_id))
            if cursor.rowcount != 1:
                raise ValueError("Request already settled or missing")

    def cancel_unsent(self, request_id):
        with self._connection() as db:
            db.execute("DELETE FROM api_cost_requests WHERE request_id=? AND status='reserved'", (request_id,))

    def snapshot(self):
        with self._connection() as db:
            day = self.clock()
            return {"day": day, "timezone": "Asia/Riyadh", "committed_usd": str(Decimal(self._total(db, day)) / SCALE),
                    "unresolved_requests": db.execute("SELECT count(*) FROM api_cost_requests WHERE status!='settled'").fetchone()[0]}
