"""Minimal MCP server over stdio — JSON-RPC 2.0, newline-delimited, no deps.

stdout carries protocol frames only. Everything human-readable goes to stderr,
which Jeeves captures into ~/.local/state/jeeves/agent.log.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import registry

SERVER_NAME = "jeeves"
SERVER_VERSION = "1.0.0"
FALLBACK_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def log(message: str) -> None:
    print(f"[jeeves-mcp] {message}", file=sys.stderr, flush=True)


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    asked = params.get("protocolVersion")
    version = asked if asked in SUPPORTED_PROTOCOLS else FALLBACK_PROTOCOL
    client = (params.get("clientInfo") or {}).get("name", "unknown")
    log(f"initialize from {client} (protocol {asked} -> {version})")
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name") or ""
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return {
            "content": [{"type": "text", "text": "arguments must be a JSON object"}],
            "isError": True,
        }
    text, is_error = registry.call(name, args)
    payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["isError"] = True
    return payload


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. Returns None for notifications."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if method == "notifications/initialized" or (method or "").startswith("notifications/"):
        return None

    try:
        if method == "initialize":
            result = _initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": registry.catalogue()}
        elif method == "tools/call":
            result = _tools_call(params)
        elif method in {"resources/list", "resources/templates/list"}:
            result = {"resources": [], "resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            if not is_notification:
                _error(req_id, METHOD_NOT_FOUND, f"method not found: {method}")
            return None
    except Exception as exc:  # noqa: BLE001
        log(f"internal error in {method}: {type(exc).__name__}: {exc}")
        if not is_notification:
            _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return None

    if is_notification:
        return None
    _result(req_id, result)
    return result


def serve() -> int:
    log(f"ready with {len(registry.REGISTRY)} tools")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"bad JSON frame: {exc}")
            _error(None, PARSE_ERROR, "invalid JSON")
            continue
        if not isinstance(message, dict):
            _error(None, INVALID_REQUEST, "expected a JSON object")
            continue
        handle(message)
    log("stdin closed, exiting")
    return 0
