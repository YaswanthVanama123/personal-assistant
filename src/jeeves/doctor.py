"""`jeeves doctor` — check every moving part and say exactly how to fix it.

macOS privacy permissions are the usual reason a tool silently does nothing, so
each check reports the precise System Settings pane to open.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import subprocess
import sys
import time
from pathlib import Path

from . import config, mac

OK = "\033[38;5;114m✓\033[0m"
WARN = "\033[38;5;179m!\033[0m"
BAD = "\033[38;5;203m✗\033[0m"
DIM = "\033[2m"
RESET = "\033[0m"

PRIVACY = "System Settings → Privacy & Security"


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, label: str, detail: str = "") -> None:
        print(f" {OK} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))

    def warn(self, label: str, fix: str) -> None:
        self.warnings += 1
        print(f" {WARN} {label}")
        print(f"     {DIM}{fix}{RESET}")

    def bad(self, label: str, fix: str) -> None:
        self.failures += 1
        print(f" {BAD} {label}")
        print(f"     {DIM}{fix}{RESET}")


def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def check_runtime(r: Report) -> None:
    _section("Agent runtime")
    binary = shutil.which("claude")
    if not binary:
        r.bad(
            "the `claude` command was not found",
            "Jeeves uses Claude Code as its agent runtime. Install it, then re-run.",
        )
        return
    version = mac.run([binary, "--version"], timeout=30).out.splitlines()
    r.ok("claude found", f"{binary} — {version[0] if version else 'unknown version'}")

    # Does it actually start? This is the check that catches auth problems,
    # duplicate installs, nested sessions and unpermitted tools.
    from . import agent

    timeout = 150
    print(
        f"     {DIM}running a test turn (up to {timeout}s — the first one is "
        f"slower, it starts the tool server)…{RESET}"
    )

    started = time.monotonic()
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(5):
            print(
                f"     {DIM}…still waiting ({time.monotonic() - started:.0f}s){RESET}",
                flush=True,
            )

    heartbeat = threading.Thread(target=tick, daemon=True)
    heartbeat.start()

    saw: list[str] = []
    try:
        probe = agent.Agent(interface="doctor", turn_timeout=timeout)
        turn = probe.ask(
            "Reply with exactly: READY",
            on_event=lambda e: saw.append(e.kind),
        )
        probe.close()
    except agent.AgentError as exc:
        stop.set()
        r.bad("the runtime could not start", str(exc))
        return
    finally:
        stop.set()

    if turn.ok and "READY" in turn.reply.upper():
        r.ok("runtime answers", f"{turn.duration_s:.1f}s, ${turn.cost_usd:.4f}")
    elif turn.ok:
        r.warn("runtime answered unexpectedly", f"got {turn.reply[:120]!r}")
    else:
        hint = ""
        if "did not finish" in turn.error and "text" not in saw:
            hint = (
                "\n     The runtime produced no output at all. Test it on its own "
                "with:  claude -p 'Reply OK'\n     If that also hangs, the runtime "
                "itself needs attention (usually sign-in) rather than Jeeves."
            )
        r.bad("the runtime returned an error", turn.error + hint)


def check_tools(r: Report) -> None:
    _section("Tool server")
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "doctor", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "jeeves.mcp.server"],
            input="".join(json.dumps(f) + "\n" for f in frames),
            capture_output=True,
            text=True,
            timeout=60,
            env={**_child_env()},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        r.bad("the MCP tool server did not start", str(exc))
        return

    count = 0
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and "tools" in (msg.get("result") or {}):
            count = len(msg["result"]["tools"])
    if count:
        from . import policy
        from .mcp import registry
        from .mcp import tools as _t  # noqa: F401

        tiers = registry.by_risk()
        r.ok(
            f"{count} tools registered",
            f"{len(tiers['read'])} read · {len(tiers['write'])} reversible · "
            f"{len(tiers['risky'])} gated",
        )
        del policy
    else:
        r.bad("the tool server returned no tools", proc.stderr.strip()[-300:] or "no output")


def _child_env() -> dict[str, str]:
    import os

    return {
        "PYTHONPATH": str(config.SRC_ROOT),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "JEEVES_STATE_DIR": str(config.STATE_DIR),
    }


def check_native(r: Report) -> None:
    _section("Native helper (voice, OCR, calendar, reminders, contacts)")
    if not config.NATIVE_BIN.exists():
        r.bad(
            "jeeves-native is not built",
            "Run: bash scripts/build_native.sh",
        )
        return
    r.ok("jeeves-native built", str(config.NATIVE_BIN))

    signed = mac.run(["codesign", "-dv", str(config.NATIVE_BIN)], timeout=20)
    if "adhoc" in signed.err or "Signature" in signed.err:
        r.ok("code signature present", "permission grants will persist across rebuilds")
    else:
        r.warn(
            "no code signature",
            "macOS may re-prompt for permissions on every rebuild. "
            "Re-run scripts/build_native.sh.",
        )

    # The helper must run from inside Jeeves.app, or TCC cannot read the usage
    # strings and Speech Recognition kills the process instead of prompting.
    # A symlink to the binary breaks this; the build script writes an exec shim.
    state = mac.run([str(config.NATIVE_BIN), "audio-check"], timeout=45)
    try:
        inside = json.loads(state.out).get("inside_app_bundle") if state.ok else None
    except json.JSONDecodeError:
        inside = None
    if inside is True:
        r.ok("running inside Jeeves.app", "privacy prompts will be attributed correctly")
    elif inside is False:
        r.bad(
            "the helper is running outside its app bundle",
            "Voice will crash: TCC cannot read the usage strings from a bare "
            "executable. Re-run scripts/build_native.sh, and invoke "
            f"{config.NATIVE_BIN} rather than a symlink to it.",
        )

    # OCR is testable without any privacy permission.
    probe = config.CACHE_DIR / "doctor-ocr.png"
    config.ensure_dirs()
    made = _make_probe_image(probe)
    if made:
        out = mac.run([str(config.NATIVE_BIN), "ocr", str(probe)], timeout=60)
        if out.ok and "JEEVES" in out.out.upper():
            r.ok("on-device OCR works", f"read {out.out.strip()[:40]!r}")
        else:
            r.warn("OCR returned nothing", f"expected to read a test image ({out.err[:120]})")


def _make_probe_image(target: Path) -> bool:
    """Render a tiny text PNG using only system tools."""
    src = target.with_suffix(".txt")
    try:
        src.write_text("JEEVES OCR PROBE 12345\n")
    except OSError:
        return False
    pdf = target.with_suffix(".pdf")
    if not mac.run(["sh", "-c", f"cupsfilter {src!s} > {pdf!s} 2>/dev/null"], timeout=60).ok:
        return False
    return mac.run(
        ["sips", "-s", "format", "png", "-Z", "1400", str(pdf), "--out", str(target)],
        timeout=60,
    ).ok and target.exists()


def check_permissions(r: Report) -> None:
    _section("macOS permissions")

    # Automation (Apple Events) — needed for Notes, Mail, Messages, Music, Finder.
    # Uses Finder terminology, so this must go through tell_literal.
    probe = mac.tell_literal("Finder", "return name of home", timeout=20)
    if probe.ok:
        r.ok("Automation", "Notes, Mail, Messages, Music and Finder are reachable")
    else:
        detail = probe.err.strip()[:90]
        r.warn(
            "Automation is not granted",
            f"Notes, Mail, Messages, Music and Trash will fail ({detail}). "
            f"Run a command that uses them once and approve the prompt, or enable "
            f"your terminal under {PRIVACY} → Automation.",
        )

    # Screen Recording — needed for screenshots and reading the screen.
    shot = config.CACHE_DIR / "doctor-shot.png"
    config.ensure_dirs()
    mac.run(["screencapture", "-x", str(shot)], timeout=40)
    if shot.exists() and shot.stat().st_size > 1000:
        r.ok("Screen Recording", "screenshot and screen_text will work")
        shot.unlink(missing_ok=True)
    else:
        r.warn(
            "Screen Recording is not granted",
            f"screenshot and screen_text will fail. Enable your terminal under "
            f"{PRIVACY} → Screen Recording, then restart the terminal.",
        )

    if not config.NATIVE_BIN.exists():
        r.warn(
            "Calendars, Reminders, Contacts, Accessibility and Microphone not checked",
            "the native helper is not built yet",
        )
        return

    # Accessibility — reading and driving apps with no AppleScript API (WhatsApp,
    # Slack, Electron/Catalyst apps) and typing into the frontmost window.
    ax = mac.run([str(config.NATIVE_BIN), "ui-dump", "Finder", "--max", "3"], timeout=30)
    if ax.code == 77:
        r.warn(
            "Accessibility is not granted",
            "WhatsApp, ui_read_app, ui_inspect and typing into apps will fail. "
            f"Enable jeeves-native under {PRIVACY} → Accessibility, then restart "
            "your terminal.",
        )
    elif ax.ok or ax.code == 4:
        r.ok("Accessibility", "WhatsApp and other UI-driven apps are readable")
    else:
        r.warn("Accessibility check inconclusive", ax.err[:140] or "no output")

    for label, args, pane in (
        ("Calendars", ["events", "1"], "Calendars"),
        ("Reminders", ["reminders"], "Reminders"),
        ("Contacts", ["contacts", "zzz-nobody"], "Contacts"),
    ):
        result = mac.run([str(config.NATIVE_BIN), *args], timeout=45)
        if result.code == 77:
            r.warn(
                f"{label} is not granted",
                f"Enable jeeves-native (or your terminal) under {PRIVACY} → {pane}.",
            )
        elif result.ok:
            r.ok(label, "readable")
        else:
            r.warn(f"{label} check failed", result.err[:140] or "unknown error")

    # Voice: check everything except actually speaking.
    import json as _json

    audio = mac.run([str(config.NATIVE_BIN), "audio-check"], timeout=45)
    try:
        state = _json.loads(audio.out) if audio.ok else {}
    except _json.JSONDecodeError:
        state = {}

    if not state:
        r.warn("voice input not checked", audio.err[:140] or "audio-check produced no output")
        return

    for key, label, fix in (
        ("microphone_permission", "Microphone",
         f"Grant Microphone to jeeves-native under {PRIVACY} → Microphone."),
        ("speech_permission", "Speech Recognition",
         f"Grant Speech Recognition under {PRIVACY} → Speech Recognition."),
    ):
        if state.get(key) == "authorized":
            r.ok(label, "granted")
        else:
            r.warn(f"{label} is {state.get(key, 'unknown')}", fix)

    if state.get("dictation_enabled"):
        r.ok("Dictation", "on-device speech recognition can run")
    else:
        r.warn(
            "Dictation is off — voice modes will not work",
            "On-device speech recognition needs it. Turn it on: System Settings → "
            "Keyboard → Dictation. Siri itself is not required.",
        )

    if state.get("format_usable"):
        r.ok(
            "Audio input",
            f"{state.get('input_device')} at {state.get('hardware_sample_rate')} Hz",
        )
    else:
        r.warn(
            "no usable audio input",
            f"device: {state.get('input_device')}. Check System Settings → Sound → Input.",
        )


def check_storage(r: Report) -> None:
    _section("Storage")
    try:
        config.ensure_dirs()
        (config.STATE_DIR / ".writeprobe").write_text("ok")
        (config.STATE_DIR / ".writeprobe").unlink()
        r.ok("state directory writable", str(config.STATE_DIR))
    except OSError as exc:
        r.bad("cannot write to the state directory", f"{config.STATE_DIR}: {exc}")
        return

    from . import memory

    try:
        memory.connect().close()
        fts = "with FTS5 full-text search" if memory.has_fts() else "LIKE search only (no FTS5)"
        counts = memory.connect()
        facts = counts.execute("SELECT count(*) FROM facts").fetchone()[0]
        audits = counts.execute("SELECT count(*) FROM audit").fetchone()[0]
        counts.close()
        r.ok(f"database ready ({fts})", f"{facts} remembered fact(s), {audits} audit entr(ies)")
    except sqlite3.Error as exc:
        r.bad("the database is unusable", str(exc))


def check_voice(r: Report) -> None:
    _section("Speech output")
    wanted = str(config.load().get("voice.tts_voice", ""))
    available = mac.installed_voices()
    if not available:
        r.bad("no speech voices found", "`say -v '?'` returned nothing")
        return

    if not wanted:
        r.ok(
            "using the macOS system voice",
            f"{len(available)} voices installed; set voice.tts_voice to pick one",
        )
        return

    resolved = mac.resolve_voice(wanted)
    if resolved:
        r.ok(f"voice “{resolved}” available", f"{len(available)} voices installed")
    else:
        suggestion = ", ".join(sorted(available)[:6])
        r.warn(
            f"configured voice “{wanted}” is not installed",
            f"Falling back to the system default. Installed include: {suggestion}… "
            "Add higher-quality voices under System Settings → Accessibility → "
            "Spoken Content → System Voice → Manage Voices, then set "
            f"voice.tts_voice in {config.CONFIG_DIR / 'jeeves.toml'}.",
        )


def run(skip_runtime: bool = False) -> int:
    print("\033[1mJeeves — system check\033[0m")
    print(f"{DIM}{time.strftime('%Y-%m-%d %H:%M')} · python {sys.version.split()[0]}{RESET}")

    r = Report()
    check_storage(r)
    check_tools(r)
    check_native(r)
    check_permissions(r)
    check_voice(r)
    if skip_runtime:
        _section("Agent runtime")
        print(f" {DIM}skipped (--skip-runtime){RESET}")
    else:
        check_runtime(r)

    _section("Summary")
    if r.failures:
        print(f" {BAD} {r.failures} problem(s) must be fixed before Jeeves will work.")
    elif r.warnings:
        print(f" {WARN} {r.warnings} optional capabilit(ies) unavailable; the rest works.")
    else:
        print(f" {OK} Everything checks out. Try: jeeves chat")
    print()
    return 1 if r.failures else 0
