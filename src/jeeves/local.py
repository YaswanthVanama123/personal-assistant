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
from urllib.parse import quote_plus

from . import config, mac
from .mcp import registry
from .mcp import tools as _tools  # noqa: F401  (registers every tool)

SITES = {
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "twitter": "https://twitter.com",
    "reddit": "https://www.reddit.com",
    "maps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "calendar.google": "https://calendar.google.com",
}

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
    # "send a message to X on whatsapp" with no message: ask for the text
    # rather than guessing, and remember who it is for.
    rule(
        rf"^{P}(?:send\s+(?:a\s+)?(?:message|msg|text)\s+to|message|text|write\s+to)\s+"
        r"(?P<chat>.+?)(?:\s+(?:on|in|via)\s+whats\s?app)?$",
        "whatsapp_send",
        "send a message to Sarah on WhatsApp",
        build=lambda m: {"chat": m.group("chat").strip()},
    ),
    rule(
        rf"^{P}(?:send|whats\s?app)\s+(?P<text>.+?)\s+to\s+(?P<chat>.+?)"
        r"(?:\s+(?:on|in|via)\s+whats\s?app)?$",
        "whatsapp_send",
        "send hi to Sarah on WhatsApp",
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

    # --------------------------------------------------------- browser tabs
    # These exist so "close all my tabs" never becomes quit_app.
    rule(
        rf"^{P}close\s+(?:all\s+(?:of\s+)?)(?:my\s+|the\s+)?tabs?"
        r"(?:\s+(?:in|on|of)\s+(?P<browser>.+))?$",
        "browser_close_all_tabs",
        "close all tabs",
        build=lambda m: {"browser": (m.group("browser") or "").strip()},
    ),
    rule(
        rf"^{P}(?:remove|close)\s+(?:all\s+)?(?:the\s+)?tabs?\s+"
        r"(?:currently\s+)?(?:open\s+)?(?:in|on)\s+(?P<browser>.+)$",
        "browser_close_all_tabs",
        "remove all the tabs in Google Chrome",
        build=lambda m: {"browser": m.group("browser").strip()},
    ),
    rule(
        rf"^{P}close\s+(?:this\s+|the\s+|current\s+)?tab"
        r"(?:\s+(?:in|on)\s+(?P<browser>.+))?$",
        "browser_close_tab",
        "close this tab",
        build=lambda m: {"browser": (m.group("browser") or "").strip()},
    ),
    rule(
        rf"^{P}(?:open\s+(?:a\s+)?)?new\s+tab(?:\s+(?:in|on)\s+(?P<browser>.+))?$",
        "browser_new_tab",
        "new tab",
        build=lambda m: {"browser": (m.group("browser") or "").strip()},
    ),
    rule(
        rf"^{P}(?:list|show|what(?:'s| is)?\s+in)\s+(?:my\s+|the\s+)?tabs?"
        r"(?:\s+(?:in|on)\s+(?P<browser>.+))?$",
        "browser_list_tabs",
        "list my tabs",
        build=lambda m: {"browser": (m.group("browser") or "").strip()},
    ),
    rule(
        rf"^{P}switch\s+to\s+(?:the\s+)?tab\s+(?P<index>\d+)$",
        "browser_switch_tab",
        "switch to tab 3",
        build=lambda m: {"index": int(m.group("index"))},
    ),
    rule(
        rf"^{P}switch\s+to\s+(?:the\s+)?(?P<match>.+?)\s+tab$",
        "browser_switch_tab",
        "switch to the gmail tab",
        build=lambda m: {"match": m.group("match").strip()},
    ),

    # ------------------------------------------------------------------- web
    # "search X on youtube" / "search X in youtube" — destination at the end.
    rule(
        rf"^{P}(?:search|find|play|look\s+up)\s+(?:for\s+)?(?P<q>.+?)\s+"
        r"(?:on|in|at)\s+(?:the\s+)?(?:youtube|you\s?tube)$",
        "open_url",
        "search Telugu item songs on YouTube",
        build=lambda m: {
            "url": "https://www.youtube.com/results?search_query="
            + quote_plus(m.group("q").strip())
        },
    ),
    rule(
        rf"^{P}(?:search|find|look\s+up)\s+(?:for\s+)?(?P<q>.+?)\s+"
        r"(?:on|in)\s+(?:the\s+)?google$",
        "open_url",
        "search flight times on Google",
        build=lambda m: {
            "url": "https://www.google.com/search?q=" + quote_plus(m.group("q").strip())
        },
    ),
    # Bare "search youtube" with no query: just open it.
    rule(
        rf"^{P}(?:search|open|go\s+to)\s+(?:the\s+)?(?:youtube|you\s?tube)$",
        "open_url",
        "search youtube",
        build=lambda m: {"url": "https://www.youtube.com"},
    ),
    rule(
        rf"^{P}(?:open\s+)?(?:youtube|you\s?tube)\s+(?:and\s+)?(?:search|play|look)"
        r"(?:\s+(?:for|up))?\s+(?P<q>.+)$",
        "open_url",
        "youtube search lofi beats",
        build=lambda m: {
            "url": "https://www.youtube.com/results?search_query="
            + quote_plus(m.group("q").strip())
        },
    ),
    rule(
        rf"^{P}(?:search|look\s+up)\s+(?:on\s+)?(?:youtube|you\s?tube)\s+"
        r"(?:for\s+)?(?P<q>.+)$",
        "open_url",
        "search youtube for lofi beats",
        build=lambda m: {
            "url": "https://www.youtube.com/results?search_query="
            + quote_plus(m.group("q").strip())
        },
    ),
    rule(
        rf"^{P}(?:google|search\s+(?:the\s+)?(?:web|google)\s+(?:for\s+)?)(?P<q>.+)$",
        "open_url",
        "google flight times to tokyo",
        build=lambda m: {
            "url": "https://www.google.com/search?q=" + quote_plus(m.group("q").strip())
        },
    ),
    rule(
        rf"^{P}open\s+(?:the\s+)?(?:website\s+|site\s+)?(?P<url>https?://\S+)$",
        "open_url",
        "open https://example.com",
        build=_clean_groups,
    ),
    rule(
        rf"^{P}(?:open|go\s+to)\s+(?:the\s+)?(?P<site>youtube|gmail|github|twitter|reddit|"
        r"maps|drive|calendar\.google)(?:\.com)?$",
        "open_url",
        "open youtube",
        build=lambda m: {"url": SITES[m.group("site").lower()]},
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



# ---------------------------------------------------------------- normalising
#
# Speech does not arrive as canned phrases. People say "can you please open
# WhatsApp", "what is the date today", "what's the time now" — filler and
# politeness wrapped around an intent the table already has. Rather than writing
# a rule per phrasing, each utterance is reduced through a series of
# progressively more aggressive rewrites, and every rule is tried against each
# variant until one matches.
#
# This is not understanding. It is normalisation, and it has a ceiling: a request
# whose *meaning* is not in the table will still miss. But it turns dozens of
# near-misses into hits.

# Politeness and hedging that carries no meaning, stripped from the front.
LEADING_NOISE = (
    "yeah then", "yes then", "ok then", "okay then", "alright then", "and then",
    "so then", "yeah", "yep", "yes", "ok", "okay", "alright", "right",
    "can you please", "could you please", "would you please", "can you", "could you",
    "would you", "will you", "please", "kindly", "i want you to", "i need you to",
    "i would like you to", "hey jeeves", "hi jeeves", "ok jeeves", "okay jeeves",
    "jeeves", "hey", "guess", "um", "uh", "so", "just", "now",
)

# Trailing words that do not change which tool is wanted.
TRAILING_NOISE = (
    "please", "for me", "right now", "now", "today", "thanks", "thank you",
    "will you", "would you", "ok", "okay",
)

# Both directions, because speech-to-text is inconsistent about contractions.
CONTRACTIONS = {
    "what is": "what's", "what has": "what's", "that is": "that's",
    "it is": "it's", "let us": "let's", "do not": "don't",
    "cannot": "can not", "i am": "i'm", "i will": "i'll", "i would": "i'd",
    "who is": "who's", "when is": "when's", "where is": "where's",
    "how is": "how's", "there is": "there's",
}
EXPANSIONS = {v: k for k, v in CONTRACTIONS.items()}

# Transcription frequently drops apostrophes. Deliberately excludes "its",
# "ill" and "id", which are real words that would be mangled.
APOSTROPHES = {
    "whats": "what's", "wheres": "where's", "hows": "how's", "thats": "that's",
    "lets": "let's", "dont": "don't", "im": "i'm", "whos": "who's",
    "whens": "when's", "theres": "there's", "youre": "you're", "cant": "can't",
}

# Words people use interchangeably for the same intent.
SYNONYMS = {
    "the time": "what time is it",
    "what's the time": "what time is it",
    "time now": "what time is it",
    "current time": "what time is it",
    "what's today's date": "what's the date",
    "today's date": "what's the date",
    "what day is today": "what's the date",
    "launch": "open",
    "start up": "open",
    "bring up": "open",
    "shut": "quit",
    "close down": "quit",
    "text messages": "messages",
    "whats app": "whatsapp",
    "whatsup": "whatsapp",
    "whats up": "whatsapp",
}


# Case is preserved throughout: a contact name, an app name or the body of a
# message all travel through these rewrites, and lowercasing them would send
# "on my way" to "sarah" instead of "Sarah". Only the function words being
# rewritten are matched case-insensitively.


def _strip_edges(text: str) -> str:
    changed = True
    while changed:
        changed = False
        lowered = text.lower()
        for phrase in LEADING_NOISE:
            if lowered == phrase:
                continue
            if lowered.startswith(phrase + " "):
                text = text[len(phrase) + 1:]
                lowered = text.lower()
                changed = True
        for phrase in TRAILING_NOISE:
            if lowered == phrase:
                continue
            if lowered.endswith(" " + phrase):
                text = text[: -(len(phrase) + 1)]
                lowered = text.lower()
                changed = True
    return text.strip()


def _swap(text: str, table: dict[str, str]) -> str:
    for source, target in table.items():
        text = re.sub(rf"\b{re.escape(source)}\b", target, text, flags=re.IGNORECASE)
    return text


def variants(said: str) -> list[str]:
    """Progressively normalised forms of an utterance, most literal first."""
    base = " ".join(said.strip().split()).strip(" .!?,")
    seen: list[str] = []

    def add(candidate: str) -> None:
        candidate = " ".join(candidate.split()).strip()
        if candidate and candidate not in seen:
            seen.append(candidate)

    add(base)
    base = _swap(base, APOSTROPHES)
    add(base)
    add(_swap(base, SYNONYMS))
    add(_swap(base, CONTRACTIONS))
    add(_swap(base, EXPANSIONS))
    stripped = _strip_edges(base)
    add(stripped)
    add(_swap(stripped, SYNONYMS))
    add(_swap(stripped, CONTRACTIONS))
    add(_swap(stripped, EXPANSIONS))
    add(_strip_edges(_swap(_swap(base, SYNONYMS), CONTRACTIONS)))
    add(_strip_edges(_swap(_swap(base, SYNONYMS), EXPANSIONS)))
    return seen


# Words too common to identify an intent.
STOPWORDS = frozenset(
    "a an the my your is it to of for on in at do does what which that this "
    "and or me you i we are was be been being have has had can could would "
    "will shall should please now today there here how".split()
)


def _significant(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if w not in STOPWORDS and len(w) > 1}


def _keyword_match(text: str) -> Rule | None:
    """Last resort for intents that take no arguments.

    Rules with capture groups are excluded: a fuzzy match cannot tell us what to
    put in the slot, and guessing a recipient or a filename would be worse than
    admitting defeat.
    """
    wanted = _significant(text)
    if not wanted:
        return None

    scored: list[tuple[float, Rule]] = []
    for entry in RULES:
        if entry.pattern.groupindex:
            continue
        keys = _significant(entry.example)
        if not keys:
            continue
        overlap = len(wanted & keys)
        if not overlap:
            continue
        # Reward covering the rule's keywords; penalise unexplained extra words.
        score = overlap / len(keys) - 0.12 * len(wanted - keys)
        scored.append((score, entry))

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best, runner_up = scored[0], scored[1] if len(scored) > 1 else None
    if best[0] < 0.55:
        return None
    # Ambiguous between two intents: better to ask than to guess wrong.
    if runner_up is not None and best[0] - runner_up[0] < 0.15:
        return None
    return best[1]



# ------------------------------------------------------------------ pipeline
#
# Routing order, most deterministic first:
#
#   1. normalise      strip filler, fix apostrophes, map synonyms
#   2. aliases        "chrome" -> "Google Chrome" (in resolve_app)
#   3. split          "open Chrome and open YouTube" -> two commands
#   4. qualifiers     "search X in Chrome" -> query X, destination Chrome
#   5. match + run    the rule table
#   6. fallback       hand to a model only if all of the above failed
#
# A local model is a poor planner for "open Chrome". It should only ever see what
# the deterministic layers could not resolve.

SPLIT_MARKERS = (" and then ", " and also ", " then ", " and ")

# "in Chrome", "using Safari", "on Firefox" — a destination, not part of a query.
BROWSER_NAMES = (
    "google chrome", "chrome", "safari", "firefox", "brave browser", "brave",
    "microsoft edge", "edge", "arc", "vivaldi", "opera",
)
BROWSER_QUALIFIER = re.compile(
    r"\s+(?:in|on|using|with|via|through)\s+(?:the\s+)?(" + "|".join(BROWSER_NAMES) + r")\s*$",
    re.IGNORECASE,
)


def extract_browser(said: str) -> tuple[str, str]:
    """Peel a trailing browser destination off an utterance.

    "Search YouTube in Google Chrome" -> ("Search YouTube", "Google Chrome").
    Without this the browser name is swallowed into the search query.
    """
    found = BROWSER_QUALIFIER.search(said)
    if not found:
        return said, ""
    return said[: found.start()].strip(), found.group(1).strip()


def split_compound(said: str, depth: int = 0) -> list[str]:
    """Split "A and B" into parts, but only when both parts are real commands.

    The guard matters: "search for lofi and chill beats" must stay whole, while
    "open Chrome and open YouTube" must not. A split is accepted only if both
    halves independently match a rule.
    """
    if depth > 3:
        return [said]
    lowered = said.lower()
    for marker in SPLIT_MARKERS:
        start = 0
        while True:
            position = lowered.find(marker, start)
            if position < 0:
                break
            left = said[:position].strip()
            right = said[position + len(marker):].strip()
            if left and right and match_rule(left) and match_rule(right):
                return (
                    split_compound(left, depth + 1) + split_compound(right, depth + 1)
                )
            start = position + 1
    return [said]


# When a slot is missing, remember what we were about to do so the next thing
# said can fill it, rather than guessing or looping.
_pending: dict | None = None


def pending() -> dict | None:
    return _pending


def clear_pending() -> None:
    global _pending
    _pending = None


# Slots that must be present before a tool is worth calling, and what to ask.
REQUIRED_SLOTS = {
    "whatsapp_send": ("text", "What would you like me to send to {chat}?"),
    "imessage_send": ("text", "What should I say to {recipient}?"),
    "mail_draft": ("body", "What should the email say?"),
}


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
    """First rule that matches any normalised form. No side effects.

    Tries each variant in turn, most literal first, so an exact phrasing always
    wins over a rewritten one. Falls back to keyword scoring for argument-free
    intents.
    """
    if not normalise(said):
        return None
    for candidate in variants(said):
        for entry in RULES:
            found = entry.pattern.match(candidate)
            if found is not None:
                return entry, found

    entry = _keyword_match(normalise(said))
    if entry is not None:
        # Synthesise an empty match; these rules take no arguments by definition.
        return entry, re.match(r"", "")
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


def _fallback(said: str, on_tool=None) -> Outcome | None:
    """Hand an unmatched utterance to a model, if one is configured."""
    backend = str(config.load().get("brain.fallback", "none")).lower()
    if backend in ("", "none"):
        return None

    if backend == "ollama":
        from . import brain as local_brain

        reply = local_brain.Brain().ask(said, on_tool=on_tool)
        if reply.error:
            return Outcome(True, f"(local model) {reply.error}", "brain")
        return Outcome(True, reply.text, "brain")

    if backend == "claude":
        from . import agent

        turn = agent.one_shot(said)
        if turn.error:
            return Outcome(True, f"(claude) {turn.error}", "brain")
        return Outcome(True, turn.reply, "brain")

    return Outcome(True, f"unknown brain.fallback: {backend!r}", "brain")


def swallowed_separator(entry: "Rule", found: "re.Match[str]") -> bool:
    """Did a whole-utterance match absorb a separator into one of its slots?

    This is what distinguishes the two compound cases:

      "open youtube and search for lofi beats"
          the youtube-search rule consumes the "and" in its own pattern, so the
          captured query is just "lofi beats" — one intent, do not split.

      "Open Google Chrome and open the YouTube"
          the catch-all `open <name>` rule captures the whole tail as an
          application name, separator included — two intents, split.
    """
    if entry.answer is not None:
        return False
    try:
        values = entry.build(found)
    except Exception:  # noqa: BLE001 - a build error is not our concern here
        return False
    for value in values.values():
        if not isinstance(value, str):
            continue
        padded = f" {value.lower()} "
        if any(marker in padded for marker in SPLIT_MARKERS):
            return True
    return False


def resolve_one(said: str) -> tuple[Rule, re.Match[str], str] | None:
    """Find the rule for a single command, plus any browser destination.

    The browser qualifier is stripped first and deliberately: "Search YouTube in
    Google Chrome" otherwise matches the youtube-search rule with a query of
    "in Google Chrome".
    """
    stripped, browser = extract_browser(said)
    if browser:
        hit = match_rule(stripped)
        if hit is not None:
            return hit[0], hit[1], browser

    hit = match_rule(said)
    if hit is not None:
        return hit[0], hit[1], ""
    return None


def _run_rule(entry: Rule, found: re.Match[str], browser: str = "") -> Outcome:
    """Execute one matched rule, filling in a browser destination if given."""
    global _pending

    if entry.answer is not None:
        return Outcome(True, entry.answer(found))

    tool_name = entry.tool
    args = entry.build(found)

    # A browser destination turns a plain open into a targeted one.
    if browser:
        if tool_name == "open_url":
            tool_name = "browser_open_url"
            args = {"url": args.get("url", ""), "browser": browser}
        elif "browser" in registry.REGISTRY[tool_name].schema["properties"]:
            args["browser"] = browser

    # Sentinels from the volume up/down rules.
    if tool_name == "volume_set" and args.get("level") in (-1, -2):
        args = _relative_volume(args["level"])

    # Missing a slot the tool cannot work without: ask, and remember why.
    slot, question = REQUIRED_SLOTS.get(tool_name, ("", ""))
    if slot and not str(args.get(slot) or "").strip():
        _pending = {"tool": tool_name, "args": args, "slot": slot}
        try:
            prompt_text = question.format(**args)
        except KeyError:
            prompt_text = question
        return Outcome(True, prompt_text, tool_name)

    result, is_error = registry.call(tool_name, args)
    return Outcome(
        matched=True,
        text=result,
        tool=tool_name,
        needs_confirmation="CONFIRMATION REQUIRED" in result and not is_error,
    )


def interpret(said: str, on_tool=None) -> Outcome:
    """Resolve and run a single command. See the pipeline note above."""
    global _pending

    text = normalise(said)
    if not text:
        return Outcome(False, "")

    # A question was asked and this is the answer: fill the waiting slot.
    if _pending is not None:
        waiting = _pending
        _pending = None
        if match_rule(said) is None:  # not a new command, so treat as the value
            args = dict(waiting["args"])
            args[waiting["slot"]] = said.strip()
            result, is_error = registry.call(waiting["tool"], args)
            return Outcome(
                True,
                result,
                waiting["tool"],
                needs_confirmation="CONFIRMATION REQUIRED" in result and not is_error,
            )

    resolved = resolve_one(said)
    if resolved is not None:
        entry, found, browser = resolved
        return _run_rule(entry, found, browser=browser)

    handled = _fallback(said, on_tool=on_tool)
    return handled if handled is not None else Outcome(False, _suggest(text))


def route(said: str, on_tool=None) -> list[Outcome]:
    """Run an utterance, splitting it only if it is not a single command.

    Whole-utterance rules take precedence: "open youtube and search for lofi
    beats" is one search, even though both halves happen to parse separately.
    """
    whole = " ".join(said.strip().split())
    resolved = resolve_one(whole)

    if _pending is None and resolved is not None:
        entry, found, _browser = resolved
        if not swallowed_separator(entry, found):
            return [interpret(whole, on_tool=on_tool)]
        parts = split_compound(whole)
        if len(parts) > 1:
            return [interpret(part, on_tool=on_tool) for part in parts]
        return [interpret(whole, on_tool=on_tool)]

    return [interpret(part, on_tool=on_tool) for part in split_compound(whole)]


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

    def show_tool(name: str, args: dict) -> None:
        detail = ", ".join(f"{k}={str(v)[:30]}" for k, v in args.items()) if args else ""
        print(f"  \033[2m\u25b8 {name} {detail}\033[0m", flush=True)

    outcomes = route(said, on_tool=show_tool)
    spoken: list[str] = []

    for index, outcome in enumerate(outcomes):
        if outcome.needs_confirmation:
            if confirm_at_terminal(outcome.text):
                outcome = _confirm_and_rerun(said, outcomes, index)
            else:
                print("Cancelled.")
                outcomes[index] = Outcome(True, "Cancelled.")
                continue
            outcomes[index] = outcome
        print(outcome.text)
        if outcome.matched and outcome.text:
            spoken.append(outcome.text)

    if speak and spoken:
        from .voice import speakable

        mac.speak(speakable(" ".join(spoken), 400), blocking=True)
    return outcomes[-1] if outcomes else Outcome(False, "")


def _confirm_and_rerun(said: str, outcomes: list[Outcome], index: int) -> Outcome:
    """Re-run one part of a routed utterance with the user's approval attached."""
    parts = split_compound(" ".join(said.strip().split()))
    part = parts[index] if index < len(parts) else said
    hit = match_rule(part)
    browser = ""
    if hit is None:
        stripped, browser = extract_browser(part)
        hit = match_rule(stripped)
    if hit is None:
        return outcomes[index]

    entry, found = hit
    tool_name = entry.tool
    args = entry.build(found)
    if browser and tool_name == "open_url":
        tool_name, args = "browser_open_url", {"url": args.get("url", ""), "browser": browser}
    args["confirm"] = True
    text, _ = registry.call(tool_name, args)
    return Outcome(True, text, tool_name)


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
