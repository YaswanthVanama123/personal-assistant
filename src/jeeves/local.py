"""Local mode: a personal assistant with no AI and no network.

This is a deterministic command engine. It matches what you say against a table
of patterns, pulls out the pieces, and calls the same 69 tools the AI-driven mode
uses. Nothing here contacts a model, a server or an API — it is regular expressions
plus macOS.

What that buys you:

* Works with `claude` uninstalled, offline, on a plane, at zero cost.
* Completely predictable: a phrase either matches a rule or it doesn't, and the
  same phrase always does the same thing.
* Confirmation is a real terminal prompt you answer yourself, which is a stronger
  guarantee than the AI-driven gate.

What it costs you: it only understands the phrasings in the table below. It cannot
summarise, cannot compose a reply for you, and cannot work out what you meant.
Dictation covers most of the gap — when you say "reply to Sarah: I'll be ten
minutes late", speech-to-text already produced the exact words, so no
intelligence is needed to send them.

Add a rule by appending to RULES; `jeeves local --list` prints them all.
"""

from __future__ import annotations

import difflib
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import mac
from .mcp import registry
from .mcp import tools as _tools  # noqa: F401  (registers every tool)

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "half": 50, "max": 100, "maximum": 100, "full": 100, "mute": 0,
}


def to_number(text: str, default: int | None = None) -> int | None:
    text = text.strip().lower().rstrip("%")
    if text.isdigit():
        return int(text)
    if text in NUMBER_WORDS:
        return NUMBER_WORDS[text]
    # "twenty five" / "seventy-five"
    parts = re.split(r"[\s-]+", text)
    if len(parts) == 2 and all(p in NUMBER_WORDS for p in parts):
        return NUMBER_WORDS[parts[0]] + NUMBER_WORDS[parts[1]]
    return default


@dataclass
class Rule:
    pattern: re.Pattern[str]
    tool: str
    example: str
    # Turn the regex match into tool arguments.
    build: Callable[[re.Match[str]], dict] = field(default=lambda m: dict(m.groupdict()))
    # Handled entirely in Python, no tool call.
    answer: Callable[[re.Match[str]], str] | None = None


def rule(
    regex: str,
    tool: str,
    example: str,
    build: Callable[[re.Match[str]], dict] | None = None,
    answer: Callable[[re.Match[str]], str] | None = None,
) -> Rule:
    compiled = re.compile(regex, re.IGNORECASE)
    if build is not None:
        return Rule(compiled, tool, example, build, answer)
    return Rule(compiled, tool, example, answer=answer)


def _clean_groups(match: re.Match[str]) -> dict:
    return {k: v.strip() for k, v in match.groupdict().items() if v is not None}


# Optional filler that speech-to-text loves to insert.
P = r"(?:please\s+)?"
MY = r"(?:my\s+|the\s+)?"

