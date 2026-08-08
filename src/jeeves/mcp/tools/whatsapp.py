"""WhatsApp and generic app-UI tools, driven through the Accessibility API.

WhatsApp Desktop ships no AppleScript dictionary, so these read and drive its
interface directly. That has two consequences worth knowing:

* It is exact — these are the real strings from the UI, not OCR guesses.
* It is coupled to WhatsApp's layout. When an update moves things, the tools say
  so and point at ``ui_inspect``, which prints the tree so it can be re-targeted.

None of this needs a model, a network connection or an API key — only macOS
Accessibility permission.

A note on sending: WhatsApp's terms prohibit automated clients and bulk
messaging. Reading your own chats is one thing; unattended auto-replies are what
gets numbers banned, so ``whatsapp_send`` is gated like any other outbound
action and there is deliberately no "reply to everything" tool.
"""

from __future__ import annotations

import json
from typing import Any

from ... import config
from ...mac import run
from ..registry import READ, RISKY, ToolError, boolean, integer, string, tool

PERMISSION_EXIT = 77
NOT_RUNNING_EXIT = 4
LAYOUT_EXIT = 5


def native(args: list[str], timeout: int = 90) -> str:
    if not config.NATIVE_BIN.exists():
        raise ToolError("the native helper is not built. Run: bash scripts/build_native.sh")
    result = run([str(config.NATIVE_BIN), *args], timeout=timeout)
    if result.code == PERMISSION_EXIT:
        raise PermissionError(result.err or "Accessibility permission denied")
    if result.code == NOT_RUNNING_EXIT:
        raise ToolError(result.err or "the application is not running")
    if result.code == LAYOUT_EXIT:
        raise ToolError(
            (result.err or "the app's layout was not recognised")
            + " Use ui_inspect to see the current tree."
        )
    if not result.ok:
        raise ToolError(result.err or result.out or "the native helper failed")
    return result.out


def native_json(args: list[str], timeout: int = 90) -> Any:
    raw = native(args, timeout)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"native helper returned malformed JSON: {exc}") from None


# ------------------------------------------------------------------ WhatsApp


@tool(
    "whatsapp_unread",
    "List WhatsApp chats with unread messages, and who they're from. Use this "
    "for 'any new messages', 'check WhatsApp', 'who messaged me'.",
    risk=READ,
    needs_automation=True,
)
def whatsapp_unread() -> str:
    rows = native_json(["wa-unread"])
    if not rows:
        return "No unread WhatsApp messages."
    lines = [f"{len(rows)} chat(s) with unread messages:"]
    for row in rows:
        count = row.get("unread", 0)
        plural = "message" if count == 1 else "messages"
        preview = (row.get("preview") or "").strip()
        if len(preview) > 140:
            preview = preview[:137] + "…"
        lines.append(f"- {row.get('name')} — {count} {plural}")
        if preview:
            lines.append(f"    {preview}")
    return "\n".join(lines)


@tool(
    "whatsapp_chats",
    "List WhatsApp conversations in the sidebar, most recent first.",
    {"limit": integer("Maximum chats to list, 1-100.", 1, 100)},
    risk=READ,
    needs_automation=True,
)
def whatsapp_chats(limit: int = 25) -> str:
    rows = native_json(["wa-chats"])
    if not rows:
        return (
            "No chats were found. Is WhatsApp open and signed in? "
            "If it is, run ui_inspect on it — the layout may have changed."
        )
    lines = [f"{len(rows)} chat(s):"]
    for row in rows[:limit]:
        mark = f"  ({row['unread']} unread)" if row.get("unread") else ""
        lines.append(f"- {row.get('name')}{mark}")
    return "\n".join(lines)


