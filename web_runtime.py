"""Trusted UI adapter: bounded sessions, persistent controls, one async MCP owner.

No Streamlit calls here and no duplicate Agent loop. Each conversation has a
private synthetic store. All functional operations use Agent -> actual MCP.
"""

import asyncio
import json
import logging
import math
import os
import re
import secrets
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AsyncExitStack, closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from functools import wraps
from pathlib import Path
from queue import Empty, Queue
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from agent import Agent
from approval_store import issue_approval
from audit_logger import AuditLogger
from backup_manager import acquire_runtime_lease
from config import AgentConfig
from database import init_database, seed_demo_data
from demo_client import DemoOpenAI
from mcp_transport import TrustedSession, mcp_connection
from openai_transport import create_async_openai_client as AsyncOpenAI
from runtime_policy import public_demo_only, visitor_idle_timeout, worker_limit
from security import validate_text
from session_store import DirectoryLease, SessionStore
from web_controls import WebControls, WebError

CUSTOMERS = {f"عميل تجريبي {i}": f"demo_customer_{i:03d}" for i in range(1, 6)}
ERRORS = {
    "approval_invalid": "انتهت الموافقة أو تغيرت تفاصيلها؛ ابدأ إجراءً جديدًا بموافقة جديدة.",
    "duplicate_write": "سبق إرسال هذه الشكوى أو تعذر تأكيد نتيجتها؛ راجع حالتها قبل إجراء جديد.",
    "rate_limit": "بلغت حد الطلبات المتاح. انتظر قليلًا؛ بدء محادثة جديدة لا يعيد ضبط الحد.",
    "busy": "الخدمة مشغولة حاليًا. حاول لاحقًا.",
    "live_disabled": "الوضع الحقيقي معطل. يتاح محليًا فقط بعد إعداد المشغل.",
    "invalid_input": "أدخل رسالة واضحة لا تتجاوز 1000 محرف.",
    "session_lost": "فُقدت حالة المحادثة. ابدأ محادثة جديدة؛ لم يُفترض نجاح أي عملية معلقة.",
    "pending": "راجع العملية المعلقة وأكدها أو ألغها قبل إرسال رسالة أخرى.",
    "already_handled": "عولج هذا القرار بالفعل؛ لم تُعد الكتابة.",
    "mcp_error": "تعذر الاتصال بخدمة المتجر. لم نؤكد تنفيذ أي عملية جديدة.",
    "unknown_result": "تعذر تأكيد النتيجة. لا تكرر الكتابة قبل مراجعة حالتها.",
    "configuration": "إعدادات الخدمة غير مكتملة. استخدم الوضع التجريبي أو راجع المشغل.",
}


def redact(text):
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        text = text.replace(key, "[محجوب]")
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[محجوب]", text)


