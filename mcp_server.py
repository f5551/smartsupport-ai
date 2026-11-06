"""SmartSupport MCP stdio server, using official SDK 1.27.2 low-level APIs.

One process serves ONE explicit local demo customer/session. This is not
production authentication. No identity, path or approval-issuing tool is exposed.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sqlite3
import sys
from typing import Annotated, Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from approval_store import (
    WriteOperationFailed, WriteOperationPending, WriteOperationUnknown,
    consume_approval, fail_operation, mark_operation_unknown,
    reconcile_operations, settle_operation,
)
import database
import knowledge_base
from security import WriteApprovalRequired, validate_text

LOGGER = logging.getLogger(__name__)
Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def nonblank_text(cls, value):
        if isinstance(value, str):
            validate_text(value, "argument", 8000)
        return value


class SearchArguments(Arguments):
    query: Annotated[str, Field(min_length=1, max_length=1000)]


class OrderArguments(Arguments):
    order_id: Identifier


class CreateTicketArguments(OrderArguments):
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    approval_id: Identifier | None = None


class TicketArguments(Arguments):
    ticket_id: Identifier


class SaveMemoryArguments(Arguments):
    content: Annotated[str, Field(min_length=1, max_length=8000)]
    approval_id: Identifier | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str


class Envelope(BaseModel):
    ok: bool
    data: dict[str, Any] | list[dict[str, Any]] | None = None
    error: ErrorDetail | None = None


DEFINITIONS = {
    "search_faq": (SearchArguments, "Search the local electronics-store FAQ; return sourced matches or an empty list."),
    "get_order_status": (OrderArguments, "Read an order belonging to the fixed trusted demo customer; other orders are denied."),
    "create_ticket": (CreateTicketArguments, "Create a complaint for an owned order only with an existing, matching, unexpired, single-use application approval."),
    "get_ticket_status": (TicketArguments, "Read complaint status only for the fixed trusted demo customer."),
    "save_memory": (SaveMemoryArguments, "Save a summary to the fixed customer/session only with an operation-bound application approval."),
    "load_memory": (Arguments, "Load the summary for the fixed trusted customer/session; no session selector is accepted."),
}
TOOL_NAMES = frozenset(DEFINITIONS)


@dataclass(frozen=True)
class DemoSession:
    customer_id: str
    session_id: str
    db_path: Path
    approval_db: Path | None = None

    def __post_init__(self):
        validate_text(self.customer_id, "customer_id", 128)
        validate_text(self.session_id, "session_id", 128)
        if self.approval_db is not None and Path(self.approval_db).resolve() == Path(self.db_path).resolve():
            raise ValueError("Use a separate approval database")
        with database.connect(self.db_path) as connection:
            if connection.execute("SELECT 1 FROM customers WHERE customer_id = ?", (self.customer_id,)).fetchone() is None:
                raise ValueError("Unknown demo customer")
        if self.approval_db is not None and Path(self.approval_db).exists():
            try:
                reconcile_operations(self.approval_db, self.db_path)
            except (sqlite3.Error, OSError):
                LOGGER.warning("Write reconciliation unavailable; writes remain fail-closed")


def execute_approved_write(context: DemoSession, tool: str, arguments: dict, approval_id: str):
    """Execute/replay one approved write using separate durable ledgers.

    The approval DB is not atomically committed with the store DB. The store
    transaction commits the write and its idempotent result together; the
    approval ledger is then settled, or reconciled from that result after crash.
    """
    permit = consume_approval(
        context.approval_db, approval_id=approval_id,
        customer_id=context.customer_id, session_id=context.session_id,
        tool=tool, arguments=arguments, store_db=context.db_path,
    )
    if permit["state"] == "committed":
        return permit["result"]
    try:
        if tool == "create_ticket":
            result = database.create_ticket(
                context.customer_id, arguments["order_id"], arguments["description"],
                approved=True, db_path=context.db_path,
                idempotency_key=permit["idempotency_key"], session_id=context.session_id,
            )
        elif tool == "save_memory":
            result = database.save_memory(
                context.customer_id, context.session_id, arguments["content"],
                approved=True, db_path=context.db_path,
                idempotency_key=permit["idempotency_key"],
            )
        else:
            raise ValueError("Unsupported approved operation")
    except (database.DataAccessError, ValueError):
        fail_operation(context.approval_db, permit)
        raise
    except Exception:
        try:
            mark_operation_unknown(context.approval_db, permit)
        except Exception:
            pass
        raise WriteOperationUnknown("Operation outcome requires reconciliation") from None
    try:
        settle_operation(context.approval_db, permit, result)
    except Exception:
        # The store transaction may already have committed. Never report a clean
        # failure or permit a new key until reconciliation checks the store DB.
        try:
            mark_operation_unknown(context.approval_db, permit)
        except Exception:
            pass
        raise WriteOperationUnknown("Operation outcome requires reconciliation") from None
    return result


def _error(code: str, message: str) -> CallToolResult:
    payload = Envelope(ok=False, error=ErrorDetail(code=code, message=message)).model_dump()
    return CallToolResult(
        isError=True, structuredContent=payload,
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
    )


def build_server(context: DemoSession) -> Server:
    server = Server("SmartSupport AI", version="0.3.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(
            name=name, description=description,
            inputSchema=model.model_json_schema(), outputSchema=Envelope.model_json_schema(),
            annotations=ToolAnnotations(
                readOnlyHint=name not in {"create_ticket", "save_memory"},
                destructiveHint=name == "save_memory", openWorldHint=False,
            ),
        ) for name, (model, description) in DEFINITIONS.items()]

    # Manual strict validation supplies stable, redacted structured errors.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict | CallToolResult:
        if name not in DEFINITIONS:
            return _error("UNKNOWN_TOOL", "Tool is not allowed")
        try:
            model = DEFINITIONS[name][0].model_validate(arguments)
            args = model.model_dump()
            if name in {"create_ticket", "save_memory"}:
                approval_id = args.pop("approval_id")
                data = execute_approved_write(context, name, args, approval_id)
                return Envelope(ok=True, data=data).model_dump()
            if name == "search_faq":
                data = knowledge_base.search_faq(**args)
            elif name == "get_order_status":
                data = database.get_order_status(context.customer_id, **args, db_path=context.db_path)
            elif name == "get_ticket_status":
                data = database.get_ticket_status(context.customer_id, **args, db_path=context.db_path)
            else:
                data = database.load_memory(context.customer_id, context.session_id, db_path=context.db_path)
            return Envelope(ok=True, data=data).model_dump()
        except ValidationError:
            return _error("INVALID_INPUT", "Arguments must match the tool schema; extra fields are forbidden")
        except WriteApprovalRequired:
            return _error("APPROVAL_REQUIRED", "A valid operation-bound application approval is required")
        except WriteOperationPending:
            return _error("WRITE_PENDING", "This approved operation is still being reconciled; do not submit a new write yet")
        except WriteOperationUnknown:
            return _error("WRITE_RESULT_UNKNOWN", "The write result is not verified. Check operation status before requesting another write")
        except WriteOperationFailed:
            return _error("WRITE_FAILED", "The approved operation did not commit; request a new confirmation before retrying")
        except database.DataAccessError:
            return _error("ACCESS_DENIED", "Resource unavailable or access denied")
        except ValueError:
            return _error("INVALID_INPUT", "Invalid tool input")
        except (sqlite3.Error, OSError):
            LOGGER.error("Local storage unavailable")
            return _error("STORAGE_ERROR", "Local storage unavailable")
        except Exception as exc:
            # Do not log arguments, identity, paths or exception messages.
            LOGGER.error("Tool failed (%s)", type(exc).__name__)
            return _error("INTERNAL_ERROR", "Tool execution failed")

    return server


async def run(context: DemoSession) -> None:
    server = build_server(context)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    from backup_manager import runtime_lease, BackupError
    parser = argparse.ArgumentParser(description="SmartSupport MCP stdio: LOCAL DEMO ONLY")
    parser.add_argument("--db", type=Path, default=database.DEFAULT_DB_PATH)
    parser.add_argument("--demo-customer-id", required=True)
    parser.add_argument("--demo-session-id", required=True)
    parser.add_argument("--approval-db", type=Path, help="Trusted app approval store; omit to deny all writes")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        with runtime_lease():
            context = DemoSession(args.demo_customer_id, args.demo_session_id, args.db, args.approval_db)
            LOGGER.warning("Local demo identity only; not production authentication")
            asyncio.run(run(context))
    except (ValueError, sqlite3.Error, OSError, BackupError):
        LOGGER.error("Cannot start: check initialized database and local demo session")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