@tool(
    "whatsapp_read",
    "Open a WhatsApp conversation and read the visible messages. Give the "
    "contact or group name as it appears in WhatsApp.",
    {
        "chat": string("Contact or group name, e.g. 'Sarah' or 'Family'."),
        "limit": integer("How many recent messages to return, 1-200.", 1, 200),
    },
    required=["chat"],
    risk=READ,
    needs_automation=True,
)
def whatsapp_read(chat: str, limit: int = 30) -> str:
    payload = native_json(["wa-read", chat, "--max", str(limit)], timeout=120)
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not messages:
        return (
            f"Opened {chat!r} but read no messages. The chat may be empty, or "
            "the name may not have matched — whatsapp_chats lists the exact names."
        )
    lines = [f"Last {len(messages)} line(s) from {chat!r}:"]
    lines += [f"  {m}" for m in messages]
    return "\n".join(lines)


@tool(
    "whatsapp_send",
    "Send a WhatsApp message. Always requires confirmation. The chat name must "
    "match what WhatsApp shows — check with whatsapp_chats if unsure.",
    {
        "chat": string("Contact or group name as shown in WhatsApp."),
        "text": string("Message text to send."),
    },
    required=["chat", "text"],
    risk=RISKY,
    needs_automation=True,
    preview=lambda chat, text: (
        "  send a WhatsApp message now — it leaves the Mac and cannot be recalled\n"
        f"  To:   {chat}\n"
        f"  Text: {text[:400]}{'…' if len(text) > 400 else ''}\n"
        "  Jeeves will search for that chat, open the top result and press Return.\n"
        "  Confirm the name is right: an ambiguous search could open a different chat."
    ),
)
def whatsapp_send(chat: str, text: str) -> str:
    if not text.strip():
        raise ToolError("the message text is empty")
    payload = native_json(["wa-send", chat, text], timeout=120)
    sent_to = payload.get("sent_to", chat) if isinstance(payload, dict) else chat
    return f"Sent to {sent_to}: {text[:100]}{'…' if len(text) > 100 else ''}"


# ------------------------------------------------------------- generic app UI


@tool(
    "ui_read_app",
    "Read the visible text out of any application's window, including apps with "
    "no AppleScript support. Exact text from the UI, not OCR.",
    {
        "app": string("Application name, e.g. 'Slack', 'WhatsApp', 'Discord'."),
        "limit": integer("Maximum lines of text, 1-1000.", 1, 1000),
    },
    required=["app"],
    risk=READ,
    needs_automation=True,
)
def ui_read_app(app: str, limit: int = 200) -> str:
    text = native(["ui-dump", app, "--max", str(limit)])
    return text or f"{app} is running but exposed no readable text."


@tool(
    "ui_inspect",
    "Print an application's accessibility tree with roles and identifiers. This "
    "is the debugging tool for when an app's layout changes and a UI-driven tool "
    "stops finding things.",
    {
        "app": string("Application name."),
        "limit": integer("Maximum nodes, 1-2000.", 1, 2000),
    },
    required=["app"],
    risk=READ,
    needs_automation=True,
)
def ui_inspect(app: str, limit: int = 300) -> str:
    return native(["ui-dump", app, "--roles", "--max", str(limit)]) or "(empty tree)"


@tool(
    "ui_type_text",
    "Type text into whatever application currently has keyboard focus. Requires "
    "confirmation, because what it does depends entirely on what has focus.",
    {
        "text": string("Text to type."),
        "press_return": boolean("Press Return afterwards, which may send or submit.",
                               default=False),
    },
    required=["text"],
    risk=RISKY,
    needs_automation=True,
    preview=lambda text, press_return=False: (
        "  type into the frontmost application, whatever that currently is\n"
        f"  Text:   {text[:300]}{'…' if len(text) > 300 else ''}\n"
        f"  Return: {'yes — this may send or submit' if press_return else 'no'}"
    ),
)
def ui_type_text(text: str, press_return: bool = False) -> str:
    native(["ui-type", text], timeout=60)
    if press_return:
        from ...mac import osascript

        osascript(
            'on run argv\n  tell application "System Events" to key code 36\nend run\n',
            timeout=15,
        )
    return f"Typed {len(text)} character(s)" + (" and pressed Return." if press_return else ".")
