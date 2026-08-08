"""Thin, injection-safe bridge to macOS: osascript, shell, Trash, paths.

Every AppleScript in Jeeves is written as an ``on run argv`` handler and given
its data through ``argv``. Nothing is ever string-interpolated into a script, so
a filename containing quotes, backslashes or newlines cannot alter the program.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config

DEFAULT_TIMEOUT = 30


class MacError(RuntimeError):
    """A macOS command failed. Message is safe to show the user."""


@dataclass(slots=True)
class Result:
    ok: bool
    out: str
    err: str
    code: int

    def value(self) -> str:
        if not self.ok:
            raise MacError(self.err or f"command failed with status {self.code}")
        return self.out


# ------------------------------------------------------------------- process


def run(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | None = None,
) -> Result:
    """Run a command without a shell. Never raises on non-zero exit."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv is a list, no shell
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        return Result(False, "", f"{argv[0]}: not found", 127)
    except PermissionError as exc:
        # Managed Macs can deny exec of specific binaries (`ps`, `top`) outright.
        return Result(False, "", f"{argv[0]}: not permitted ({exc.strerror})", 126)
    except NotADirectoryError:
        return Result(False, "", f"{cwd}: not a directory", 126)
    except subprocess.TimeoutExpired:
        return Result(False, "", f"timed out after {timeout}s", 124)
    except OSError as exc:
        return Result(False, "", f"{argv[0]}: {exc}", 1)
    return Result(proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip(), proc.returncode)


def osascript(script: str, *args: str, timeout: int = DEFAULT_TIMEOUT, jxa: bool = False) -> Result:
    """Run an ``on run argv`` AppleScript (or JXA ``run(argv)``) with arguments.

    The script is fed on stdin and the arguments are passed by the kernel, so no
    escaping is required or performed.
    """
    argv = ["osascript"]
    if jxa:
        argv += ["-l", "JavaScript"]
    argv += ["-", *args]
    return run(argv, stdin=script, timeout=timeout)


def tell(app: str, body: str, *args: str, timeout: int = DEFAULT_TIMEOUT) -> Result:
    """Run ``body`` inside ``tell application <app>`` for *standard* commands.

    The app name travels through argv, so it cannot break out of the script. The
    trade-off is that AppleScript cannot resolve application-specific terminology
    against a variable application, so this only works for commands every app
    understands: ``quit``, ``launch``, ``activate``, ``open``. For anything
    app-specific (Finder's ``trash``, Music's ``current track``) use
    :func:`tell_literal`.
    """
    script = (
        "on run argv\n"
        "  set _app to item 1 of argv\n"
        "  set argv to rest of argv\n"
        "  tell application _app\n"
        f"    {body}\n"
        "  end tell\n"
        "end run\n"
    )
    return osascript(script, app, *args, timeout=timeout)


# An application name we are willing to embed in a script literal. Deliberately
# strict: letters, digits, spaces and the few punctuation marks real app names
# use ("Google Chrome", "Visual Studio Code", "IINA"). No quotes, backslashes or
# newlines, so the string literal cannot be terminated early.
_SAFE_APP_NAME = re.compile(r"^[A-Za-z0-9 ._+&()-]{1,64}$")


def tell_literal(app: str, body: str, *args: str, timeout: int = DEFAULT_TIMEOUT) -> Result:
    """Run ``body`` inside ``tell application "<app>"`` with a literal app name.

    Required whenever ``body`` uses application-specific terminology, because
    AppleScript resolves that at compile time and cannot do so for a variable.
    The name is validated against a strict character allow-list first; user data
    still travels through ``argv``.
    """
    if not _SAFE_APP_NAME.match(app):
        raise MacError(
            f"{app!r} is not a usable application name (letters, digits, spaces "
            "and . _ + & ( ) - only)"
        )
    script = (
        "on run argv\n"
        f'  tell application "{app}"\n'
        f"    {body}\n"
        "  end tell\n"
        "end run\n"
    )
    return osascript(script, *args, timeout=timeout)


