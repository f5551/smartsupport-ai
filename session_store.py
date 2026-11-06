"""Private UI journal, not a store/MCP database or production identity provider.

Authenticated application recovery is bound to user_id/customer_id. Legacy
anonymous recovery remains an internal compatibility path and is not exposed in
the customer UI. Task snapshots are never executable continuations. One OS-locked
backend owns a directory; SQLite transactions serialize its threads.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from web_controls import WebError


RECOVERY_MESSAGE = "انقطع التنفيذ أو انتهت صلاحية استكماله؛ لم نؤكد أي كتابة. راجع حالتها ثم ابدأ إجراءً جديدًا بموافقة جديدة."


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def stopped_result(prior, task, reason, *, cancelled):
    result={'task_id':task,'tools_used':[],'turns':0,'input_tokens':0,
            'output_tokens':0,'estimated_cost_usd':'0','write_receipts':[],
            'cost_summary':{'usage_source':'unverified'}}
    result.update(prior)
    result.update(web_task_id=task,task_status='cancelled' if cancelled else 'failed',
                  stop_reason=reason,message=RECOVERY_MESSAGE,pending_approval=None,requires_new_action=True)
    if not cancelled:
        result.update(usage_complete=False,outcome_unknown=True)
    return result


class DirectoryLease:
    """Fail closed on another live backend; OS releases the lock after a crash."""
    def __init__(self, directory):
        self.file = open(Path(directory) / "session_registry.lock", "a+b")
        try:
            self.file.seek(0, 2)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise WebError("busy") from None

    def close(self):
        if not self.file.closed:
            self.file.close()


class SessionStore:
    def __init__(self, directory, *, clock=None):
        self.path = Path(directory) / "web_sessions.sqlite3"
        self.clock = clock or time.time
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS visitors (
                  id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL,
                  touched REAL NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                  user_id INTEGER, customer_id TEXT);
                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY, visitor TEXT NOT NULL REFERENCES visitors(id),
                  customer TEXT NOT NULL, session TEXT NOT NULL, mode TEXT NOT NULL,
                  active INTEGER NOT NULL, messages TEXT NOT NULL, result TEXT, current_task TEXT);
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, conversation TEXT NOT NULL REFERENCES conversations(id),
                  request_id TEXT NOT NULL, input_hash TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('running','pending_approval','completed','failed','cancelled')),
                  result TEXT, pending_hash TEXT, pending_expires REAL,
                  UNIQUE(conversation,request_id));
                CREATE TABLE IF NOT EXISTS write_attempts (
                  fingerprint TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), created REAL NOT NULL);
            """)
            columns={r["name"] for r in db.execute("PRAGMA table_info(visitors)")}
            if "user_id" not in columns:
                db.execute("ALTER TABLE visitors ADD COLUMN user_id INTEGER")
            if "customer_id" not in columns:
                db.execute("ALTER TABLE visitors ADD COLUMN customer_id TEXT")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, visitor, secret, ttl, protected=(), *, user_id=None, customer_id=None):
        now = self.clock()
        with self.connection() as db:
            rows=db.execute("SELECT id FROM visitors WHERE expires<=? OR touched>?",(now,now)).fetchall()
            db.executemany("UPDATE visitors SET revoked=1 WHERE id=?", ((r['id'],) for r in rows if r['id'] not in protected))
            if db.execute("SELECT count(*) FROM visitors WHERE revoked=0").fetchone()[0] >= 64:
                raise WebError("busy")
            db.execute("INSERT INTO visitors(id,secret_hash,touched,expires,revoked,user_id,customer_id) VALUES(?,?,?,?,0,?,?)",
                       (visitor, hashlib.sha256(secret.encode()).hexdigest(), now, now+ttl,user_id,customer_id))

    def account_visitors(self, user_id, customer_id):
        """Load only unexpired sessions already bound to this authenticated account."""
        now=self.clock()
        with self.connection() as db:
            db.execute("UPDATE visitors SET revoked=1 WHERE revoked=0 AND (expires<=? OR touched>?)",(now,now))
            visitors=db.execute("SELECT * FROM visitors WHERE user_id=? AND customer_id=? AND revoked=0 ORDER BY touched DESC",
                                (user_id,customer_id)).fetchall()
            return [(dict(v),[dict(r) for r in db.execute("SELECT * FROM conversations WHERE visitor=? ORDER BY rowid",(v['id'],))]) for v in visitors]

    def latest_active_conversation(self, visitor):
        with self.connection() as db:
            row=db.execute("SELECT id FROM conversations WHERE visitor=? AND active=1 ORDER BY rowid DESC LIMIT 1",(visitor,)).fetchone()
            return row["id"] if row else None

    def revoke(self, visitors):
        with self.connection() as db:
            db.executemany("UPDATE visitors SET revoked=1 WHERE id=?", ((v,) for v in visitors))

    def revoke_account(self, user_id):
        with self.connection() as db:
            db.execute("UPDATE visitors SET revoked=1 WHERE user_id=?",(user_id,))

    def expire_unloaded(self, protected):
        now=self.clock()
        with self.connection() as db:
            rows=db.execute("SELECT id FROM visitors WHERE revoked=0 AND (expires<=? OR touched>?)",(now,now)).fetchall()
            expired=[r['id'] for r in rows if r['id'] not in protected]
            db.executemany("UPDATE visitors SET revoked=1 WHERE id=?",((v,) for v in expired))
            return len(expired)

    def touch(self, visitor, ttl):
        now = self.clock()
        with self.connection() as db:
            if db.execute("UPDATE visitors SET touched=?,expires=? WHERE id=? AND revoked=0",
                          (now, now+ttl, visitor)).rowcount != 1:
                raise WebError("session_lost")

    def authenticate(self, visitor, secret, ttl, *, active=False):
        if not isinstance(visitor, str) or not isinstance(secret, str) or len(secret) != 64:
            raise WebError("session_lost")
        now = self.clock()
        with self.connection() as db:
            row = db.execute("SELECT * FROM visitors WHERE id=?", (visitor,)).fetchone()
            expected = row['secret_hash'] if row else '0'*64
            valid = hmac.compare_digest(expected, hashlib.sha256(secret.encode()).hexdigest())
            if not row or not valid or row['revoked']:
                raise WebError("session_lost")
            expired=not active and (now < row['touched'] or now >= min(row['expires'],row['touched']+ttl))
            if expired:
                db.execute("UPDATE visitors SET revoked=1 WHERE id=?",(visitor,))
                rows=None
            else:
                rows=[dict(r) for r in db.execute("SELECT * FROM conversations WHERE visitor=? ORDER BY rowid", (visitor,))]
        # Commit revocation before rejecting; a later clock rollback cannot revive it.
        if rows is None:
            raise WebError('session_lost')
        return rows

    def has_visitor(self, visitor):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM visitors WHERE id=?", (visitor,)).fetchone() is not None

    def conversation_count(self, protected=()):
        now = self.clock()
        with self.connection() as db:
            rows=db.execute("SELECT c.visitor,v.expires FROM conversations c JOIN visitors v ON c.visitor=v.id WHERE v.revoked=0").fetchall()
            return sum(r['expires']>now or r['visitor'] in protected for r in rows)

    def add_conversation(self, identifier, visitor, customer, session, mode, previous):
        with self.connection() as db:
            db.execute("INSERT INTO conversations VALUES(?,?,?,?,?,1,'[]',NULL,NULL)",
                       (identifier,visitor,customer,session,mode))
            if previous:
                db.execute("UPDATE conversations SET active=0 WHERE id=? AND visitor=?", (previous,visitor))

    def recover_interrupted(self):
        # Called ONLY after acquiring exclusive OS ownership. Never replay work.
        with self.connection() as db:
            now=self.clock()
            db.execute("UPDATE visitors SET revoked=1 WHERE expires<=? OR touched>?",(now,now))
            for task in db.execute("SELECT * FROM tasks WHERE status IN ('running','pending_approval')").fetchall():
                pending = task['status']=='pending_approval'
                result = stopped_result(json.loads(task['result']) if task['result'] else {},task['id'],
                                        'approval_restart_required' if pending else 'interrupted',cancelled=pending)
                db.execute("UPDATE tasks SET status=?,result=?,pending_hash=NULL,pending_expires=NULL WHERE id=?",
                           (result['task_status'],encoded(result),task['id']))
                con = db.execute("SELECT * FROM conversations WHERE id=? AND current_task=?",
                                 (task['conversation'],task['id'])).fetchone()
                if con:
                    messages=json.loads(con['messages'])
                    messages.append({'role':'assistant','content':RECOVERY_MESSAGE})
                    db.execute("UPDATE conversations SET messages=?,result=? WHERE id=?",
                               (encoded(messages),encoded(result),con['id']))

    def begin(self, conversation, request_id, message, messages):
        with self.connection() as db:
            prior=db.execute("SELECT * FROM tasks WHERE conversation=? AND request_id=?",(conversation,request_id)).fetchone()
            if prior:
                if prior['input_hash']!=digest(message):
                    raise WebError('invalid_input')
                if prior['status']=='running':
                    raise WebError('unknown_result')
                return prior['id'],json.loads(prior['result'])
            task=uuid4().hex
            db.execute("INSERT INTO tasks(id,conversation,request_id,input_hash,status) VALUES(?,?,?,?,'running')",
                       (task,conversation,request_id,digest(message)))
            db.execute("UPDATE conversations SET messages=?,current_task=?,result=NULL WHERE id=?",
                       (encoded(messages),task,conversation))
            return task,None

    def prior_result(self, conversation, request_id, message):
        with self.connection() as db:
            prior=db.execute("SELECT * FROM tasks WHERE conversation=? AND request_id=?",(conversation,request_id)).fetchone()
            if prior is None:
                return None
            if prior['input_hash']!=digest(message):
                raise WebError('invalid_input')
            if prior['status']=='running':
                raise WebError('unknown_result')
            return json.loads(prior['result'])

    def finish(self, conversation, task, result, messages, *, cancelled=False):
        if cancelled:
            message=('أُلغيت العملية؛ يلزم إجراء جديد وموافقة جديدة لأي كتابة أخرى.'
                     if not result.get('pending_approval') else RECOVERY_MESSAGE)
            result.update(pending_approval=None,stop_reason='cancelled',requires_new_action=True,message=message)
            messages[-1]['content']=message
        pending=result.get('pending_approval')
        status='cancelled' if cancelled else 'pending_approval' if pending else 'completed' if result['stop_reason']=='final_answer' else 'failed'
        result.update(web_task_id=task,task_status=status)
        with self.connection() as db:
            if db.execute("UPDATE tasks SET status=?,result=?,pending_hash=?,pending_expires=? "
                          "WHERE id=? AND conversation=? AND status='running'",
                          (status,encoded(result),digest({'task':task,'pending':pending}) if pending else None,
                           self.clock()+120 if pending else None,task,conversation)).rowcount != 1:
                raise WebError('already_handled')
            db.execute("UPDATE conversations SET messages=?,result=? WHERE id=? AND current_task=?",
                       (encoded(messages),encoded(result),conversation,task))

    def claim_pending(self, conversation, task, pending, decision):
        with self.connection() as db:
            row=db.execute("SELECT * FROM tasks WHERE id=? AND conversation=?",(task,conversation)).fetchone()
            if not row or row['status']!='pending_approval':
                raise WebError('already_handled')
            now=self.clock()
            if not row['pending_expires']-120 <= now < row['pending_expires'] or row['pending_hash']!=digest({'task':task,'pending':pending}):
                raise WebError('approval_invalid')
            db.execute("UPDATE tasks SET status='running',pending_hash=NULL,pending_expires=NULL WHERE id=?",(task,))
            return row['pending_expires']-self.clock()

    def abandon(self, conversation, task, messages, *, reason='unknown_result', cancelled=False):
        with self.connection() as db:
            row=db.execute("SELECT result FROM tasks WHERE id=? AND conversation=?",(task,conversation)).fetchone()
            result=stopped_result(json.loads(row['result']) if row and row['result'] else {},task,reason,cancelled=cancelled)
            db.execute("UPDATE tasks SET status=?,result=?,pending_hash=NULL,pending_expires=NULL WHERE id=? AND conversation=?",
                       (result['task_status'],encoded(result),task,conversation))
            messages.append({'role':'assistant','content':RECOVERY_MESSAGE})
            db.execute("UPDATE conversations SET result=?,messages=? WHERE id=? AND current_task=?",
                       (encoded(result),encoded(messages),conversation,task))
            return result
