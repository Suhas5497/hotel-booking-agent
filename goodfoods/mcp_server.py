"""GoodFoods MCP server — a from-scratch implementation of the Model Context Protocol
(JSON-RPC 2.0; methods: initialize, ping, tools/list, tools/call).

Why MCP: the tool layer becomes a standalone, protocol-standard service. The same server
powers our Streamlit agent in-process, and can be plugged into any MCP client (Claude
Desktop, IDEs, a partner's agent, a WhatsApp/voice bot) over stdio without code changes:

    python -m goodfoods.mcp_server            # speaks newline-delimited JSON-RPC on stdio

Tool `annotations` (readOnlyHint / destructiveHint) are part of the MCP spec; the agent
uses them to decide which calls need explicit human confirmation — policy is declared by
the server, not hardcoded in the client.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import catalog
from .services import ReservationService, ToolError

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "goodfoods-reservations", "version": "1.0.0"}
log = logging.getLogger("goodfoods.mcp")

# Kept deliberately short: every tool schema is sent with every model call and counts toward
# the provider's tokens-per-minute limit.
_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_TIME = {"type": "string", "description": "24h HH:MM; if the guest gave no AM/PM, as they said it"}
_PARTY = {"type": "integer", "description": "Guests, 1-20"}
_RID = {"type": "string", "description": "e.g. BLR-012"}
_PHONE = {"type": "string", "description": "Guest's 10-digit mobile number."}
_CODE = {"type": "string", "description": "Confirmation code, format GF-XXXXXX."}
_DRY = {"type": "boolean", "description": "Validate and preview only; nothing is written."}


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., dict]
    read_only: bool = True
    destructive: bool = False

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": self.read_only, "destructiveHint": self.destructive,
                            "idempotentHint": self.read_only, "openWorldHint": False},
        }


def build_tools(svc: ReservationService) -> list[Tool]:
    area = {"type": "string", "description": "Bengaluru area from the instructions"}
    feats = "rooftop, outdoor_seating, private_dining, live_music, kid_friendly, bar, pure_veg, quiet_zone, wifi"
    return [
        Tool("search_restaurants",
             "Filter outlets by cuisine, area, budget or features. Add date, time and party_size to see availability.",
             {"type": "object", "properties": {
                 "cuisine": {"type": "string", "description": "e.g. italian, south indian, biryani, sushi"},
                 "area": area,
                 "party_size": _PARTY, "date": _DATE, "time": _TIME,
                 "max_price_per_person": {"type": "integer", "description": "Max ₹ per person"},
                 "features": {"type": "array", "items": {"type": "string"}, "description": f"Any of: {feats}"},
                 "veg_only": {"type": "boolean"},
                 "limit": {"type": "integer", "description": "Default 5"}}},
             svc.search_restaurants),
        Tool("recommend_restaurants",
             "Best-fit outlets with reasons, for guests unsure where to go. Understands occasions and preferences.",
             {"type": "object", "properties": {
                 "occasion": {"type": "string", "description": "e.g. anniversary, family, business, birthday"},
                 "cuisine": {"type": "string"}, "area": area,
                 "party_size": _PARTY, "date": _DATE, "time": _TIME,
                 "budget_per_person": {"type": "integer", "description": "₹ per person"},
                 "preferences": {"type": "string", "description": "e.g. rooftop, quiet, vegetarian"},
                 "limit": {"type": "integer", "description": "Default 3"}}},
             svc.recommend_restaurants),
        Tool("get_restaurant_details",
             "One outlet's address, hours, seating, features and dishes.",
             {"type": "object", "properties": {"restaurant_id": _RID}, "required": ["restaurant_id"]},
             svc.get_restaurant_details),
        Tool("check_availability",
             "Can an outlet seat a party at a date/time? If not, gives other times and nearby free outlets.",
             {"type": "object", "properties": {"restaurant_id": _RID, "date": _DATE, "time": _TIME,
                                               "party_size": _PARTY},
              "required": ["restaurant_id", "date", "time", "party_size"]},
             svc.check_availability),
        Tool("make_reservation",
             "Book a table once the guest has given every required detail.",
             {"type": "object", "properties": {
                 "restaurant_id": _RID, "date": _DATE, "time": _TIME, "party_size": _PARTY,
                 "customer_name": {"type": "string", "description": "Name to hold the table under"},
                 "phone": _PHONE,
                 "special_requests": {"type": "string", "description": "Optional, e.g. high chair"},
                 "dry_run": _DRY},
              "required": ["restaurant_id", "date", "time", "party_size", "customer_name", "phone"]},
             svc.make_reservation, read_only=False),
        Tool("lookup_reservation",
             "Find a guest's bookings by phone. Without the confirmation code the codes are hidden; "
             "changing or cancelling needs the full code.",
             {"type": "object", "properties": {"phone": _PHONE, "confirmation_code": _CODE}, "required": ["phone"]},
             svc.lookup_reservation),
        Tool("modify_reservation",
             "Change an existing booking's date, time, party size, name or requests. Needs the full code and phone.",
             {"type": "object", "properties": {
                 "confirmation_code": _CODE, "phone": _PHONE, "new_date": _DATE, "new_time": _TIME,
                 "new_party_size": _PARTY, "new_name": {"type": "string", "description": "New name for the booking"},
                 "special_requests": {"type": "string"}, "dry_run": _DRY},
              "required": ["confirmation_code", "phone"]},
             svc.modify_reservation, read_only=False),
        Tool("cancel_reservation",
             "Cancel an existing booking.",
             {"type": "object", "properties": {"confirmation_code": _CODE, "phone": _PHONE, "dry_run": _DRY},
              "required": ["confirmation_code", "phone"]},
             svc.cancel_reservation, read_only=False, destructive=True),
    ]


def _coerce(value: Any, schema: dict) -> Any:
    """Small models often send "4" for 4 or "true" for True. Coerce per JSON schema."""
    kind = schema.get("type")
    if value is None:
        return None
    try:
        if kind == "integer" and not isinstance(value, bool):
            return int(float(value))
        if kind == "number":
            return float(value)
        if kind == "boolean" and isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        if kind == "array" and isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if kind == "string" and not isinstance(value, str):
            return str(value)
    except (TypeError, ValueError):
        return value   # let the service raise a precise validation error
    return value


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class MCPServer:
    def __init__(self, service: ReservationService):
        self.tools = {t.name: t for t in build_tools(service)}
        self.initialized = False

    # -- JSON-RPC entry point --------------------------------------------------------
    def handle(self, message: dict) -> dict | None:
        msg_id = message.get("id")
        method = message.get("method")
        if msg_id is None:   # notification: no response
            if method == "notifications/initialized":
                self.initialized = True
            return None
        try:
            if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
                raise JsonRpcError(-32600, "Invalid Request")
            params = message.get("params") or {}
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": SERVER_INFO,
                          "instructions": "GoodFoods table reservations for Bengaluru outlets."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [t.descriptor() for t in self.tools.values()]}
            elif method == "tools/call":
                result = self._call_tool(params.get("name"), params.get("arguments") or {})
            else:
                raise JsonRpcError(-32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except JsonRpcError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # never crash the transport
            log.exception("internal error")
            # Details go to the server log, not to the client.
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": "Internal error"}}

    def _call_tool(self, name: str, arguments: dict) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            raise JsonRpcError(-32602, f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise JsonRpcError(-32602, "arguments must be an object")
        props = tool.input_schema.get("properties", {})
        # Drop unknown keys and empty strings (models love "special_requests": "").
        args = {k: _coerce(v, props[k]) for k, v in arguments.items() if k in props and v not in ("", None, [])}
        missing = [k for k in tool.input_schema.get("required", []) if k not in args]
        started = time.perf_counter()
        if missing:
            payload, is_error = {"error": "MISSING_ARGUMENTS", "message": f"Missing: {', '.join(missing)}",
                                 "hint": "Ask the guest for the missing details before calling again."}, True
        else:
            try:
                payload, is_error = tool.handler(**args), False
            except ToolError as exc:
                payload, is_error = exc.to_dict(), True
        log.info("tool=%s ok=%s ms=%.1f", name, not is_error, (time.perf_counter() - started) * 1000)
        # Tool-level errors are *results* (isError) so the model can read them and recover.
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
                "structuredContent": payload, "isError": is_error}


def serve_stdio(server: MCPServer) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = server.handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)   # stdout is reserved for protocol
    from .seed import build_database
    serve_stdio(MCPServer(ReservationService(build_database())))


if __name__ == "__main__":
    main()
