"""Audit-only child: original MCP server with one explicitly injected read fault."""
from pathlib import Path
import os
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import knowledge_base
import mcp_server

mode = sys.argv.pop(1)

def fault(query):
    if mode == "crash":
        os._exit(17)
    if mode == "hang":
        time.sleep(30)
        return []
    raise ValueError("Unsupported audit fault")

knowledge_base.search_faq = fault
mcp_server.main()
