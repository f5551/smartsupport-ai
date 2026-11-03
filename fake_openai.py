"""Explicitly simulated OpenAI responses for tests and --fake-demo. No network."""

from copy import deepcopy
import json
from uuid import uuid4

from openai.types.responses import Response


def tool_call(name, arguments, call_id=None):
    return {"type": "function_call", "name": name, "call_id": call_id or "call_" + uuid4().hex,
            "arguments": json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else arguments}


def fake_response(text="", *, calls=(), input_tokens=20, output_tokens=10, cached_tokens=0, usage=True, status="completed", extra_items=(), model="fake-test-model"):
    items = list(extra_items) + list(calls)
    if text:
        items.append({"type": "message", "id": "msg_fake", "role": "assistant", "status": "completed",
                      "content": [{"type": "output_text", "text": text, "annotations": []}]})
    return Response.model_validate({
        "id": "resp_fake_" + uuid4().hex, "object": "response", "created_at": 0,
        "model": model, "status": status, "output": items,
        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "total_tokens": input_tokens + output_tokens,
                  "input_tokens_details": {"cached_tokens": cached_tokens, "cache_write_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}} if usage else None,
    })


class FakeOpenAI:
    usage_source = "simulation"
    def __init__(self, responses):
        self.script = iter(responses)
        self.requests = []
        self.responses = self
        self.options = {}

    def with_options(self, **options):
        self.options = options
        return self

    async def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        item = next(self.script)
        if isinstance(item, BaseException):
            raise item
        return item(kwargs) if callable(item) else item
