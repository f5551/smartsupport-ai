"""Turn the six discovered MCP input schemas into strict Responses functions."""

from copy import deepcopy

from jsonschema import Draft202012Validator

FIELDS = {
    "search_faq": {"query"}, "get_order_status": {"order_id"},
    "create_ticket": {"order_id", "description"}, "get_ticket_status": {"ticket_id"},
    "save_memory": {"content"}, "load_memory": set(),
}
WRITES = frozenset({"create_ticket", "save_memory"})
DESCRIPTIONS = {
    "search_faq": "Search store FAQ; return sourced matches.",
    "get_order_status": "Read this customer's order status.",
    "create_ticket": "Prepare an owned-order complaint; application approval required.",
    "get_ticket_status": "Read this customer's ticket status.",
    "save_memory": "Prepare session memory save; application approval required.",
    "load_memory": "Read this customer's current-session memory.",
}


def convert_tools(discovered):
    if len(discovered) != 6 or {tool.name for tool in discovered} != set(FIELDS):
        raise ValueError("Expected exactly the six allowed MCP tools")
    definitions, validators = [], {}
    for tool in discovered:
        schema = deepcopy(tool.inputSchema)
        properties = schema.get("properties", {})
        if tool.name in WRITES:
            properties.pop("approval_id", None)  # Application supplies it ONLY at resume.
        if schema.get("type") != "object" or set(properties) != FIELDS[tool.name]:
            raise ValueError("Unexpected MCP schema fields")
        # These six SDK schemas are flat string objects. Reject future schema
        # changes we cannot safely convert, instead of silently dropping strict.
        for field in properties.values():
            if field.get("type") != "string" or set(field) - {"title", "type", "minLength", "maxLength", "description"}:
                raise ValueError("Unsupported MCP argument schema")
            field.pop("title", None)
        schema = {"type": "object", "properties": properties,
                  "required": sorted(properties), "additionalProperties": False}
        Draft202012Validator.check_schema(schema)
        validators[tool.name] = Draft202012Validator(schema)
        definitions.append({"type": "function", "name": tool.name,
                            "description": DESCRIPTIONS[tool.name], "parameters": schema, "strict": True})
    return definitions, validators
