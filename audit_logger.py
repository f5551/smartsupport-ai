"""Allowlisted JSONL events: no messages, arguments, identity or grant tokens.

Each logger owns a random session-pseudonym key. One writer per file/process is
supported; this is not a tamper-proof or cross-process audit service.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from threading import Lock

EVENTS = {"task_start", "model_request", "model_result", "model_error", "mcp_request", "mcp_result",
          "approval_requested", "approval_submitted", "approval_granted", "approval_denied", "task_end",
          "security_event", "budget_error", "transport_error"}
EVENTS.add("limit_error")
TOOLS = {"search_faq", "get_order_status", "create_ticket", "get_ticket_status", "save_memory", "load_memory"}
STATUSES = {"started", "requested", "success", "failure", "pending", "submitted", "granted", "denied", "blocked", "stopped", "detected"}
REASONS = {"final_answer", "budget_limit", "max_turns", "pending_approval", "mcp_error", "openai_error",
           "cost_unavailable", "usage_unavailable", "incomplete_response", "invalid_response", "audit_error"}
REASONS.update({"max_tool_calls", "duplicate_tool_call", "input_token_limit", "daily_budget_limit", "budget_storage_error"})
CODES = {"INVALID_INPUT", "ACCESS_DENIED", "APPROVAL_REQUIRED", "STORAGE_ERROR", "INTERNAL_ERROR", "UNKNOWN_TOOL",
         "USER_DECLINED", "SECURITY_POLICY_REJECTED", "UNTRUSTED_INSTRUCTION", "API_ERROR", "RATE_LIMIT",
         "MCP_TRANSPORT_ERROR", "BUDGET_LIMIT", "USAGE_UNAVAILABLE", "PRICE_UNAVAILABLE", "MODEL_PRICE_MISMATCH",
         "TIER_PRICE_MISMATCH", "UNPRICED_CACHE_WRITE", "CACHE_USAGE_UNAVAILABLE", "AUDIT_UNAVAILABLE"}
CODES.update({"MAX_TOOL_CALLS", "DUPLICATE_TOOL_CALL", "INPUT_TOKEN_LIMIT", "DAILY_BUDGET_LIMIT", "BUDGET_STORAGE_ERROR"})


class AuditError(OSError):
    pass


def _amount(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


class AuditLogger:
    def __init__(self, path=None, *, source="local_execution", scenario=None, fault_source="none"):
        self.path = Path(path) if path is not None else None
        self.events = []
        self._key = os.urandom(32)
        self._lock = Lock()
        self.source = source if source in {"local_execution", "isolated_integration", "unit_test"} else "local_execution"
        self.scenario = scenario if scenario in {"success_1", "near_failure_recovery", "explicit_failure"} else None
        self.fault_source = fault_source if fault_source in {"none", "injected_mcp_transport_failure"} else "none"

    def _id(self, value, prefix):
        return prefix + hmac.new(self._key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:24]

    def emit(self, *, task_id, session_id, event_type, status, **fields):
        if event_type not in EVENTS or status not in STATUSES:
            raise ValueError("Unsupported audit event")
        # No arbitrary metadata merges. Unrecognized fields are intentionally dropped.
        event = {"timestamp": datetime.now(timezone.utc).isoformat(),
                 "task_id": task_id if re.fullmatch(r"[0-9a-f]{32}", str(task_id)) else self._id(task_id, "task_"),
                 "session_id": self._id(session_id, "session_"), "event_type": event_type,
                 "tool_name": fields.get("tool_name") if fields.get("tool_name") in TOOLS else None,
                 "status": status, "duration_ms": None, "input_tokens": None, "output_tokens": None,
                 "cached_tokens": None, "estimated_cost_usd": _amount(fields.get("estimated_cost_usd")),
                 "cost_usd": _amount(fields.get("cost_usd")),
                 "stop_reason": fields.get("stop_reason") if fields.get("stop_reason") in REASONS else None,
                 "error_code": fields.get("error_code") if fields.get("error_code") in CODES else None,
                 "usage_source": fields.get("usage_source") if fields.get("usage_source") in {"api", "simulation", "unverified"} else "unverified",
                 "cost_kind": fields.get("cost_kind") if fields.get("cost_kind") in {"api_usage", "simulated_usage", "unverified_usage", "estimate", "unknown"} else "unknown",
                 "source": self.source, "scenario": self.scenario, "fault_source": self.fault_source}
        for key in ("input_tokens", "output_tokens", "cached_tokens", "duration_ms"):
            value = fields.get(key)
            if type(value) is int and value >= 0:
                event[key] = value
        pricing = fields.get("pricing")
        if isinstance(pricing, dict):
            safe = {key: _amount(pricing.get(key)) for key in (
                "input_price_per_million", "cached_price_per_million", "output_price_per_million",
                "fixed_request_usd", "multiplier", "cache_write_surcharge_per_million")}
            safe["provider"] = "OpenAI" if pricing.get("provider") == "OpenAI" else None
            model = pricing.get("model", "")
            safe["model"] = model if isinstance(model, str) and re.fullmatch(r"(?:gpt-|fake-|configured-)[A-Za-z0-9.-]{1,100}", model) else "unverified-model"
            safe["service_tier"] = pricing.get("service_tier") if pricing.get("service_tier") in {"default", "flex", "priority", "batch"} else None
            verified = pricing.get("verified_at")
            safe["verified_at"] = verified if isinstance(verified, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified) else None
            url = pricing.get("source_url")
            safe["source_url"] = url if url in {
                "https://developers.openai.com/api/docs/pricing",
                "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
                "https://developers.openai.com/api/docs/models/gpt-4.1-mini",
                "https://developers.openai.com/api/docs/models/gpt-4o-mini"} else None
            event["pricing"] = safe
        serialized = json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        with self._lock:
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as target:
                        target.write(serialized)
                        target.flush()
                        os.fsync(target.fileno())
                except OSError as exc:
                    raise AuditError("Audit unavailable") from exc
            self.events.append(event)
        return event
