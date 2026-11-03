"""Optional PAID integration. Never runs on key presence alone."""

import asyncio
import os

import pytest
from openai_transport import create_async_openai_client as AsyncOpenAI

from agent import Agent
from config import AgentConfig
from mcp_transport import TrustedSession, mcp_connection


@pytest.mark.live_api
def test_real_openai_with_real_mcp_opt_in_only(db_path):
    if os.environ.get("RUN_LIVE_OPENAI") != "1":
        pytest.skip("Paid test requires explicit RUN_LIVE_OPENAI=1 opt-in")
    # Do not load .env to activate the opt-in flag implicitly.
    config = AgentConfig.from_env()
    if not os.environ.get("OPENAI_API_KEY") or config.input_price is None or config.output_price is None:
        pytest.skip("API key and verified model prices are required")
    async def scenario():
        context = TrustedSession("demo_customer_001", "optional-live-test")
        async with AsyncOpenAI(max_retries=0, timeout=config.timeout_seconds) as client:
            async with mcp_connection(db_path, customer_id=context.customer_id, session_id=context.session_id) as session:
                result = await Agent(client, session, context, config).run("استخدم search_faq لمعرفة مدة الشحن في المتجر.")
                assert result.stop_reason == "final_answer"
                assert "search_faq" in result.tools_used
    asyncio.run(scenario())
