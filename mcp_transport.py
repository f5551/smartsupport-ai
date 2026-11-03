"""Production-reusable stdio connection; no database operations or model calls."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from security import validate_text

ROOT = Path(__file__).resolve().parent


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
    args = [str(ROOT / "mcp_server.py"), "--db", str(db_path),
            "--demo-customer-id", context.customer_id, "--demo-session-id", context.session_id]
    if approval_db is not None:
        args += ["--approval-db", str(approval_db)]
    parameters = StdioServerParameters(
        command=sys.executable, args=args, cwd=str(ROOT),
        env={"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(parameters, errlog=errlog) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
            await session.initialize()
            yield session
