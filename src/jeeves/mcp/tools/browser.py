"""Browser control: tabs, windows and navigation.

Closing a tab is not quitting the browser, and a 7B model asked to "close all
tabs" will reach for `quit_app` unless something better exists. These tools make
the correct action available and deterministic.

Chromium-family browsers (Chrome, Brave, Edge, Arc, Vivaldi) share Chrome's
AppleScript dictionary; Safari's differs, so both dialects are implemented.
"""

from __future__ import annotations

from ...mac import Result, tell_literal
from ..registry import READ, RISKY, WRITE, ToolError, enum, integer, string, tool

CHROMIUM = {
    "google chrome", "chrome", "brave browser", "brave", "microsoft edge",
    "edge", "arc", "vivaldi", "chromium", "opera",
}
SAFARI = {"safari", "webkit"}

DEFAULT_BROWSER = "Google Chrome"


def _family(app: str) -> str:
    lowered = app.lower()
    if lowered in CHROMIUM:
        return "chromium"
    if lowered in SAFARI:
        return "safari"
    # Unknown browser: Chromium's dictionary is the more common one.
    return "chromium"


def _browser(app: str = "") -> str:
    """Resolve a browser name, preferring one that is actually running."""
    from .apps import resolve_app, running_apps

    if app:
        return resolve_app(app)
    running = {name.lower(): name for name in running_apps()}
    for candidate in ("Google Chrome", "Safari", "Brave Browser", "Arc", "Microsoft Edge"):
        if candidate.lower() in running:
            return candidate
    return resolve_app(DEFAULT_BROWSER)


def _run(app: str, body: str, *args: str, timeout: int = 30) -> Result:
    result = tell_literal(app, body, *args, timeout=timeout)
    if not result.ok:
        raise PermissionError(
            f"could not control {app}: {result.err or 'no response'}. Grant your "
            f"terminal permission to control {app} under System Settings → "
            "Privacy & Security → Automation."
        )
    return result


@tool(
    "browser_list_tabs",
    "List the open tabs in a browser window, with their titles and URLs.",
    {
        "browser": string("Browser name. Defaults to whichever is running."),
        "limit": integer("Maximum tabs to list, 1-100.", 1, 100),
    },
    risk=READ,
    needs_automation=True,
)
def browser_list_tabs(browser: str = "", limit: int = 40) -> str:
    app = _browser(browser)
    if _family(app) == "safari":
        body = (
            "set out to {}\n"
            "      tell front window\n"
            "        repeat with t in tabs\n"
            '          set end of out to (name of t) & "  —  " & (URL of t)\n'
            "        end repeat\n"
            "      end tell\n"
            '      set text item delimiters to linefeed\n'
            "      return out as text"
        )
    else:
        body = (
            "set out to {}\n"
            "      set w to front window\n"
            "      repeat with t in tabs of w\n"
            '        set end of out to (title of t) & "  —  " & (URL of t)\n'
            "      end repeat\n"
            '      set text item delimiters to linefeed\n'
            "      return out as text"
        )
    lines = _run(app, body).out
    if not lines:
        return f"{app} has no open tabs."
    listed = lines.splitlines()[:limit]
    return f"{app} — {len(lines.splitlines())} tab(s):\n" + "\n".join(
        f"  {i + 1}. {line}" for i, line in enumerate(listed)
    )


@tool(
    "browser_new_tab",
    "Open a new browser tab, optionally at a URL.",
    {
        "url": string("Optional URL to open in the new tab."),
        "browser": string("Browser name. Defaults to whichever is running."),
    },
    risk=WRITE,
    undo="browser_close_tab",
    needs_automation=True,
)
def browser_new_tab(url: str = "", browser: str = "") -> str:
    app = _browser(browser)
    target = url.strip() or "about:blank"
    if target != "about:blank" and not target.startswith(("http://", "https://")):
        target = "https://" + target

    if _family(app) == "safari":
        body = (
            "activate\n"
            "      tell front window to set current tab to "
            "(make new tab with properties {URL:(item 1 of argv)})"
        )
    else:
        body = (
            "activate\n"
            "      tell front window to make new tab at end of tabs "
            "with properties {URL:(item 1 of argv)}"
        )
    _run(app, body, target)
    return f"Opened a new tab in {app}" + (f" at {target}." if url else ".")


@tool(
    "browser_close_tab",
    "Close the tab currently in front. Does NOT quit the browser.",
    {"browser": string("Browser name. Defaults to whichever is running.")},
    risk=WRITE,
    undo="reopen it with Command-Shift-T in the browser",
    needs_automation=True,
)
def browser_close_tab(browser: str = "") -> str:
    app = _browser(browser)
    body = (
        "tell front window to close current tab"
        if _family(app) == "safari"
        else "tell front window to close active tab"
    )
    _run(app, body)
    return f"Closed the front tab in {app}."