class AgentWorker:
    """A single coroutine enters/uses/exits MCP cancel scopes on its own thread.

    UI reruns never recreate a pending Agent or reuse a closed event loop.
    Idle workers close after 3 minutes; pending state is then intentionally lost.
    """
    def __init__(self, context, directory, mode):
        self.context, self.directory, self.mode = context, directory, mode
        self.queue, self.ready = Queue(), Event()
        self.error, self.pending = None, None
        self.processing = Event()
        self.thread = Thread(target=self._main, daemon=True, name="smartsupport-agent")
        self.thread.start()
        if not self.ready.wait(20):
            self.queue.put(("close", None, Future()))
            raise WebError("mcp_error")
        if self.error:
            raise WebError(self.error)

    def _main(self):
        try:
            asyncio.run(self._serve())
        except WebError as error:
            self.error = error.code
        except BaseException:
            self.error = "mcp_error"
        finally:
            self.ready.set()
            while True:
                try:
                    _, _, future = self.queue.get_nowait()
                except Empty:
                    break
                if not future.done():
                    future.set_exception(WebError("session_lost"))

    async def _serve(self):
        async with AsyncExitStack() as stack:
            if self.mode == "Live":
                if public_demo_only():
                    raise WebError("live_disabled")
                try:
                    config = AgentConfig.from_env()
                    if not os.environ.get("OPENAI_API_KEY"):
                        raise ValueError("Missing credential")
                except Exception:
                    raise WebError("configuration") from None
                client = await stack.enter_async_context(AsyncOpenAI(max_retries=0, timeout=config.timeout_seconds))
            else:
                config = AgentConfig(model="fake-test-model", input_price=Decimal("0.1"),
                                     cached_input_price=Decimal("0.05"), output_price=Decimal("0.2"))
                client = DemoOpenAI()
            session = await stack.enter_async_context(mcp_connection(
                self.directory / "store.sqlite3", customer_id=self.context.customer_id,
                session_id=self.context.session_id, approval_db=self.directory / "approvals.sqlite3"))
            self.audit = AuditLogger(self.directory / "audit.jsonl")
            self.agent = Agent(client, session, self.context, config, audit=self.audit)
            self.model = config.model
            self.ready.set()
            last_activity = monotonic()
            while monotonic() - last_activity < 180:
                try:
                    kind, payload, future = self.queue.get_nowait()
                except Empty:
                    await asyncio.sleep(0.04)
                    continue
                if kind == "close":
                    future.set_result(None)
                    return
                last_activity = monotonic()
                self.processing.set()
                try:
                    started = monotonic()
                    if kind == "send":
                        if isinstance(payload, dict):
                            execution = self.agent.run(payload["message"], history=payload["history"], request_id=payload.get("request_id"))
                        else:
                            execution = self.agent.run(payload)
                        result = await asyncio.wait_for(execution, 90)
                    else:
                        pending = self.pending
                        if not pending or pending["pending_id"] != payload["pending_id"]:
                            raise WebError("session_lost")
                        if {k:v for k,v in pending.items() if k!='call_id'} != payload.get('expected_pending'):
                            raise WebError('approval_invalid')
                        if monotonic() >= payload.get('approval_deadline',0):
                            raise WebError('approval_invalid')
                        self.pending = None
                        reference = None
                        if payload["decision"] == "approve":
                            reference = issue_approval(self.directory / "approvals.sqlite3",
                                customer_id=self.context.customer_id, session_id=self.context.session_id,
                                tool=pending["tool"], arguments=pending["arguments"], confirmed=True)
                        result = await asyncio.wait_for(self.agent.resume(payload["task_id"], pending["pending_id"],
                            decision=payload["decision"], approval_id=reference), 90)
                    self.pending = deepcopy(result.pending_approval)
                    snapshot = result.as_dict()
                    # Only operational, allowlisted audit fields. No arguments,
                    # outputs, grant references, model reasoning or session ids.
                    snapshot["activity"] = [
                        {key: event.get(key) for key in ("tool_name", "status", "duration_ms", "error_code")}
                        for event in self.audit.events
                        if event["task_id"] == result.task_id and event["event_type"] == "mcp_result"]
                    snapshot["execution_ms"] = round((monotonic() - started) * 1000)
                    snapshot["activity_timeline"] = [
                        {key: event.get(key) for key in ("tool_name", "status", "duration_ms", "error_code", "timestamp")}
                        for event in self.audit.events
                        if event["task_id"] == result.task_id and event["event_type"] == "mcp_result"]
                    snapshot["budget_usd"] = str(config.budget_usd)
                    snapshot["tool_call_count"] = sum(
                        event["task_id"] == result.task_id and event["event_type"] == "mcp_request"
                        for event in self.audit.events)
                    snapshot["model"] = self.model
                    future.set_result(snapshot)
                except Exception:
                    self.pending = None
                    future.set_exception(WebError("unknown_result"))
                finally:
                    self.processing.clear()

    def call(self, kind, payload):
        if not self.thread.is_alive() or self.error:
            raise WebError("session_lost")
        future = Future()
        self.queue.put((kind, payload, future))
        try:
            return future.result(timeout=100)
        except FutureTimeout:
            raise WebError("unknown_result") from None

    def close(self):
        if self.thread.is_alive():
            self.queue.put(("close", None, Future()))
            self.thread.join(timeout=5)


