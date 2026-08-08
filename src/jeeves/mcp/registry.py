"""Tool registry: declaration, JSON-Schema helpers, risk tiers, audit, confirm gate.

Tools declare a risk tier. The tier decides whether Jeeves may act immediately or
must obtain the user's explicit agreement first:

    READ   never gated. Reading files, listing events, searching.
    WRITE  mutating but reversible or trivially undone. Volume, opening apps,
           creating a note, moving a file to the Trash.
    RISKY  outbound, destructive or expensive. Sending mail or iMessage,
           running a shell command, permanent deletion.

A RISKY tool gains an implicit ``confirm`` boolean in its schema. Called without
it, the tool performs no action and returns a CONFIRMATION REQUIRED block
describing exactly what would happen; Jeeves must relay that to the user and may
only retry with ``confirm=true`` after the user agrees.

This gate is mediated by the model, so it is a usability feature, not a security
boundary. The real backstops are the hard deny list in ``mac.check_path``, the
shell allow/deny lists, preferring the Trash over ``unlink``, and the fact that
every call is written to the audit log.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable

from .. import config, memory
from ..mac import MacError

READ, WRITE, RISKY = "read", "write", "risky"


class ToolError(Exception):
    """Recoverable failure; the message is returned to the model."""


class NeedsConfirmation(Exception):
    def __init__(self, summary: str, undo: str = "") -> None:
        super().__init__(summary)
        self.summary = summary
        self.undo = undo


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    fn: Callable[..., Any]
    risk: str = READ
    undo: str = ""
    preview: Callable[..., str] | None = None
    needs_automation: bool = False
    # Lets a tool decide its own tier from its arguments — `shell` uses this so
    # `git status` runs immediately while `git push` asks first.
    risk_fn: Callable[..., str] | None = None

    def effective_risk(self, args: dict[str, Any]) -> str:
        if self.risk_fn is None:
            return self.risk
        try:
            return self.risk_fn(**{k: v for k, v in args.items() if k != "confirm"})
        except Exception:  # noqa: BLE001 - fall back to the declared tier
            return self.risk

    def mcp_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


REGISTRY: dict[str, Tool] = {}


# ------------------------------------------------------ JSON-Schema shorthand


def string(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def integer(desc: str, minimum: int | None = None, maximum: int | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "integer", "description": desc}
    if minimum is not None:
        node["minimum"] = minimum
    if maximum is not None:
        node["maximum"] = maximum
    return node


def boolean(desc: str, default: bool | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "boolean", "description": desc}
    if default is not None:
        node["default"] = default
    return node


def enum(desc: str, values: list[str]) -> dict[str, Any]:
    return {"type": "string", "description": desc, "enum": values}


def array(desc: str, item_type: str = "string") -> dict[str, Any]:
    return {"type": "array", "description": desc, "items": {"type": item_type}}


CONFIRM_PROP = boolean(
    "Set true ONLY after the user has explicitly approved this exact action in "
    "conversation. Never set it on your own initiative.",
    default=False,
)


def tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]] | None = None,
    *,
    required: list[str] | None = None,
    risk: str = READ,
    undo: str = "",
    preview: Callable[..., str] | None = None,
    needs_automation: bool = False,
    risk_fn: Callable[..., str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as an MCP tool."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        props = dict(properties or {})
        if risk == RISKY:
            props["confirm"] = dict(CONFIRM_PROP)
        schema = {
            "type": "object",
            "properties": props,
            "required": list(required or []),
            "additionalProperties": False,
        }
        if name in REGISTRY:
            raise RuntimeError(f"duplicate tool name: {name}")
        REGISTRY[name] = Tool(
            name=name,
            description=description.strip(),
            schema=schema,
            fn=fn,
            risk=risk,
            undo=undo,
            preview=preview,
            needs_automation=needs_automation,
            risk_fn=risk_fn,
        )
        return fn

    return decorate


# -------------------------------------------------------------- invocation


def _render_args(args: dict[str, Any]) -> str:
    shown = {k: v for k, v in args.items() if k != "confirm"}
    if not shown:
        return "(no arguments)"
    parts = []
    for key, value in shown.items():
        text = str(value)
        if len(text) > 220:
            text = text[:217] + "…"
        parts.append(f"  {key}: {text}")
    return "\n".join(parts)


def _gate(spec: Tool, args: dict[str, Any]) -> None:
    """Raise NeedsConfirmation when policy requires the user to agree first."""
    mode = config.load().get("safety.mode", "guarded")
    risk = spec.effective_risk(args)
    if mode == "open" or risk == READ:
        return
    if mode == "guarded" and risk == WRITE:
        return
    if args.get("confirm") is True:
        return

    if spec.preview is not None:
        try:
            detail = spec.preview(**{k: v for k, v in args.items() if k != "confirm"})
        except Exception:  # noqa: BLE001 - preview must never break the gate
            detail = _render_args(args)
    else:
        detail = _render_args(args)

    undo = f"\nHow to undo: {spec.undo}" if spec.undo else ""
    raise NeedsConfirmation(
        f"CONFIRMATION REQUIRED — `{spec.name}` was NOT run.\n\n"
        f"Proposed action:\n{detail}{undo}\n\n"
        "Describe this to the user in plain language and ask them to approve it. "
        "If they agree, call this tool again with confirm=true and identical "
        "arguments. If they decline or are ambiguous, do not retry.",
        undo=spec.undo,
    )


def _validate(spec: Tool, args: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = spec.schema["properties"]
    unknown = set(args) - set(props)
    if unknown:
        raise ToolError(
            f"unknown argument(s): {', '.join(sorted(unknown))}. "
            f"Accepted: {', '.join(sorted(props)) or 'none'}"
        )
    missing = [key for key in spec.schema["required"] if key not in args or args[key] is None]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")

    clean: dict[str, Any] = {}
    for key, value in args.items():
        want = props[key].get("type")
        try:
            if want == "integer" and not isinstance(value, bool):
                value = int(value)
            elif want == "boolean" and isinstance(value, str):
                value = value.strip().lower() in {"true", "1", "yes", "on"}
            elif want == "string" and not isinstance(value, str):
                value = str(value)
            elif want == "array" and isinstance(value, str):
                value = [v for v in (s.strip() for s in value.split(",")) if v]
        except (TypeError, ValueError):
            raise ToolError(f"argument {key!r} must be a {want}") from None

        node = props[key]
        if want == "integer":
            if "minimum" in node and value < node["minimum"]:
                raise ToolError(f"{key} must be >= {node['minimum']}")
            if "maximum" in node and value > node["maximum"]:
                raise ToolError(f"{key} must be <= {node['maximum']}")
        if "enum" in node and value not in node["enum"]:
            raise ToolError(f"{key} must be one of: {', '.join(node['enum'])}")
        clean[key] = value
    return clean


def call(name: str, raw_args: dict[str, Any]) -> tuple[str, bool]:
    """Execute a tool. Returns (text, is_error). Never raises."""
    spec = REGISTRY.get(name)
    if spec is None:
        close = [n for n in REGISTRY if name.lower() in n.lower()][:5]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"No such tool: {name}.{hint}", True

    try:
        args = _validate(spec, dict(raw_args or {}))
        _gate(spec, args)
        payload = {k: v for k, v in args.items() if k != "confirm"}
        # Only pass parameters the function actually declares.
        sig = inspect.signature(spec.fn)
        if not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            payload = {k: v for k, v in payload.items() if k in sig.parameters}
        result = spec.fn(**payload)
        text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
        if spec.effective_risk(args) != READ:
            memory.audit(name, args, "ok", text, spec.undo)
        return text or "(done)", False

    except NeedsConfirmation as exc:
        memory.audit(name, raw_args, "awaiting-confirmation", exc.summary, spec.undo)
        return exc.summary, False
    except (ToolError, MacError) as exc:
        memory.audit(name, raw_args, "error", str(exc))
        return f"{name} failed: {exc}", True
    except PermissionError as exc:
        memory.audit(name, raw_args, "denied", str(exc))
        automation = (
            "\nThis needs macOS Automation permission. Run `jeeves doctor` for the fix."
            if spec.needs_automation
            else ""
        )
        return f"{name} was denied by macOS: {exc}{automation}", True
    except Exception as exc:  # noqa: BLE001 - a crashing tool must not kill the server
        memory.audit(name, raw_args, "crash", f"{type(exc).__name__}: {exc}")
        return f"{name} raised {type(exc).__name__}: {exc}", True


def catalogue() -> list[dict[str, Any]]:
    return [spec.mcp_schema() for spec in sorted(REGISTRY.values(), key=lambda t: t.name)]


def by_risk() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {READ: [], WRITE: [], RISKY: []}
    for spec in REGISTRY.values():
        out[spec.risk].append(spec.name)
    return {k: sorted(v) for k, v in out.items()}