RULES: list[Rule] = [
    # ---------------------------------------------------------- WhatsApp
    rule(
        rf"^{P}(?:read|check|any|show)\s*{MY}(?:new\s+|unread\s+)?"
        r"(?:whats\s?app\s+)?messages?\??$",
        "whatsapp_unread",
        "read my messages",
    ),
    rule(
        rf"^{P}(?:who|anyone)\s+(?:has\s+)?(?:messaged|texted|written)\s*(?:me)?\??$",
        "whatsapp_unread",
        "who messaged me",
    ),
    rule(
        rf"^{P}(?:check|open)\s+whats\s?app$",
        "whatsapp_unread",
        "check whatsapp",
    ),
    rule(
        rf"^{P}(?:read|show|open)\s+(?:{MY})?(?:chat|conversation|messages)\s+"
        r"(?:with|from)\s+(?P<chat>.+?)\??$",
        "whatsapp_read",
        "read my chat with Sarah",
        build=_clean_groups,
    ),
    rule(
        rf"^{P}what\s+did\s+(?P<chat>.+?)\s+say\??$",
        "whatsapp_read",
        "what did Sarah say",
        build=_clean_groups,
    ),
    rule(
        rf"^{P}(?:reply|respond)\s+to\s+(?P<chat>.+?)\s*(?:[:,]|saying|with|that)\s+(?P<text>.+)$",
        "whatsapp_send",
        "reply to Sarah: on my way",
        build=_clean_groups,
    ),
    rule(
        rf"^{P}(?:send|text|message|whats\s?app)\s+(?P<chat>.+?)\s*"
        r"(?:[:,]|saying|that)\s+(?P<text>.+)$",
        "whatsapp_send",
        "message Sarah: running late",
        build=_clean_groups,
    ),
    rule(
        rf"^{P}(?:tell|let)\s+(?P<chat>.+?)\s+(?:know\s+)?(?:that\s+)?(?P<text>i.+)$",
        "whatsapp_send",
        "tell Sarah I'll be late",
        build=_clean_groups,
    ),
    rule(
        rf"^{P}list\s+{MY}(?:whats\s?app\s+)?chats?$",
        "whatsapp_chats",
        "list my chats",
    ),

    # ------------------------------------------------------- iMessage / mail
    rule(
        rf"^{P}(?:imessage|sms)\s+(?P<recipient>\S+)\s*(?:[:,]|saying)\s+(?P<text>.+)$",
        "imessage_send",
        "imessage +15551234567: on my way",
        build=_clean_groups,
    ),
    rule(rf"^{P}(?:any\s+)?(?:new\s+|unread\s+)?(?:e-?)?mail\??$", "mail_unread", "any unread mail"),
    rule(rf"^{P}check\s+{MY}(?:e-?)?mail$", "mail_unread", "check my mail"),

    # ------------------------------------------------------------- calendar
    rule(
        rf"^{P}what(?:'s| is)?\s+(?:on\s+)?{MY}(?:calendar|schedule|agenda)"
        r"(?:\s+(?:for\s+)?today)?\??$",
        "calendar_agenda",
        "what's on my calendar",
        build=lambda m: {"days": 1},
    ),
    rule(
        rf"^{P}what(?:'s| is)?\s+(?:on\s+)?(?:tomorrow|my\s+day\s+tomorrow)\??$",
        "calendar_agenda",
        "what's on tomorrow",
        build=lambda m: {"days": 2},
    ),
    rule(
        rf"^{P}what(?:'s| is)?\s+(?:on\s+)?{MY}week\??$",
        "calendar_agenda",
        "what's on my week",
        build=lambda m: {"days": 7},
    ),
    rule(
        rf"^{P}(?:am\s+i\s+free|when\s+am\s+i\s+free|find\s+(?:me\s+)?(?:a\s+)?free\s+"
        r"(?:slot|time))\s*(?:today)?\??$",
        "calendar_free_slots",
        "am I free today",
        build=lambda m: {},
    ),
    rule(
        rf"^{P}(?:next|my\s+next)\s+(?:meeting|event)\??$",
        "calendar_agenda",
        "my next meeting",
        build=lambda m: {"days": 1},
    ),

    # ------------------------------------------------------------ reminders
    rule(
        rf"^{P}(?:remind\s+me\s+to|add\s+(?:a\s+)?reminder\s+to)\s+(?P<title>.+?)"
        r"(?:\s+at\s+(?P<when>.+))?$",
        "reminders_add",
        "remind me to call the dentist",
        build=lambda m: {"title": m.group("title").strip()},
    ),
    rule(
        rf"^{P}(?:what\s+are\s+)?{MY}reminders\??$",
        "reminders_list",
        "my reminders",
    ),

    # ---------------------------------------------------------------- notes
    rule(
        rf"^{P}(?:make|create|add)\s+(?:a\s+)?note\s*(?:[:,]|saying|that)?\s*(?P<body>.+)$",
        "notes_create",
        "make a note: buy milk",
        build=lambda m: {"title": m.group("body").strip()[:60], "body": m.group("body").strip()},
    ),
    rule(
        rf"^{P}(?:search|find)\s+{MY}notes?\s+(?:for\s+)?(?P<query>.+)$",
        "notes_search",
        "search my notes for tax",
        build=_clean_groups,
    ),

    # --------------------------------------------------------------- system
    # The up/down/mute forms must precede the generic "volume <level>" rule,
    # which would otherwise match "volume up" with level="up".
    rule(rf"^{P}(?:volume\s+up|louder|turn\s+it\s+up)$", "volume_set",
         "volume up", build=lambda m: {"level": -1}),
    rule(rf"^{P}(?:volume\s+down|quieter|turn\s+it\s+down)$", "volume_set",
         "volume down", build=lambda m: {"level": -2}),
    rule(rf"^{P}mute$", "volume_mute", "mute", build=lambda m: {"muted": True}),
    rule(rf"^{P}unmute$", "volume_mute", "unmute", build=lambda m: {"muted": False}),
    rule(
        rf"^{P}(?:what(?:'s| is)?\s+(?:the\s+)?)?{MY}volume\??$",
        "volume_get",
        "what's the volume",
    ),
    rule(
        rf"^{P}(?:set\s+)?{MY}volume\s+(?:to\s+)?(?P<level>\d{{1,3}}|[a-z]+(?:[\s-][a-z]+)?)%?$",
        "volume_set",
        "set volume to 40",
        build=lambda m: {"level": to_number(m.group("level"), 50)},
    ),
    rule(rf"^{P}(?:what(?:'s| is)?\s+)?{MY}(?:battery|charge)(?:\s+level)?\??$",
         "battery", "battery level"),
    rule(rf"^{P}(?:wi-?fi|network|internet)(?:\s+status)?\??$", "wifi_status", "wifi status"),
    rule(rf"^{P}(?:ip|my\s+ip)(?:\s+address)?\??$", "network_info", "my ip address"),
    rule(rf"^{P}(?:system\s+)?info(?:rmation)?$", "system_info", "system info"),
    rule(rf"^{P}(?:how\s+much\s+)?disk(?:\s+space)?(?:\s+(?:is\s+)?left)?\??$",
         "system_info", "how much disk space"),
    rule(
        rf"^{P}(?:set\s+)?brightness\s+(?:to\s+)?(?P<percent>[\w\s-]+?)%?$",
        "brightness_set",
        "brightness to 70",
        build=lambda m: {"percent": to_number(m.group("percent"), 60)},
    ),
    rule(rf"^{P}(?:sleep|lock)(?:\s+(?:the\s+)?(?:screen|display|mac))?$",
         "sleep_display", "lock the screen"),
    rule(
        rf"^{P}keep\s+(?:me\s+|the\s+mac\s+)?awake(?:\s+for\s+(?P<minutes>[\w\s-]+?)"
        r"(?:\s+minutes?)?)?$",
        "caffeinate",
        "keep awake for 30 minutes",
        build=lambda m: {"minutes": to_number(m.group("minutes") or "", 60) or 60},
    ),

    # ------------------------------------------------------------------ apps
    rule(
        rf"^{P}(?:open|launch|start)\s+(?P<name>.+?)$",
        "open_app",
        "open Safari",
        build=_clean_groups,
    ),
    rule(
        rf"^{P}(?:quit|close)\s+(?P<name>.+?)$",
        "quit_app",
        "quit Safari",
        build=_clean_groups,
    ),
    rule(rf"^{P}(?:what(?:'s| is)?\s+)?(?:running|open)\??$",
         "list_running_apps", "what's running"),

    # ----------------------------------------------------------------- files
    rule(
        rf"^{P}(?:find|search\s+for)\s+(?:a\s+|the\s+)?(?:file\s+)?(?:called\s+)?(?P<query>.+)$",
        "find_files",
        "find file called invoice",
        build=lambda m: {"query": m.group("query").strip(), "match": "name"},
    ),
    rule(
        rf"^{P}what\s+(?:did\s+i|files?)\s+(?:change|changed)\s*(?:recently|today)?\??$",
        "recent_files",
        "what files changed recently",
        build=lambda m: {"hours": 24},
    ),

    # ------------------------------------------------------------- clipboard
    rule(rf"^{P}(?:what(?:'s| is)?\s+(?:on|in)\s+)?{MY}clipboard\??$",
         "clipboard_read", "what's on my clipboard"),
    rule(
        rf"^{P}copy\s+(?P<text>.+)$",
        "clipboard_write",
        "copy hello world",
        build=_clean_groups,
    ),

    # ---------------------------------------------------------------- screen
    rule(rf"^{P}(?:take\s+(?:a\s+)?)?screenshot$", "screenshot", "take a screenshot",
         build=lambda m: {"mode": "screen"}),
    rule(
        rf"^{P}(?:read|what(?:'s| is)?\s+on)\s+(?:my\s+)?screen\??$",
        "screen_text",
        "read my screen",
        build=lambda m: {"fast": True},
    ),

    # ----------------------------------------------------------------- music
    rule(rf"^{P}(?:play|resume)(?:\s+music)?$", "music_control", "play music",
         build=lambda m: {"action": "play"}),
    rule(rf"^{P}(?:pause|stop)(?:\s+music)?$", "music_control", "pause music",
         build=lambda m: {"action": "pause"}),
    rule(rf"^{P}(?:next|skip)(?:\s+(?:song|track))?$", "music_control", "next track",
         build=lambda m: {"action": "next"}),
    rule(
        rf"^{P}play\s+(?P<query>.+)$",
        "music_play_search",
        "play Rubber Soul",
        build=_clean_groups,
    ),

    # ---------------------------------------------------------------- memory
    rule(
        rf"^{P}remember\s+(?:that\s+)?(?P<text>.+)$",
        "remember",
        "remember that I prefer 24-hour time",
        build=lambda m: {"text": m.group("text").strip(), "kind": "profile"},
    ),
    rule(
        rf"^{P}what\s+do\s+you\s+(?:know|remember)\s*(?:about\s+(?P<query>.+))?\??$",
        "recall",
        "what do you remember about Sarah",
        build=lambda m: {"query": (m.group("query") or "").strip()},
    ),
    rule(rf"^{P}what\s+have\s+you\s+done\??$", "audit_trail", "what have you done"),

    # ------------------------------------------------ answered without a tool
    rule(
        rf"^{P}what(?:'s| is)?\s+the\s+time\??$|^{P}what\s+time\s+is\s+it\??$",
        "",
        "what time is it",
        answer=lambda m: time.strftime("It's %H:%M on %A %d %B."),
    ),
    rule(
        rf"^{P}what(?:'s| is)?\s+(?:the\s+)?date\??$|^{P}what\s+day\s+is\s+it\??$",
        "",
        "what's the date",
        answer=lambda m: time.strftime("Today is %A, %d %B %Y."),
    ),
]