@dataclass
class Conversation:
    visitor: str
    customer_label: str
    context: TrustedSession
    directory: Path
    mode: str
    messages: list = field(default_factory=list)
    result: dict | None = None
    worker: AgentWorker | None = None
    active: bool = True
    lock: RLock = field(default_factory=RLock)
    current_task: str | None = None


@dataclass
class VisitorSession:
    last_activity: float
    operations: int = 0
    conversation_ids: set = field(default_factory=set)
    user_id: int | None = None
    customer_id: str | None = None


def visitor_operation(method):
    """Pin identity for the whole operation, including lock waits and MCP I/O."""
    @wraps(method)
    def wrapped(self, visitor, *args, **kwargs):
        with self._visitor_operation(visitor):
            return method(self, visitor, *args, **kwargs)
    return wrapped


class WebBackend:
    def __init__(self, directory, *, bind_address="127.0.0.1", controls=None,
                 visitor_idle_seconds=None, clock=None, wall_clock=None):
        idle = visitor_idle_timeout() if visitor_idle_seconds is None else visitor_idle_seconds
        if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle <= 0:
            raise ValueError("Visitor idle timeout must be positive and finite")
        self.visitor_idle_seconds = float(idle)
        self._clock = clock or monotonic
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._maintenance_lease = acquire_runtime_lease()
        try:
            self._lease = DirectoryLease(self.directory)
        except BaseException:
            self._maintenance_lease.close()
            raise
        self._closed = False
        try:
            self.journal = SessionStore(self.directory, clock=wall_clock)
            self.journal.recover_interrupted()
        except BaseException:
            self._lease.close()
            self._maintenance_lease.close()
            raise
        self.controls = controls or WebControls(self.directory / "web_controls.sqlite3")
        self.bind_address = bind_address
        self.visitors, self.conversations = {}, {}
        self._pending_conversations = 0
        self.lock = RLock()
        self._recovery_secrets = {}
        self._revoked_visitors = set()

    def _expire_locked(self, now):
        """Caller holds registry lock. No conversation locks or worker joins."""
        expired, workers = [], []
        for visitor, state in self.visitors.items():
            if state.operations or now - state.last_activity < self.visitor_idle_seconds:
                continue
            owned = [self.conversations[key] for key in state.conversation_ids]
            # A worker can still be finishing after its caller received a timeout.
            if any(getattr(con.worker, "processing", None) is not None and con.worker.processing.is_set()
                   for con in owned):
                continue
            expired.append(visitor)
            workers.extend(con.worker for con in owned if con.worker is not None)
        if expired:
            self.journal.revoke(expired)
        for visitor in expired:
            del self.visitors[visitor]
            self._recovery_secrets.pop(visitor, None)
        unloaded=self.journal.expire_unloaded(self.visitors)
        # Conversation objects, directories, approvals, memory and audit stay intact.
        return len(expired)+unloaded, workers

    @staticmethod
    def _close_expired_workers(workers):
        # Never join a worker while holding registry or conversation locks.
        for worker in workers:
            try:
                worker.close()
            except Exception:
                logging.getLogger(__name__).warning("Expired visitor worker cleanup did not complete")

    def cleanup_expired_visitors(self):
        with self.lock:
            if self._closed:
                raise WebError('session_lost')
            count, workers = self._expire_locked(self._clock())
        self._close_expired_workers(workers)
        return count

    @contextmanager
    def _visitor_operation(self, visitor):
        pinned=False
        with self.lock:
            if self._closed:
                raise WebError("session_lost")
            _, workers = self._expire_locked(self._clock())
            state = self.visitors.get(visitor)
            if state is not None and visitor not in self._revoked_visitors:
                self.journal.touch(visitor, self.visitor_idle_seconds)
                state.operations += 1
                state.last_activity = self._clock()
                pinned=True
        try:
            self._close_expired_workers(workers)
            if state is None or visitor in self._revoked_visitors:
                raise WebError("session_lost")
            yield
        finally:
            if pinned:
                workers=[]
                with self.lock:
                    state.operations -= 1
                    if visitor in self._revoked_visitors:
                        if state.operations==0 and self.visitors.get(visitor) is state:
                            self.visitors.pop(visitor,None)
                            workers=[self.conversations[c].worker for c in state.conversation_ids
                                     if c in self.conversations and self.conversations[c].worker]
                    else:
                        state.last_activity = self._clock()
                        self.journal.touch(visitor, self.visitor_idle_seconds)
                self._close_expired_workers(workers)

    def _conversation_slots_locked(self):
        # Include valid owners not yet unlocked after restart; in-memory ownership
        # remains a conservative floor if the registries ever disagree.
        return max(self.journal.conversation_count(self.visitors),
                   sum(len(state.conversation_ids) for state in self.visitors.values())) + self._pending_conversations

    def create_browser_session(self):
        """Trusted adapter: return the independent secret once to its browser.

        Do not place either credential in URLs, logs or model/MCP arguments.
        Possession proves this anonymous browser session, not a customer account.
        """
        visitor = self.new_visitor()
        with self.lock:
            return visitor, self._recovery_secrets.pop(visitor)

    def create_account_session(self, principal):
        """Create or restore an account-owned visitor using verified server identity."""
        if principal.role != "customer" or principal.customer_id not in set(CUSTOMERS.values()):
            raise WebError("session_lost")
        label = next(label for label, customer_id in CUSTOMERS.items() if customer_id == principal.customer_id)
        self.cleanup_expired_visitors()
        with self.lock:
            if self._closed:
                raise WebError("session_lost")
            records = self.journal.account_visitors(principal.user_id,principal.customer_id)
            for owner, rows in records:
                visitor=owner['id']
                if visitor in self.visitors and visitor not in self._revoked_visitors:
                    return visitor
                if len(self.visitors) >= 64:
                    break
                restored={}
                for row in rows:
                    if (not re.fullmatch(r'[0-9a-f]{32}', row['id']) or row['customer'] != label
                            or row['mode'] not in {'Demo','Live'}):
                        raise WebError("session_lost")
                    restored[row['id']]=Conversation(visitor,label,
                        TrustedSession(principal.customer_id,row['session']),self.directory / row['id'],row['mode'],
                        messages=json.loads(row['messages']),result=json.loads(row['result']) if row['result'] else None,
                        active=bool(row['active']),current_task=row['current_task'])
                self.conversations.update(restored)
                self.visitors[visitor]=VisitorSession(self._clock(),conversation_ids=set(restored),
                                                       user_id=principal.user_id,customer_id=principal.customer_id)
                self.journal.touch(visitor,self.visitor_idle_seconds)
                return visitor
        visitor=secrets.token_hex(32)
        secret=secrets.token_hex(32)
        with self.lock:
            if len(self.visitors)>=64:
                raise WebError("busy")
            state=VisitorSession(self._clock(),user_id=principal.user_id,customer_id=principal.customer_id)
            self.journal.register(visitor,secret,self.visitor_idle_seconds,self.visitors,
                                  user_id=principal.user_id,customer_id=principal.customer_id)
            self.visitors[visitor]=state
        return visitor

    def logout_account(self, user_id):
        with self.lock:
            owned=[v for v,state in self.visitors.items() if state.user_id==user_id]
            workers=[]
            for visitor in owned:
                state=self.visitors[visitor]
                active_worker=any(getattr(con.worker,"processing",None) is not None and con.worker.processing.is_set()
                                   for con in (self.conversations[c] for c in state.conversation_ids if c in self.conversations))
                if state.operations==0 and not active_worker:
                    self.visitors.pop(visitor,None)
                    workers.extend(self.conversations[c].worker for c in state.conversation_ids
                                   if c in self.conversations and self.conversations[c].worker)
                else:
                    self._revoked_visitors.add(visitor)
            self.journal.revoke_account(user_id)
        self._close_expired_workers(workers)

    def restore_account(self, principal):
        return self.create_account_session(principal)

    def restore_visitor(self, visitor, recovery_secret):
        """Unlock only this owner's snapshots; no worker, model, tool or grant."""
        self.cleanup_expired_visitors()
        with self.lock:
            if self._closed:
                raise WebError("session_lost")
            # Local activity uses monotonic leases; a long active call must not
            # be revoked by a second recovery attempt after its wall expiry.
            rows = self.journal.authenticate(visitor, recovery_secret, self.visitor_idle_seconds,
                                             active=visitor in self.visitors)
            if visitor not in self.visitors:
                if len(self.visitors) >= 64:
                    raise WebError("busy")
                restored = {}
                for row in rows:
                    # IDs are server-created hex, never arbitrary persisted paths.
                    if not re.fullmatch(r'[0-9a-f]{32}', row['id']) or row['customer'] not in CUSTOMERS or row['mode'] not in {'Demo','Live'}:
                        raise WebError("session_lost")
                    restored[row['id']] = Conversation(visitor, row['customer'],
                        TrustedSession(CUSTOMERS[row['customer']], row['session']),
                        self.directory / row['id'], row['mode'], messages=json.loads(row['messages']),
                        result=json.loads(row['result']) if row['result'] else None,
                        active=bool(row['active']), current_task=row['current_task'])
                self.conversations.update(restored)
                self.visitors[visitor] = VisitorSession(self._clock(), conversation_ids=set(restored))
            self.journal.touch(visitor,self.visitor_idle_seconds)
            self.visitors[visitor].last_activity=self._clock()
            return [{'id':row['id'],'customer_label':row['customer'],'mode':row['mode'],'active':bool(row['active'])} for row in rows]

    def live_allowed(self):
        enabled=False
        try:
            import sqlite3
            with closing(sqlite3.connect(self.directory / "auth.sqlite3", timeout=1)) as db:
                row=db.execute("SELECT value FROM system_settings WHERE name='live_enabled'").fetchone()
                enabled=bool(row and row[0]=="1")
        except Exception:
            pass
        return (enabled and not public_demo_only()
                and os.environ.get("SMARTSUPPORT_ENABLE_LOCAL_LIVE") == "1"
                and os.environ.get("SMARTSUPPORT_DEPLOYMENT", "public") == "local"
                and self.bind_address in {"127.0.0.1", "::1"})

    def live_ready(self):
        """UI availability only; worker still validates before any API request."""
        if not self.live_allowed():
            return False
        try:
            AgentConfig.from_env()  # Loads operator .env only after local opt-in.
            return bool(os.environ.get("OPENAI_API_KEY", "").strip())
        except Exception:
            return False

    def new_visitor(self, *, user_id=None, customer_id=None):
        state, workers = None, []
        try:
            with self.lock:
                if self._closed:
                    raise WebError("session_lost")
                _, workers = self._expire_locked(self._clock())
                if len(self.visitors) >= 64:
                    raise WebError("busy")
                # Never reuse a token bound to retained conversations, even on
                # an accidental RNG collision. No caller can choose its token.
                for _ in range(8):
                    visitor = secrets.token_hex(32)
                    if visitor not in self.visitors and not self.journal.has_visitor(visitor) and not any(con.visitor == visitor for con in self.conversations.values()):
                        break
                else:
                    raise WebError("busy")
                state = VisitorSession(self._clock(), operations=1,user_id=user_id,customer_id=customer_id)
                secret = secrets.token_hex(32)
                self.journal.register(visitor,secret,self.visitor_idle_seconds,self.visitors,
                                      user_id=user_id,customer_id=customer_id)
                self._recovery_secrets[visitor]=secret
                self.visitors[visitor] = state
            return visitor
        finally:
            self._close_expired_workers(workers)
            if state is not None:
                with self.lock:
                    state.operations -= 1
                    state.last_activity = self._clock()

    @visitor_operation
    def new_conversation(self, visitor, customer_label, mode="Demo", *, previous=None):
        with self.lock:
            if visitor not in self.visitors or customer_label not in CUSTOMERS or mode not in {"Demo", "Live"}:
                raise WebError("session_lost")
            state=self.visitors[visitor]
            if state.customer_id is not None and CUSTOMERS[customer_label] != state.customer_id:
                raise WebError("session_lost")
            if mode == "Live" and not self.live_allowed():
                raise WebError("live_disabled")
            old = self._get(visitor, previous, enforce_live=False) if previous else None
            if self._conversation_slots_locked() >= 128:
                raise WebError("busy")
            self._pending_conversations += 1
        reserved = True
        try:
            self.controls.consume(visitor)
            identifier = uuid4().hex
            path = self.directory / identifier
            # Explicit synthetic fixture initialization, never customer operations.
            init_database(path / "store.sqlite3")
            seed_demo_data(path / "store.sqlite3")
            context = TrustedSession(CUSTOMERS[customer_label], uuid4().hex)
            if old:
                with old.lock:
                    self._get(visitor, previous, enforce_live=False)
                    old.active = False
                    if old.worker:
                        old.worker.close()
            with self.lock:
                self.journal.add_conversation(identifier,visitor,customer_label,context.session_id,mode,previous)
                self.conversations[identifier] = Conversation(visitor, customer_label, context, path, mode)
                self.visitors[visitor].conversation_ids.add(identifier)
                self._pending_conversations -= 1
                reserved = False
                return identifier
        finally:
            if reserved:
                with self.lock:
                    self._pending_conversations -= 1

    def _get(self, visitor, conversation_id, *, enforce_live=True):
        with self.lock:
            conversation = self.conversations.get(conversation_id)
            if visitor not in self.visitors or conversation is None or not conversation.active or not secrets.compare_digest(conversation.visitor, visitor):
                raise WebError("session_lost")
            if enforce_live and conversation.mode == "Live" and not self.live_allowed():
                raise WebError("live_disabled")
            return conversation

    @visitor_operation
    def view(self, visitor, conversation_id):
        con = self._get(visitor, conversation_id)
        with con.lock:
            return {"messages": deepcopy(con.messages), "result": deepcopy(con.result),
                    "customer_label": con.customer_label, "mode": con.mode,
                    "mcp_status": ("error" if (con.result or {}).get("stop_reason") == "mcp_error"
                                   else "verified" if con.worker and con.worker.thread.is_alive() and not con.worker.error
                                   else "not_checked"),
                    "model": redact(con.worker.model) if con.worker and hasattr(con.worker, "model") else (con.result or {}).get('model')}

    @visitor_operation
    def history(self, visitor, customer_label, mode):
        """Read-only archives owned by this visitor; never reactivate a task."""
        with self.lock:
            if visitor not in self.visitors:
                raise WebError("session_lost")
            if mode == "Live" and not self.live_allowed():
                raise WebError("live_disabled")
            owned = [(key, con) for key, con in self.conversations.items()
                     if secrets.compare_digest(con.visitor, visitor)
                     and con.customer_label == customer_label and con.mode == mode and not con.active]
        snapshots = []
        for key, con in owned:
            with con.lock:
                snapshots.append({"id": key, "messages": deepcopy(con.messages),
                                  "result": deepcopy(con.result), "mode": con.mode,
                                  "customer_label": con.customer_label,
                                  "model": (con.result or {}).get("model"), "mcp_status": "archived"})
        return snapshots

    def _worker(self, con):
        if con.worker is None or not con.worker.thread.is_alive():
            with self.lock:
                active = sum(bool(c.worker and c.worker.thread.is_alive()) for c in self.conversations.values())
                if active >= worker_limit():
                    raise WebError("busy")
                con.worker = AgentWorker(con.context, con.directory, con.mode)
        return con.worker

    def _store(self, conversation_id, con, result, *, cancelled=False):
        # Approval references never appear in AgentResult. Do not expose call ids.
        if result.get("pending_approval"):
            result["pending_approval"].pop("call_id", None)
        result=deepcopy(result)
        result['message']=redact(result['message'])
        messages=con.messages+[{'role':'assistant','content':result['message']}]
        self.journal.finish(conversation_id,con.current_task,result,messages,cancelled=cancelled)
        con.result = result
        con.messages = messages
        return deepcopy(result)

    @visitor_operation
    def send(self, visitor, conversation_id, message, *, request_id=None):
        con = self._get(visitor, conversation_id)
        with con.lock:
            self._get(visitor, conversation_id)
            try:
                validate_text(message, "message", 1000)
            except ValueError:
                raise WebError("invalid_input") from None
            if redact(message) != message:
                raise WebError("invalid_input")
            if request_id is not None and (not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',request_id)):
                raise WebError('invalid_input')
            if request_id is not None:
                prior=self.journal.prior_result(conversation_id,request_id,message)
                if prior is not None:
                    return deepcopy(prior)
            if con.result and con.result.get("pending_approval"):
                raise WebError("pending")
            self.controls.consume(visitor)
            prompt = {"message": message, "history": deepcopy(con.messages[-5:])}
            messages=con.messages+[{"role": "user", "content": redact(message)}]
            task,prior=self.journal.begin(conversation_id,request_id or uuid4().hex,message,messages)
            if prior is not None:
                return deepcopy(prior)
            con.current_task=task
            con.messages=messages
            prompt['request_id']=task
            try:
                result = self._worker(con).call("send", prompt)
                return self._store(conversation_id,con, result)
            except Exception:
                con.result=self.journal.abandon(conversation_id,task,con.messages)
                raise

    @visitor_operation
    def decide(self, visitor, conversation_id, pending_id, decision):
        con = self._get(visitor, conversation_id)
        with con.lock:
            self._get(visitor, conversation_id)
            pending = (con.result or {}).get("pending_approval")
            if decision not in {"approve", "reject"} or not pending or pending["pending_id"] != pending_id:
                raise WebError("already_handled")
            if decision == "approve" and any(redact(v) != v for v in pending["arguments"].values()):
                raise WebError("invalid_input")
            self.controls.consume(visitor)
            try:
                remaining=self.journal.claim_pending(conversation_id,con.current_task,pending,decision)
                deadline=monotonic()+remaining
            except WebError as error:
                if error.code in {'approval_invalid','duplicate_write'}:
                    con.result=self.journal.abandon(conversation_id,con.current_task,con.messages,
                                                   reason=error.code,cancelled=True)
                    if con.worker:
                        con.worker.close()
                        con.worker=None
                raise
            # Durable claim BEFORE grant issuance, independent of session_state.
            try:
                self.controls.claim_decision(conversation_id, pending_id)
            except Exception:
                con.result=self.journal.abandon(conversation_id,con.current_task,con.messages)
                if con.worker and not con.worker.processing.is_set():
                    con.worker.close()
                    con.worker=None
                raise
            con.result["pending_approval"] = None
            if con.worker is None or not con.worker.thread.is_alive():
                con.result=self.journal.abandon(conversation_id,con.current_task,con.messages,reason='session_lost',cancelled=True)
                raise WebError("session_lost")
            try:
                result = con.worker.call("decision", {"task_id": con.result["task_id"],
                                          "pending_id": pending_id, "decision": decision,
                                          "expected_pending":deepcopy(pending),"approval_deadline":deadline})
                return self._store(conversation_id,con, result,cancelled=decision=='reject')
            except Exception:
                con.result=self.journal.abandon(conversation_id,con.current_task,con.messages)
                raise

    def close(self):
        with self.lock:
            if any(state.operations for state in self.visitors.values()):
                # A replacement cannot take ownership while old calls can write.
                raise WebError('busy')
            self._closed=True
            conversations = list(self.conversations.values())
        for con in conversations:
            if con.worker:
                con.worker.close()
        if any(getattr(con.worker,'processing',Event()).is_set() for con in conversations):
            raise WebError('busy')
        self._lease.close()
        self._maintenance_lease.close()
