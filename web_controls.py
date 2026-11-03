"""Server-side persistent request limits and one-shot UI decisions (not auth)."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import time


class WebError(Exception):
    """Only stable codes cross the UI boundary; never exception messages."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class WebControls:
    def __init__(self, path, *, per_minute=10, per_session=30, global_per_hour=120):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = per_minute, per_session, global_per_hour
        with self.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS web_requests(visitor TEXT NOT NULL, created_at REAL NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_web_requests_visitor_time ON web_requests(visitor,created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_web_requests_time ON web_requests(created_at)")
            con.execute("CREATE TABLE IF NOT EXISTS web_decisions(action TEXT PRIMARY KEY, created_at REAL NOT NULL)")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=10)
        try:
            with con:
                yield con
        finally:
            con.close()

    def consume(self, visitor):
        identity = hashlib.sha256(visitor.encode()).hexdigest()
        now = time.time()
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            recent = con.execute("SELECT count(*) FROM web_requests WHERE visitor=? AND created_at>?", (identity, now-60)).fetchone()[0]
            total = con.execute("SELECT count(*) FROM web_requests WHERE visitor=?", (identity,)).fetchone()[0]
            global_count = con.execute("SELECT count(*) FROM web_requests WHERE created_at>?", (now-3600,)).fetchone()[0]
            if recent >= self.limits[0] or total >= self.limits[1] or global_count >= self.limits[2]:
                raise WebError("rate_limit")
            con.execute("INSERT INTO web_requests VALUES(?,?)", (identity, now))

    def claim_decision(self, conversation_id, pending_id):
        action = hashlib.sha256((conversation_id + ":" + pending_id).encode()).hexdigest()
        with self.connection() as con:
            try:
                con.execute("INSERT INTO web_decisions VALUES(?,?)", (action, time.time()))
            except sqlite3.IntegrityError:
                raise WebError("already_handled") from None