@dataclass
class Outcome:
    matched: bool
    text: str
    tool: str = ""
    needs_confirmation: bool = False


def _relative_volume(direction: int) -> dict:
    """volume up/down needs the current level first."""
    current, _ = registry.call("volume_get", {})
    found = re.search(r"(\d+)", current)
    now = int(found.group(1)) if found else 50
    step = 15 if direction == -1 else -15
    return {"level": max(0, min(100, now + step))}


def normalise(said: str) -> str:
    return " ".join(said.strip().split()).rstrip(".!")


def match_rule(said: str) -> tuple[Rule, re.Match[str]] | None:
    """First rule that matches, with no side effects. The unit of testing."""
    text = normalise(said)
    if not text:
        return None
    for entry in RULES:
        found = entry.pattern.match(text)
        if found is not None:
            return entry, found
    return None


def parse(said: str) -> tuple[str, dict] | None:
    """Resolve an utterance to (tool, args) without running anything."""
    hit = match_rule(said)
    if hit is None:
        return None
    entry, found = hit
    if entry.answer is not None:
        return "", {}
    args = entry.build(found)
    if entry.tool == "volume_set" and args.get("level") in (-1, -2):
        args = {"level": args["level"]}  # resolved at call time against the real volume
    return entry.tool, args


def interpret(said: str) -> Outcome:
    """Match one utterance against the rule table and run it."""
    hit = match_rule(said)
    if hit is None:
        return Outcome(False, _suggest(normalise(said))) if normalise(said) else Outcome(False, "")

    entry, found = hit
    if entry.answer is not None:
        return Outcome(True, entry.answer(found))

    args = entry.build(found)
    # Sentinels from the volume up/down rules.
    if entry.tool == "volume_set" and args.get("level") in (-1, -2):
        args = _relative_volume(args["level"])

    result, is_error = registry.call(entry.tool, args)
    return Outcome(
        matched=True,
        text=result,
        tool=entry.tool,
        needs_confirmation="CONFIRMATION REQUIRED" in result and not is_error,
    )


