"""Regression tests from real reported failures.

Every phrase here was observed misrouting in voice mode. Each entry records what
went wrong so a future change cannot quietly reintroduce it.

Parsing and routing only — no tools are executed.
"""

import sys

sys.path.insert(0, "src")

from jeeves import local  # noqa: E402

failures: list[str] = []


def plan(said: str) -> tuple[list[str], list[dict]]:
    """What the router would do, without running anything.

    Mirrors local.route(): a rule matching the whole utterance wins over
    decomposition, and a browser qualifier is peeled off before matching.
    """
    whole = " ".join(said.split())
    resolved = local.resolve_one(whole)
    if resolved is not None and not local.swallowed_separator(resolved[0], resolved[1]):
        parts = [whole]
    else:
        parts = local.split_compound(whole)

    names: list[dict] = []
    tools: list[str] = []
    for part in parts:
        resolved = local.resolve_one(part)
        if resolved is None:
            tools.append("NO MATCH")
            names.append({})
            continue
        entry, found, browser = resolved
        tool = entry.tool or "(direct answer)"
        values = entry.build(found) if entry.answer is None else {}
        if browser and tool == "open_url":
            tool = "browser_open_url"
            values = {"url": values.get("url", ""), "browser": browser}
        tools.append(tool)
        names.append(values)
    return tools, names


def expect(said: str, tools: list[str], because: str, args: list[dict] | None = None) -> None:
    """Assert which tools an utterance routes to, in order."""
    got, got_args = plan(said)

    if got != tools:
        failures.append(f"  {said!r}\n    {because}\n    expected {tools}\n    got      {got}")
        return
    for index, wanted in enumerate(args or []):
        for key, value in wanted.items():
            if got_args[index].get(key) != value:
                failures.append(
                    f"  {said!r}\n    {because}\n"
                    f"    part {index} arg {key}: expected {value!r}, "
                    f"got {got_args[index].get(key)!r}"
                )


# ---------------------------------------------------------------------------
# 1. "chrome" is not an installed app name; the alias must resolve it.
#    Previously: open_app(name="chrome") failed with
#    "Running apps matching that: Google Chrome."
expect(
    "Can you please open chrome",
    ["open_app"],
    "politeness stripped, and 'chrome' handed to the alias resolver",
    [{"name": "chrome"}],  # resolve_app maps this to "Google Chrome"
)

# 2. Conversational lead-in must not derail routing.
#    Previously: the model chose quit_app, then looped on open_url.
expect(
    "Yeah then open Google Chrome",
    ["open_app"],
    "'yeah then' is filler, not a compound command",
    [{"name": "Google Chrome"}],
)

# 3. Closing tabs is not quitting the browser.
#    Previously: quit_app(name="Google Chrome") six times.
expect(
    "Remove all the tabs currently in Google Chrome",
    ["browser_close_all_tabs"],
    "tab removal must never route to quit_app",
    [{"browser": "Google Chrome"}],
)
expect("close all tabs", ["browser_close_all_tabs"], "bare form works too")
expect("close this tab", ["browser_close_tab"], "single tab is a different tool")
expect("new tab", ["browser_new_tab"], "opening a tab is not opening an app")
expect("list my tabs", ["browser_list_tabs"], "reading tabs is read-only")
expect("switch to tab 3", ["browser_switch_tab"], "positional switch", [{"index": 3}])

# 4. Compound commands must decompose.
#    Previously: open_app(name="Google Chrome and open the YouTube")
expect(
    "Open Google Chrome and open the YouTube",
    ["open_app", "open_url"],
    "'and' splits into two commands when both halves are valid",
    [{"name": "Google Chrome"}, {"url": "https://www.youtube.com"}],
)
expect(
    "Open Chrome and open YouTube",
    ["open_app", "open_url"],
    "same, with the alias form",
)
expect(
    "open chrome and then open youtube",
    ["open_app", "open_url"],
    "'and then' is also a separator",
)
expect(
    "mute and lock the screen",
    ["volume_mute", "sleep_display"],
    "splitting is not browser-specific",
)

