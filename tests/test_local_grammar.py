"""Test the no-AI command grammar: does each phrase resolve to the right tool?

Parsing only — nothing is executed, so this suite needs no permissions and has
no side effects.
"""

import sys

sys.path.insert(0, "src")

from jeeves import local  # noqa: E402
from jeeves.mcp import registry  # noqa: E402

# phrase -> (tool, subset of expected args)
CASES: list[tuple[str, str, dict]] = [
    # WhatsApp: reading
    ("read my messages", "whatsapp_unread", {}),
    ("read messages", "whatsapp_unread", {}),
    ("check my unread messages", "whatsapp_unread", {}),
    ("any new messages?", "whatsapp_unread", {}),
    ("who messaged me", "whatsapp_unread", {}),
    ("check whatsapp", "whatsapp_unread", {}),
    ("read my chat with Sarah", "whatsapp_read", {"chat": "Sarah"}),
    ("show conversation with Mum", "whatsapp_read", {"chat": "Mum"}),
    ("what did Priya say", "whatsapp_read", {"chat": "Priya"}),
    ("list my chats", "whatsapp_chats", {}),

    # WhatsApp: sending — the dictated-reply path
    ("reply to Sarah: on my way", "whatsapp_send",
     {"chat": "Sarah", "text": "on my way"}),
    ("reply to Sarah saying I'll be ten minutes late", "whatsapp_send",
     {"chat": "Sarah", "text": "I'll be ten minutes late"}),
    ("message the Family group: dinner at eight", "whatsapp_send",
     {"chat": "the Family group", "text": "dinner at eight"}),
    ("text Dad: call me back", "whatsapp_send", {"chat": "Dad", "text": "call me back"}),
    ("tell Sarah I am running late", "whatsapp_send",
     {"chat": "Sarah", "text": "I am running late"}),
    ("respond to Ravi, sounds good", "whatsapp_send",
     {"chat": "Ravi", "text": "sounds good"}),

    # iMessage and mail
    ("imessage +15551234567: on my way", "imessage_send",
     {"recipient": "+15551234567", "text": "on my way"}),
    ("any unread mail", "mail_unread", {}),
    ("check my mail", "mail_unread", {}),

    # Calendar
    ("what's on my calendar", "calendar_agenda", {"days": 1}),
    ("what is on my schedule today", "calendar_agenda", {"days": 1}),
    ("what's on tomorrow", "calendar_agenda", {"days": 2}),
    ("what's on my week", "calendar_agenda", {"days": 7}),
    ("am I free today", "calendar_free_slots", {}),

    # Reminders and notes
    ("remind me to call the dentist", "reminders_add", {"title": "call the dentist"}),
    ("my reminders", "reminders_list", {}),
    ("make a note: buy milk", "notes_create", {"body": "buy milk"}),
    ("search my notes for tax", "notes_search", {"query": "tax"}),

    # System — order matters here, up/down must beat the generic rule
    ("set volume to 40", "volume_set", {"level": 40}),
    ("volume to twenty five", "volume_set", {"level": 25}),
    ("set volume to half", "volume_set", {"level": 50}),
    ("volume up", "volume_set", {"level": -1}),
    ("louder", "volume_set", {"level": -1}),
    ("volume down", "volume_set", {"level": -2}),
    ("quieter", "volume_set", {"level": -2}),
    ("what's the volume", "volume_get", {}),
    ("mute", "volume_mute", {"muted": True}),
    ("unmute", "volume_mute", {"muted": False}),
    ("battery level", "battery", {}),
    ("what's my battery", "battery", {}),
    ("wifi status", "wifi_status", {}),
    ("my ip address", "network_info", {}),
    ("brightness to 70", "brightness_set", {"percent": 70}),
    ("lock the screen", "sleep_display", {}),
    ("keep awake for 30 minutes", "caffeinate", {"minutes": 30}),

    # Apps and files
    ("open Safari", "open_app", {"name": "Safari"}),
    ("launch Visual Studio Code", "open_app", {"name": "Visual Studio Code"}),
    ("quit Music", "quit_app", {"name": "Music"}),
    ("what's running", "list_running_apps", {}),
    ("find file called invoice", "find_files", {"query": "invoice", "match": "name"}),

    # Clipboard, screen, music, memory
    ("what's on my clipboard", "clipboard_read", {}),
    ("copy hello world", "clipboard_write", {"text": "hello world"}),
    ("take a screenshot", "screenshot", {"mode": "screen"}),
    ("read my screen", "screen_text", {"fast": True}),
    ("pause", "music_control", {"action": "pause"}),
    ("next track", "music_control", {"action": "next"}),
    ("play Rubber Soul", "music_play_search", {"query": "Rubber Soul"}),
    ("remember that I prefer metric units", "remember",
     {"text": "I prefer metric units", "kind": "profile"}),
    ("what do you remember about Sarah", "recall", {"query": "Sarah"}),
    ("what have you done", "audit_trail", {}),

    # Answered in Python, no tool
    ("what time is it", "", {}),
    ("what's the date", "", {}),
]

failures: list[str] = []

for phrase, want_tool, want_args in CASES:
    got = local.parse(phrase)
    if got is None:
        failures.append(f"  {phrase!r}\n    no rule matched (expected {want_tool or 'direct answer'})")
        continue
    tool, args = got
    if tool != want_tool:
        failures.append(f"  {phrase!r}\n    expected tool {want_tool!r}, got {tool!r}")
        continue
    for key, value in want_args.items():
        if args.get(key) != value:
            failures.append(
                f"  {phrase!r}\n    arg {key}: expected {value!r}, got {args.get(key)!r}"
            )

# Every rule must target a tool that actually exists.
for entry in local.RULES:
    if entry.tool and entry.tool not in registry.REGISTRY:
        failures.append(f"  rule {entry.example!r} targets unknown tool {entry.tool!r}")

# Unmatched input must be handled gracefully, never crash.
for nonsense in ["", "   ", "asdfghjkl", "!!!", "tell", "reply to"]:
    outcome = local.interpret(nonsense)
    if outcome.matched and nonsense.strip():
        failures.append(f"  {nonsense!r} unexpectedly matched a rule")

# The suggester must not crash and should offer something for near-misses.
suggestion = local.interpret("sarah message lunch").text
if "don't have a rule" not in suggestion:
    failures.append("  near-miss input did not produce a suggestion")

# Model-directed text must be stripped from human-facing confirmations.
sample = (
    "CONFIRMATION REQUIRED — `x` was NOT run.\n\nProposed action:\n  do a thing\n\n"
    "Describe this to the user in plain language and ask them to approve it."
)
if "Describe this to the user" in local.human_preview(sample):
    failures.append("  human_preview left model-directed instructions in place")

print(f"{len(CASES)} grammar cases checked, {len(failures)} failure(s)")
if failures:
    print("\nFAILURES:")
    print("\n".join(failures))
    sys.exit(1)
print(f"all {len(local.RULES)} rules valid, targeting real tools")
