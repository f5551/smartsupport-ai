"""Production-reusable stdio connection; no database operations or model calls."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
import sys
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from security import validate_text
from backup_manager import acquire_runtime_lease

ROOT = Path(__file__).resolve().parent


def _child_environment() -> dict[str, str]:
    """Return the minimal non-secret environment required by the MCP child.

    The MCP subprocess must use the same optional storage lock path as its
    parent.  Do not inherit the complete environment: that could expose API
    credentials to a process that never needs them.
    """
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    lock_path = os.environ.get("SMARTSUPPORT_STORAGE_LOCK_PATH")
    if lock_path:
        environment["SMARTSUPPORT_STORAGE_LOCK_PATH"] = lock_path
    return environment


@dataclass(frozen=True)
class TrustedSession:
    """Caller-supplied LOCAL DEMO identity, not a production authentication token."""
    customer_id: str
    session_id: str

    def __post_init__(self):
        validate_text(self.customer_id, "customer_id", 128)
        validate_text(self.session_id, "session_id", 128)


@asynccontextmanager
async def mcp_connection(
    db_path, *, customer_id="demo_customer_001", session_id="local-demo-session",
    approval_db=None, errlog=sys.stderr,
):
    context = TrustedSession(customer_id, session_id)
    maintenance_lease = acquire_runtime_lease()
    try:
        args = [str(ROOT / "mcp_server.py"), "--db", str(db_path),
                "--demo-customer-id", context.customer_id, "--demo-session-id", context.session_id]
        if approval_db is not None:
            args += ["--approval-db", str(approval_db)]
        parameters = StdioServerParameters(
            command=sys.executable, args=args, cwd=str(ROOT),
            env=_child_environment(),
        )
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                yield session
    finally:
        maintenance_lease.close()