@tool(
    "browser_close_all_tabs",
    "Close every tab in the front window, leaving one blank tab so the browser "
    "stays open. This is what 'close all my tabs' means — it is NOT quit_app.",
    {"browser": string("Browser name. Defaults to whichever is running.")},
    risk=RISKY,
    needs_automation=True,
    preview=lambda browser="": (
        f"  close every tab in {browser or 'the front browser window'}\n"
        "  One blank tab is left behind, so the browser stays open.\n"
        "  Unsaved form input in those tabs is lost; history is not."
    ),
)
def browser_close_all_tabs(browser: str = "") -> str:
    app = _browser(browser)
    if _family(app) == "safari":
        body = (
            "activate\n"
            "      tell front window\n"
            '        set current tab to (make new tab with properties {URL:"about:blank"})\n'
            "        set n to (count of tabs)\n"
            "        repeat with i from (n - 1) to 1 by -1\n"
            "          close tab i\n"
            "        end repeat\n"
            "        return n - 1\n"
            "      end tell"
        )
    else:
        body = (
            "activate\n"
            "      set w to front window\n"
            '      make new tab at end of tabs of w with properties {URL:"about:blank"}\n'
            "      set n to (count of tabs of w)\n"
            "      repeat with i from (n - 1) to 1 by -1\n"
            "        close tab i of w\n"
            "      end repeat\n"
            "      return n - 1"
        )
    closed = _run(app, body, timeout=60).out or "?"
    return f"Closed {closed} tab(s) in {app}, leaving one blank tab open."


@tool(
    "browser_switch_tab",
    "Bring a tab to the front by its position, or by matching its title.",
    {
        "index": integer("1-based tab position. Omit if using `match`.", 1, 200),
        "match": string("Text to find in the tab title or URL."),
        "browser": string("Browser name. Defaults to whichever is running."),
    },
    risk=WRITE,
    needs_automation=True,
)
def browser_switch_tab(index: int = 0, match: str = "", browser: str = "") -> str:
    app = _browser(browser)
    if not index and not match:
        raise ToolError("give either an index or some text to match")

    if match and _family(app) != "safari":
        body = (
            "activate\n"
            "      set q to item 1 of argv\n"
            "      set w to front window\n"
            "      repeat with i from 1 to (count of tabs of w)\n"
            "        set t to tab i of w\n"
            "        if (title of t contains q) or (URL of t contains q) then\n"
            "          set active tab index of w to i\n"
            "          return title of t\n"
            "        end if\n"
            "      end repeat\n"
            '      return "none"'
        )
        found = _run(app, body, match).out
        if found == "none":
            raise ToolError(f"no tab in {app} matched {match!r}")
        return f"Switched {app} to: {found}"

    if match:  # Safari
        body = (
            "activate\n"
            "      set q to item 1 of argv\n"
            "      tell front window\n"
            "        repeat with t in tabs\n"
            "          if (name of t contains q) or (URL of t contains q) then\n"
            "            set current tab to t\n"
            "            return name of t\n"
            "          end if\n"
            "        end repeat\n"
            "      end tell\n"
            '      return "none"'
        )
        found = _run(app, body, match).out
        if found == "none":
            raise ToolError(f"no tab in {app} matched {match!r}")
        return f"Switched {app} to: {found}"

    body = (
        "activate\n      tell front window to set current tab to tab (item 1 of argv as integer)"
        if _family(app) == "safari"
        else "activate\n      set active tab index of front window to (item 1 of argv as integer)"
    )
    _run(app, body, str(index))
    return f"Switched {app} to tab {index}."


@tool(
    "browser_open_url",
    "Open a URL in a specific browser, reusing its front window.",
    {
        "url": string("URL to open."),
        "browser": string("Browser name, e.g. 'Google Chrome'. Defaults to whichever is running."),
        "new_tab": enum("Open in a new tab, or replace the current one.", ["new", "current"]),
    },
    required=["url"],
    risk=WRITE,
    needs_automation=True,
)
def browser_open_url(url: str, browser: str = "", new_tab: str = "new") -> str:
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    if new_tab == "new":
        return browser_new_tab(url=target, browser=browser)

    app = _browser(browser)
    body = (
        "activate\n      tell front window to set URL of current tab to (item 1 of argv)"
        if _family(app) == "safari"
        else "activate\n      set URL of active tab of front window to (item 1 of argv)"
    )
    _run(app, body, target)
    return f"Opened {target} in the front {app} tab."