def _suggest(text: str) -> str:
    """No rule matched — offer the closest phrasings we do know."""
    examples = [entry.example for entry in RULES]
    close = difflib.get_close_matches(text, examples, n=3, cutoff=0.35)
    if not close:
        # Fall back to word overlap, which catches "sarah message" style input.
        words = set(text.lower().split())
        scored = sorted(
            ((len(words & set(e.lower().split())), e) for e in examples), reverse=True
        )
        close = [e for score, e in scored[:3] if score > 0]
    hint = "\n".join(f"  {e}" for e in close) if close else ""
    return (
        f"I don't have a rule for that.\n"
        + (f"Closest things I do understand:\n{hint}\n" if hint else "")
        + "Run `jeeves local --list` for everything, or use `jeeves chat` for "
        "free-form requests."
    )


def confirm_at_terminal(preview: str) -> bool:
    """Ask the human directly. Stronger than the AI-mediated gate."""
    print(f"\n{human_preview(preview)}\n")
    try:
        answer = input("Go ahead? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def human_preview(text: str) -> str:
    """Strip the instructions aimed at a model — a person is reading this."""
    for marker in ("\n\nDescribe this to the user", "\nDescribe this to the user"):
        head, _, _ = text.partition(marker)
        if head != text:
            return head.strip()
    return text.strip()


def handle(said: str, speak: bool = False) -> Outcome:
    """Interpret, confirm if needed, report."""
    outcome = interpret(said)

    if outcome.needs_confirmation:
        if not confirm_at_terminal(outcome.text):
            print("Cancelled.")
            return Outcome(True, "Cancelled.")
        # Re-run with explicit approval from the person at the keyboard.
        hit = match_rule(said)
        if hit is not None:
            entry, found = hit
            args = entry.build(found)
            args["confirm"] = True
            text, _ = registry.call(entry.tool, args)
            outcome = Outcome(True, text, entry.tool)

    print(outcome.text)
    if speak and outcome.matched:
        from .voice import speakable

        mac.speak(speakable(outcome.text, 400), blocking=True)
    return outcome


# ------------------------------------------------------------------ entry points


def list_rules() -> int:
    print("Local mode understands these, with no AI involved:\n")
    by_tool: dict[str, list[str]] = {}
    for entry in RULES:
        by_tool.setdefault(entry.tool or "(answered directly)", []).append(entry.example)
    for tool_name in sorted(by_tool):
        print(f"  \033[1m{tool_name}\033[0m")
        for example in by_tool[tool_name]:
            print(f"    {example}")
    print(f"\n{len(RULES)} rules over {len(registry.REGISTRY)} tools.")
    print("Names, message text and numbers are yours to vary — the wording around")
    print("them is what has to match. Add rules in src/jeeves/local.py.")
    return 0


def run_once(said: str, speak: bool = False) -> int:
    outcome = handle(said, speak=speak)
    return 0 if outcome.matched else 1


def repl(speak: bool = False) -> int:
    print("Jeeves — local mode. No AI, no network, no cost.")
    print("Type a command, /list to see them all, /quit to leave.\n")
    while True:
        try:
            said = input("\033[38;5;114mlocal\033[0m  ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not said:
            continue
        if said in {"/quit", "/exit", "/q"}:
            break
        if said in {"/list", "/help"}:
            list_rules()
            continue
        handle(said, speak=speak)
        print()
    return 0


def voice_loop() -> int:
    """Fully offline voice assistant: on-device speech in, speech out."""
    from . import config
    from .voice import listen

    silence = float(config.load().get("voice.silence_timeout", 1.4))
    print("Jeeves — local voice mode. Entirely on-device. Say 'stop' to finish.\n")
    mac.speak("Local mode. At your service.", blocking=True)

    while True:
        print("Listening…", flush=True)
        try:
            said = listen(silence)
        except PermissionError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        if not said:
            continue
        print(f"you    {said}")
        if said.strip().lower().rstrip(".!") in {"stop", "quit", "exit", "goodbye"}:
            mac.speak("Very good.", blocking=True)
            break
        handle(said, speak=True)
        print()
    return 0
