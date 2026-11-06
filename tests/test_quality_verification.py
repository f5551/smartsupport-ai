"""Independent negative checks for the Agent's MCP trust boundary.

These tests exercise the in-process validation paths that subprocess MCP
coverage cannot attribute to the parent pytest process.  No API or store write
is used.
"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from agent import Agent
from config import AgentConfig
from fake_openai import FakeOpenAI, fake_response, tool_call
from mcp_server import DEFINITIONS
from mcp_transport import TrustedSession


class _MalformedMCP:
    def __init__(self, structured_content, is_error=False):
        self.structured_content = structured_content
        self.is_error = is_error

    async def list_tools(self):
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name, description=description,
                                   inputSchema=model.model_json_schema())
                   for name, (model, description) in DEFINITIONS.items()]
        )

    async def call_tool(self, name, arguments):
        return SimpleNamespace(isError=self.is_error,
                               structuredContent=self.structured_content)


def _run_with_mcp(mcp):
    client = FakeOpenAI([
        fake_response(calls=[tool_call("search_faq", {"query": "الشحن"})]),
    ])
    config = AgentConfig(model="fake-test-model", input_price=Decimal("0.1"),
                         output_price=Decimal("0.2"))
    agent = Agent(client, mcp, TrustedSession("demo_customer_001", "quality-session"), config)
    return asyncio.run(agent.run("ما مدة الشحن؟")), client


def test_inconsistent_mcp_envelope_is_rejected_before_continuation():
    result, client = _run_with_mcp(_MalformedMCP({"ok": "yes", "data": [], "error": None}))

    assert result.stop_reason == "mcp_error"
    assert len(client.requests) == 1
    assert "yes" not in result.message


def test_oversized_mcp_result_is_rejected_before_model_sees_it():
    payload = {"ok": True, "data": "x" * 65000, "error": None}
    result, client = _run_with_mcp(_MalformedMCP(payload))

    assert result.stop_reason == "mcp_error"
    assert len(client.requests) == 1
    assert "x" * 100 not in result.message