# --------------------------------------------------------------------- paths

# Never readable or writable, whatever the config says.
HARD_FORBIDDEN = (
    Path.home() / ".ssh",
    Path.home() / ".aws",
    Path.home() / ".gnupg",
    Path.home() / ".config/gh",
    Path.home() / "Library/Keychains",
    Path("/etc/ssh"),
    Path("/etc/sudoers"),
    Path("/private/etc/master.passwd"),
    Path("/Library/Keychains"),
)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def expand(raw: str) -> Path:
    """Expand ~ and env vars, then make absolute without requiring existence."""
    text = os.path.expandvars(str(raw)).strip()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(config.load().get("agent.workdir", str(Path.home()))) / path
    # Resolve symlinks and .. so containment checks cannot be tricked. The path
    # need not exist; strict=False walks as far as it can.
    return path.resolve()


def check_path(raw: str) -> Path:
    """Expand ``raw`` and reject it if it lands in a protected location."""
    path = expand(raw)
    forbidden = list(HARD_FORBIDDEN)
    forbidden += [expand(p) for p in config.load().get("safety.forbidden_paths", [])]
    for blocked in forbidden:
        if path == blocked or _is_within(path, blocked):
            raise MacError(
                f"{path} is in a protected location ({blocked}) and Jeeves will not touch it."
            )
    return path


def trash(raw: str) -> str:
    """Move a file or folder to the Trash (recoverable)."""
    path = check_path(raw)
    if not path.exists():
        raise MacError(f"{path} does not exist")
    # `trash` is Finder terminology, so the app name must be a literal.
    result = tell_literal(
        "Finder",
        "move (POSIX file (item 1 of argv) as alias) to trash",
        str(path),
    )
    if not result.ok:
        raise MacError(f"could not move {path} to Trash: {result.err}")
    return f"Moved to Trash: {path}"


# ------------------------------------------------------------------ feedback


def notify(title: str, message: str, subtitle: str = "") -> None:
    """Post a Notification Centre banner. Best-effort; never raises."""
    script = (
        "on run argv\n"
        "  display notification (item 2 of argv) "
        "with title (item 1 of argv) subtitle (item 3 of argv)\n"
        "end run\n"
    )
    osascript(script, title, message, subtitle, timeout=10)


@lru_cache(maxsize=1)
def installed_voices() -> frozenset[str]:
    """Names of the speech voices `say` can use."""
    listing = run(["say", "-v", "?"], timeout=30).out
    return frozenset(
        line.split()[0] for line in listing.splitlines() if line.strip() and line[0].isalpha()
    )


@lru_cache(maxsize=8)
def resolve_voice(wanted: str) -> str:
    """Map a configured voice to one that exists; '' means system default."""
    if not wanted:
        return ""
    # Config may say "Ava (Premium)"; `say` wants the bare name.
    bare = wanted.split(" (")[0].strip()
    return bare if bare in installed_voices() else ""


def speak(text: str, voice: str = "", rate: int = 0, blocking: bool = True) -> None:
    """Speak text with the macOS speech synthesiser.

    An unavailable configured voice silently falls back to the system default, so
    a stale config never leaves Jeeves mute.
    """
    cfg = config.load()
    chosen = resolve_voice(voice or str(cfg.get("voice.tts_voice", "")))
    rate = rate or int(cfg.get("voice.tts_rate", 190))

    argv = ["say"]
    if chosen:
        argv += ["-v", chosen]
    argv += ["-r", str(rate), "--", text]

    if blocking:
        run(argv, timeout=300)
    else:
        subprocess.Popen(  # noqa: S603
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def stop_speaking() -> None:
    run(["killall", "say"], timeout=5)


def frontmost_app() -> str:
    result = osascript(
        'on run argv\n'
        '  tell application "System Events" to '
        "return name of first application process whose frontmost is true\n"
        "end run\n"
    )
    return result.out if result.ok else "unknown"


def quote_for_display(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)
