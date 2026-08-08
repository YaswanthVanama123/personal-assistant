"""Applications: launch, quit, focus, list, and open URLs or files with them."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from ...mac import check_path, run, tell
from ..registry import READ, RISKY, WRITE, ToolError, boolean, string, tool

# Two ways to enumerate running GUI apps, neither of which needs Automation
# permission. `lsappinfo` is preferred: some managed Macs deny `ps` outright.
_APP_MARKER = ".app/Contents/MacOS/"
_BUNDLE_PATH = re.compile(r'bundle path="([^"]+\.app)"')


def running_apps() -> list[str]:
    names: set[str] = set()

    listing = run(["lsappinfo", "list"], timeout=25)
    if listing.ok and listing.out:
        for match in _BUNDLE_PATH.finditer(listing.out):
            names.add(Path(match.group(1)).name.removesuffix(".app"))
        if names:
            return sorted(names)

    # Fall back to parsing the process table: GUI apps appear as
    # /Applications/Foo.app/Contents/MacOS/Foo.
    table = run(["ps", "-Ao", "comm="], timeout=25)
    if table.ok:
        for line in table.out.splitlines():
            if _APP_MARKER in line:
                bundle = line.split(_APP_MARKER, 1)[0]
                names.add(Path(bundle).name.removesuffix(".app"))
    return sorted(names)


# What people say, versus what the bundle is called. Nobody says
# "Google Chrome" out loud, and `open -a chrome` fails.
ALIASES = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "brave": "Brave Browser",
    "edge": "Microsoft Edge",
    "code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "whatsapp": "WhatsApp",
    "whats app": "WhatsApp",
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint",
    "outlook": "Microsoft Outlook",
    "teams": "Microsoft Teams",
    "vlc": "VLC",
    "iterm": "iTerm",
    "activity monitor": "Activity Monitor",
    "system settings": "System Settings",
    "preferences": "System Settings",
    "system preferences": "System Settings",
    "app store": "App Store",
    "quicktime": "QuickTime Player",
    "textedit": "TextEdit",
    "text edit": "TextEdit",
    "terminal": "Terminal",
    "finder": "Finder",
    "calendar": "Calendar",
    "reminders": "Reminders",
    "notes": "Notes",
    "mail": "Mail",
    "messages": "Messages",
    "music": "Music",
    "spotify": "Spotify",
    "slack": "Slack",
    "zoom": "zoom.us",
    "xcode": "Xcode",
    "photos": "Photos",
    "safari": "Safari",
    "chatgpt": "ChatGPT",
    "docker": "Docker Desktop",
    "postman": "Postman",
}


def resolve_app(name: str) -> str:
    """Confirm an application exists, returning its canonical name.

    Spoken names are resolved through ALIASES first, then tried directly, then
    matched case-insensitively against what is running, then matched as a prefix
    of an installed app.
    """
    wanted = " ".join(name.strip().split())
    if not wanted:
        raise ToolError("no application name was given")

    canonical = ALIASES.get(wanted.lower())
    if canonical and run(["open", "-Ra", canonical]).ok:
        return canonical

    if run(["open", "-Ra", wanted]).ok:
        return wanted

    running = running_apps()
    for candidate in running:
        if candidate.lower() == wanted.lower():
            return candidate

    # "chrome" -> "Google Chrome": match a whole word inside an installed name.
    installed = installed_apps()
    lowered = wanted.lower()
    exact = [a for a in installed if a.lower() == lowered]
    if exact:
        return exact[0]
    worded = [a for a in installed if lowered in a.lower().split()]
    if len(worded) == 1:
        return worded[0]
    partial = [a for a in installed if lowered in a.lower()]
    if len(partial) == 1:
        return partial[0]

    options = partial or [a for a in running if lowered in a.lower()]
    hint = f" Did you mean: {', '.join(sorted(options)[:6])}?" if options else ""
    raise ToolError(f"no application named {name!r} is installed.{hint}")


@lru_cache(maxsize=1)
def installed_apps() -> list[str]:
    """Application names from the usual install locations."""
    names: set[str] = set()
    for folder in (
        Path("/Applications"),
        Path("/Applications/Utilities"),
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
        Path.home() / "Applications",
    ):
        try:
            for entry in folder.iterdir():
                if entry.suffix == ".app":
                    names.add(entry.stem)
        except OSError:
            continue
    return sorted(names)


@tool("list_running_apps", "List the GUI applications that are currently running.", risk=READ)
def list_running_apps() -> str:
    apps = running_apps()
    return f"{len(apps)} running:\n" + "\n".join(f"- {a}" for a in apps)


@tool(
    "open_app",
    "Launch an application, or bring it to the front if already running.",
    {
        "name": string("Application name, e.g. 'Safari', 'Xcode', 'Music'."),
        "background": boolean("Launch without stealing focus.", default=False),
    },
    required=["name"],
    risk=WRITE,
    undo="quit_app with the same name",
)
def open_app(name: str, background: bool = False) -> str:
    app = resolve_app(name)
    argv = ["open", "-a", app]
    if background:
        argv.insert(1, "-g")
    result = run(argv)
    if not result.ok:
        raise ToolError(f"could not open {app}: {result.err}")
    return f"{'Launched' if background else 'Opened'} {app}."


@tool(
    "quit_app",
    "Ask an application to quit. Unsaved changes may prompt the app's own dialog.",
    {"name": string("Application name.")},
    required=["name"],
    risk=WRITE,
    undo="open_app with the same name",
    needs_automation=True,
)
def quit_app(name: str) -> str:
    app = resolve_app(name)
    result = tell(app, "quit", timeout=20)
    if not result.ok:
        raise ToolError(
            f"could not quit {app}: {result.err}. This needs Automation "
            "permission — run `jeeves doctor`."
        )
    return f"Asked {app} to quit."


@tool(
    "force_quit_app",
    "Force-quit an unresponsive application. Unsaved work WILL be lost.",
    {"name": string("Application name.")},
    required=["name"],
    risk=RISKY,
    preview=lambda name: f"  force-quit {name} — any unsaved work in it is lost immediately",
)
def force_quit_app(name: str) -> str:
    result = run(["pkill", "-x", name])
    if not result.ok:
        result = run(["pkill", "-f", f"{name}.app"])
    if not result.ok:
        raise ToolError(f"no running process matched {name!r}")
    return f"Force-quit {name}."


@tool(
    "focus_app",
    "Bring an already-running application to the front.",
    {"name": string("Application name.")},
    required=["name"],
    risk=WRITE,
)
def focus_app(name: str) -> str:
    app = resolve_app(name)
    result = run(["open", "-a", app])
    if not result.ok:
        raise ToolError(f"could not focus {app}: {result.err}")
    return f"{app} is now frontmost."


@tool(
    "open_url",
    "Open a URL in the default browser, or in a named browser.",
    {
        "url": string("Absolute URL, including scheme."),
        "browser": string("Optional browser name, e.g. 'Safari' or 'Google Chrome'."),
    },
    required=["url"],
    risk=WRITE,
)
def open_url(url: str, browser: str = "") -> str:
    if not url.startswith(("http://", "https://", "mailto:", "tel:", "facetime:")):
        raise ToolError(
            "url must start with http://, https://, mailto:, tel: or facetime: "
            f"(got {url[:40]!r})"
        )
    argv = ["open"]
    if browser:
        argv += ["-a", resolve_app(browser)]
    argv += ["--", url]
    result = run(argv)
    if not result.ok:
        raise ToolError(f"could not open {url}: {result.err}")
    return f"Opened {url}" + (f" in {browser}." if browser else ".")


@tool(
    "open_path",
    "Open a file or folder with its default application, or a named one.",
    {
        "path": string("File or folder path."),
        "app": string("Optional application to open it with."),
    },
    required=["path"],
    risk=WRITE,
)
def open_path(path: str, app: str = "") -> str:
    target = check_path(path)
    if not target.exists():
        raise ToolError(f"{target} does not exist")
    argv = ["open"]
    if app:
        argv += ["-a", resolve_app(app)]
    argv += ["--", str(target)]
    result = run(argv)
    if not result.ok:
        raise ToolError(f"could not open {target}: {result.err}")
    return f"Opened {target}" + (f" with {app}." if app else ".")


@tool(
    "reveal_in_finder",
    "Reveal a file or folder in a new Finder window.",
    {"path": string("File or folder path.")},
    required=["path"],
    risk=WRITE,
)
def reveal_in_finder(path: str) -> str:
    target = check_path(path)
    if not target.exists():
        raise ToolError(f"{target} does not exist")
    result = run(["open", "-R", "--", str(target)])
    if not result.ok:
        raise ToolError(f"could not reveal {target}: {result.err}")
    return f"Revealed {target} in Finder."
