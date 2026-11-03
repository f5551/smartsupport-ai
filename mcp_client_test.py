"""Real stdio MCP smoke client; temporary seeded database, no OpenAI or writes."""

import asyncio
from importlib.metadata import version
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp_transport import mcp_connection

from database import init_database, seed_demo_data

EXPECTED_TOOLS = {
    "search_faq", "get_order_status", "create_ticket",
    "get_ticket_status", "save_memory", "load_memory",
}
ROOT = Path(__file__).resolve().parent


def require(condition, message):
    # Keep smoke validation active even with python -O.
    if not condition:
        raise RuntimeError(message)


async def smoke_test(db_path) -> dict:
    async with mcp_connection(db_path) as session:
        tools = (await session.list_tools()).tools
        names = {tool.name for tool in tools}
        require(names == EXPECTED_TOOLS and len(tools) == 6, "Unexpected tools")
        faq = await session.call_tool("search_faq", {"query": "كم تستغرق مدة الشحن؟"})
        require(not faq.isError and faq.structuredContent["data"][0]["id"] == "FAQ-001", "FAQ failed")
        order = await session.call_tool("get_order_status", {"order_id": "DEMO-1001"})
        require(not order.isError and order.structuredContent["data"]["status"] == "shipped", "Order failed")
        denied = await session.call_tool("get_order_status", {"order_id": "DEMO-2001"})
        require(denied.isError and denied.structuredContent["error"]["code"] == "ACCESS_DENIED", "Ownership bypass")
        write = await session.call_tool("create_ticket", {"order_id": "DEMO-1002", "description": "تلف تجريبي"})
        require(write.isError and write.structuredContent["error"]["code"] == "APPROVAL_REQUIRED", "Write bypass")
        return {"mcp_version": version("mcp"), "tools": sorted(names),
                "faq": "passed", "owned_order": "passed", "foreign_order": "denied",
                "unapproved_ticket": "denied", "transport": "stdio subprocess"}


def main() -> None:
    # Seed only; all operation checks above use actual MCP requests.
    with TemporaryDirectory(prefix="smartsupport-mcp-") as temporary:
        db_path = Path(temporary) / "smoke.sqlite3"
        init_database(db_path)
        seed_demo_data(db_path)
        report = asyncio.run(smoke_test(db_path))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
