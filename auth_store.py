"""Local account authentication for synthetic SmartSupport test accounts.

Passwords use salted PBKDF2-HMAC-SHA256 (600,000 rounds); sessions are random
bearer tokens whose SHA-256 digests alone are persisted. This is local/demo
authentication, not a substitute for an identity provider in production.
"""
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time
import unicodedata


ROUNDS = 600_000
SESSION_SECONDS = 8 * 60 * 60
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
CUSTOMER_IDS = {f"demo_customer_{i:03d}" for i in range(1, 6)}


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str
    customer_id: str | None


class AuthError(Exception):
    """Generic authentication failure; does not disclose account existence."""


def _username(value):
    if not isinstance(value, str):
        raise ValueError("Invalid username")
    result = unicodedata.normalize("NFKC", value).strip().casefold()
    if not 3 <= len(result) <= 64 or any(c.isspace() for c in result):
        raise ValueError("Username must be 3-64 characters without spaces")
    return result


def _password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256 or len(password.encode("utf-8")) > 1024:
        raise ValueError("Password must be 12-256 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS)
    return f"pbkdf2_sha256${ROUNDS}${salt.hex()}${digest.hex()}"


def _verify(password, encoded):
    try:
        if not isinstance(password,str) or not 12 <= len(password) <= 256 or len(password.encode("utf-8")) > 1024:
            return False
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != ROUNDS:
            return False
        actual = _password_hash(password, bytes.fromhex(salt)).split("$")[-1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


class AuthStore:
    def __init__(self, directory, *, clock=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "auth.sqlite3"
        self.clock = clock or time.time
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('customer','operator')),
                  customer_id TEXT UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
                  created_at REAL NOT NULL,
                  CHECK((role='customer' AND customer_id IS NOT NULL) OR
                        (role='operator' AND customer_id IS NULL)));
                CREATE TABLE IF NOT EXISTS login_attempts(
                  username TEXT PRIMARY KEY, window_start REAL NOT NULL,
                  failures INTEGER NOT NULL, locked_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions(
                  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                  created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL);
                CREATE TABLE IF NOT EXISTS system_settings(
                  name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL,
                  updated_by INTEGER NOT NULL REFERENCES users(id));
            """)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _db(self, *, write=True):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        return _Transaction(db)

    def create_user(self, username, password, *, role="customer", customer_id=None):
        username = _username(username)
        if role == "customer":
            if customer_id not in CUSTOMER_IDS:
                raise ValueError("Customer account must use a synthetic customer ID")
        elif role == "operator":
            if customer_id is not None:
                raise ValueError("Operators cannot have customer data ownership")
        else:
            raise ValueError("Invalid role")
        password_hash = _password_hash(password)
        with self._db() as db:
            db.execute("INSERT INTO users(username,password_hash,role,customer_id,created_at) VALUES(?,?,?,?,?)",
                       (username,password_hash,role,customer_id,self.clock()))
        return True

    def login(self, username, password):
        username = _username(username)
        now = self.clock()
        failure=False
        result=None
        with self._db() as db:
            attempt = db.execute("SELECT * FROM login_attempts WHERE username=?", (username,)).fetchone()
            if attempt and attempt["locked_until"] > now:
                failure=True
            else:
                row = db.execute("SELECT * FROM users WHERE username=? AND enabled=1", (username,)).fetchone()
                valid = _verify(password, row["password_hash"]) if row and isinstance(password, str) else False
                if not valid:
                    start = attempt["window_start"] if attempt and now - attempt["window_start"] < LOCK_SECONDS else now
                    failures = (attempt["failures"] if attempt and start == attempt["window_start"] else 0) + 1
                    locked = now + LOCK_SECONDS if failures >= MAX_FAILURES else 0
                    db.execute("INSERT INTO login_attempts VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET window_start=excluded.window_start, failures=excluded.failures, locked_until=excluded.locked_until",
                               (username,start,failures,locked))
                    failure=True
                else:
                    db.execute("DELETE FROM login_attempts WHERE username=?", (username,))
                    token = secrets.token_urlsafe(48)
                    db.execute("INSERT INTO sessions VALUES(?,?,?, ?,NULL)",
                               (hashlib.sha256(token.encode()).hexdigest(),row["id"],now,now+SESSION_SECONDS))
                    result=(token, Principal(row["id"],row["username"],row["role"],row["customer_id"]))
        if failure:
            raise AuthError("Login failed")
        return result

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise AuthError("Login required")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._db(write=False) as db:
            row = db.execute("SELECT u.*,s.expires_at,s.revoked_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_hash,)).fetchone()
            if not row or row["revoked_at"] is not None or row["expires_at"] <= self.clock() or not row["enabled"]:
                raise AuthError("Login required")
            return Principal(row["id"],row["username"],row["role"],row["customer_id"])

    def logout(self, token):
        if not isinstance(token, str):
            return
        with self._db() as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                       (self.clock(),hashlib.sha256(token.encode()).hexdigest()))

    def get_live_enabled(self, token):
        principal=self.authenticate(token)
        if principal.role != "operator":
            raise PermissionError("Operator role required")
        with self._db() as db:
            row=db.execute("SELECT value FROM system_settings WHERE name='live_enabled'").fetchone()
            return bool(row and row["value"]=="1")

    def live_is_enabled(self):
        with self._db(write=False) as db:
            row=db.execute("SELECT value FROM system_settings WHERE name='live_enabled'").fetchone()
            return bool(row and row["value"]=="1")

    def set_live_enabled(self, token, enabled):
        principal=self.authenticate(token)
        if principal.role != "operator" or not isinstance(enabled,bool):
            raise PermissionError("Operator role required")
        with self._db() as db:
            db.execute("INSERT INTO system_settings(name,value,updated_at,updated_by) VALUES('live_enabled',?, ?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                       ("1" if enabled else "0",self.clock(),principal.user_id))


class _Transaction:
    def __init__(self, db): self.db = db
    def __enter__(self): return self.db
    def __exit__(self, typ, value, tb):
        try:
            self.db.rollback() if typ else self.db.commit()
        finally:
            self.db.close()