# A split must NOT happen when the second half is not a command on its own.
expect(
    "search for lofi and chill beats",
    ["find_files"],
    "'and' inside a search phrase must stay part of the query",
    [{"query": "lofi and chill beats"}],
)

# 5. A browser qualifier is a destination, not query text.
#    Previously: search_query=in+Google+Chrome
expect(
    "Search YouTube in Google Chrome",
    ["browser_open_url"],
    "'in Google Chrome' is where to open it, not what to search for",
    [{"url": "https://www.youtube.com", "browser": "Google Chrome"}],
)
expect(
    "open youtube using Safari",
    ["browser_open_url"],
    "'using X' is also a destination",
    [{"browser": "Safari"}],
)

# 6. A search with the site named last must produce a real encoded URL.
#    Previously: shell_check("open -a 'Google Chrome' https:") six times.
expect(
    "Search Telugu items songs in YouTube",
    ["open_url"],
    "site-at-the-end search must build an encoded YouTube URL",
    [{"url": "https://www.youtube.com/results?search_query=Telugu+items+songs"}],
)
expect(
    "Search Telugu item songs on YouTube",
    ["open_url"],
    "'on YouTube' behaves the same as 'in YouTube'",
    [{"url": "https://www.youtube.com/results?search_query=Telugu+item+songs"}],
)
expect(
    "open youtube and search for lofi beats",
    ["open_url"],
    "the leading 'open youtube' form still works",
    [{"url": "https://www.youtube.com/results?search_query=lofi+beats"}],
)

# 7. A send with no message must be a send intent, not a UI hunt.
#    Previously: whatsapp_read / ui_inspect / whatsapp_chats without completing.
expect(
    "Send a message to Everything in WhatsApp",
    ["whatsapp_send"],
    "recognised as a send, with the message still to be asked for",
    [{"chat": "Everything"}],
)
expect(
    "send hi to Everything on WhatsApp",
    ["whatsapp_send"],
    "message and contact both present",
)
expect(
    "message Everything: hi",
    ["whatsapp_send"],
    "colon form",
    [{"chat": "Everything", "text": "hi"}],
)

# ---------------------------------------------------------------------------
# The missing-message dialogue: ask, then use the answer.

local.clear_pending()
outcome = local.interpret("Send a message to Everything in WhatsApp")
if "What would you like me to send to Everything?" not in outcome.text:
    failures.append(f"  missing message should be asked for, got {outcome.text[:90]!r}")
waiting = local.pending()
if not waiting or waiting.get("tool") != "whatsapp_send":
    failures.append(f"  the pending send was not recorded: {waiting}")
elif waiting.get("slot") != "text":
    failures.append(f"  the wrong slot is pending: {waiting}")
elif waiting["args"].get("chat") != "Everything":
    failures.append(f"  the contact was lost: {waiting}")
local.clear_pending()

# A new command while a question is pending must win over slot-filling.
local.interpret("Send a message to Everything in WhatsApp")
follow = local.interpret("whats the time")
if "It's" not in follow.text:
    failures.append(f"  a real command should override a pending slot, got {follow.text[:70]!r}")
local.clear_pending()

# ---------------------------------------------------------------------------
# Aliases resolve to real installed applications.
from jeeves.mcp.tools.apps import ALIASES, resolve_app  # noqa: E402

for spoken, expected in [("chrome", "Google Chrome"), ("vscode", "Visual Studio Code")]:
    if spoken not in ALIASES:
        failures.append(f"  alias {spoken!r} is missing")
        continue
    if ALIASES[spoken] != expected:
        failures.append(f"  alias {spoken!r} maps to {ALIASES[spoken]!r}, expected {expected!r}")
    try:
        got = resolve_app(spoken)
        if got != expected:
            failures.append(f"  resolve_app({spoken!r}) gave {got!r}, expected {expected!r}")
    except Exception as exc:  # app genuinely absent on this machine
        print(f"  note: {spoken!r} not installed here ({str(exc)[:50]}) — alias still checked")

print(f"regression checks complete — {len(failures)} failure(s)")
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("every reported misrouting is now handled deterministically")
