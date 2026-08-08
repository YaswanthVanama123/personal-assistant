"""System state and control: volume, power, network, focus, displays, processes."""

from __future__ import annotations

import re
import shutil

from ... import config
from ...mac import Result, notify, osascript, run
from ..registry import READ, RISKY, WRITE, ToolError, boolean, enum, integer, string, tool


def _ok(result: Result, what: str) -> str:
    if not result.ok:
        raise ToolError(f"could not {what}: {result.err or 'unknown error'}")
    return result.out


def _wifi_device() -> str:
    """Find the Wi-Fi interface (en0 is usual but not guaranteed)."""
    out = run(["networksetup", "-listallhardwareports"]).out
    current: str | None = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and current in {"Wi-Fi", "AirPort"}:
            return line.split(":", 1)[1].strip()
    return "en0"


# ------------------------------------------------------------------- volume


@tool(
    "volume_get",
    "Read the current system output volume (0-100) and mute state.",
    risk=READ,
)
def volume_get() -> str:
    result = osascript(
        "on run argv\n"
        "  set s to (get volume settings)\n"
        "  return (output volume of s as text) & \"|\" & (output muted of s as text)\n"
        "end run\n"
    )
    raw = _ok(result, "read the volume")
    level, muted = (raw.split("|") + ["false"])[:2]
    return f"Volume {level}% ({'muted' if muted.strip() == 'true' else 'unmuted'})"


@tool(
    "volume_set",
    "Set the system output volume.",
    {"level": integer("Volume percent, 0-100.", 0, 100)},
    required=["level"],
    risk=WRITE,
    undo="volume_set with the previous level",
)
def volume_set(level: int) -> str:
    result = osascript(
        "on run argv\nset volume output volume (item 1 of argv as integer)\nend run\n",
        str(level),
    )
    _ok(result, "set the volume")
    return f"Volume set to {level}%"


@tool(
    "volume_mute",
    "Mute or unmute system audio output.",
    {"muted": boolean("True to mute, false to unmute.")},
    required=["muted"],
    risk=WRITE,
    undo="volume_mute with the opposite value",
)
def volume_mute(muted: bool) -> str:
    result = osascript(
        "on run argv\nset volume output muted (item 1 of argv is \"1\")\nend run\n",
        "1" if muted else "0",
    )
    _ok(result, "change mute state")
    return "Audio muted" if muted else "Audio unmuted"


# -------------------------------------------------------------------- power


@tool("battery", "Battery percentage, charging state and time remaining.", risk=READ)
def battery() -> str:
    out = run(["pmset", "-g", "batt"]).out
    if not out:
        return "No battery information available (desktop Mac?)."
    percent = re.search(r"(\d+)%", out)
    remaining = re.search(r"(\d+:\d+)\s+remaining", out)
    source = "AC power" if "AC Power" in out else "battery"
    charging = "charging" if "; charging" in out else (
        "charged" if "; charged" in out else "discharging"
    )
    parts = [f"{percent.group(1)}%" if percent else "unknown charge", f"on {source}", charging]
    if remaining:
        parts.append(f"{remaining.group(1)} remaining")
    return "Battery: " + ", ".join(parts)


@tool(
    "sleep_display",
    "Turn the display off immediately. Locks the Mac if a password is required on wake.",
    risk=WRITE,
    undo="press any key",
)
def sleep_display() -> str:
    _ok(run(["pmset", "displaysleepnow"]), "sleep the display")
    return "Display asleep."


@tool(
    "caffeinate",
    "Prevent the Mac from sleeping for a number of minutes.",
    {"minutes": integer("How long to stay awake, 1-720.", 1, 720)},
    required=["minutes"],
    risk=WRITE,
    undo="killall caffeinate",
)
def caffeinate(minutes: int) -> str:
    import subprocess

    subprocess.Popen(  # noqa: S603 - fixed argv, intentionally detached
        ["caffeinate", "-dimsu", "-t", str(minutes * 60)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return f"Will stay awake for {minutes} minute(s). Run `killall caffeinate` to stop early."


# ------------------------------------------------------------------ network


@tool("wifi_status", "Current Wi-Fi power state, network name and IP address.", risk=READ)
def wifi_status() -> str:
    device = _wifi_device()
    power = run(["networksetup", "-getairportpower", device]).out
    on = power.strip().endswith("On")
    if not on:
        return f"Wi-Fi ({device}) is off."
    network = run(["networksetup", "-getairportnetwork", device]).out
    name = network.split(": ", 1)[1] if ": " in network else "unknown network"
    ip = run(["ipconfig", "getifaddr", device]).out or "no IP address"
    return f"Wi-Fi ({device}) on — {name}, IP {ip}"


@tool(
    "wifi_power",
    "Turn Wi-Fi on or off.",
    {"on": boolean("True to enable Wi-Fi, false to disable.")},
    required=["on"],
    risk=WRITE,
    undo="wifi_power with the opposite value",
)
def wifi_power(on: bool) -> str:
    device = _wifi_device()
    _ok(
        run(["networksetup", "-setairportpower", device, "on" if on else "off"]),
        "change Wi-Fi power",
    )
    return f"Wi-Fi {'enabled' if on else 'disabled'} on {device}."


@tool("network_info", "Interface, local IP, default gateway and DNS servers.", risk=READ)
def network_info() -> str:
    device = _wifi_device()

    gateway = "unknown"
    for line in run(["route", "-n", "get", "default"]).out.splitlines():
        if "gateway:" in line:
            gateway = line.split("gateway:", 1)[1].strip()
            break

    dns = re.findall(r"nameserver\[\d+\] : (\S+)", run(["scutil", "--dns"]).out)
    # scutil repeats resolvers per search domain; keep first-seen order, drop dupes.
    unique_dns = list(dict.fromkeys(dns))[:3]

    return "\n".join(
        [
            f"Interface: {device}",
            f"Local IP:  {run(['ipconfig', 'getifaddr', device]).out or 'none'}",
            f"Gateway:   {gateway}",
            f"DNS:       {', '.join(unique_dns) if unique_dns else 'unknown'}",
        ]
    )


# -------------------------------------------------------------- focus / misc


@tool(
    "focus_mode",
    "Toggle a Focus mode such as Do Not Disturb by running a macOS Shortcut. "
    "Requires a Shortcut of the given name to exist in the Shortcuts app.",
    {"shortcut": string("Name of the Shortcut to run, e.g. 'Toggle Do Not Disturb'.")},
    required=["shortcut"],
    risk=WRITE,
    undo="run the same Shortcut again",
)
def focus_mode(shortcut: str) -> str:
    if not shutil.which("shortcuts"):
        raise ToolError("the `shortcuts` CLI is unavailable on this Mac")
    result = run(["shortcuts", "run", shortcut], timeout=45)
    if not result.ok:
        available = run(["shortcuts", "list"]).out.splitlines()[:15]
        hint = f" Available shortcuts: {', '.join(available)}" if available else (
            " No shortcuts are defined yet. Create one in the Shortcuts app "
            "(e.g. a 'Set Focus' action named 'Toggle Do Not Disturb')."
        )
        raise ToolError(f"shortcut {shortcut!r} failed.{hint}")
    return f"Ran Shortcut {shortcut!r}."


@tool("list_shortcuts", "List macOS Shortcuts available to run.", risk=READ)
def list_shortcuts() -> str:
    if not shutil.which("shortcuts"):
        raise ToolError("the `shortcuts` CLI is unavailable on this Mac")
    names = run(["shortcuts", "list"]).out.splitlines()
    return "\n".join(f"- {n}" for n in names) if names else "No Shortcuts defined."


@tool(
    "brightness_set",
    "Set the built-in display brightness.",
    {"percent": integer("Brightness percent, 0-100.", 0, 100)},
    required=["percent"],
    risk=WRITE,
    undo="brightness_set with the previous value",
)
def brightness_set(percent: int) -> str:
    if not config.NATIVE_BIN.exists():
        raise ToolError(
            "the native helper is not built. Run scripts/build_native.sh first."
        )
    result = run([str(config.NATIVE_BIN), "brightness", str(percent / 100)])
    if not result.ok:
        raise ToolError(f"could not set brightness: {result.err or result.out}")
    return f"Brightness set to {percent}%."


@tool("system_info", "Model, macOS version, chip, memory, uptime and disk free space.", risk=READ)
def system_info() -> str:
    def sysctl(key: str) -> str:
        return run(["sysctl", "-n", key]).out or "unknown"

    disk = run(["df", "-h", "/"]).out.splitlines()
    disk_line = disk[1].split() if len(disk) > 1 else []
    mem_bytes = sysctl("hw.memsize")
    try:
        mem = f"{int(mem_bytes) // (1024 ** 3)} GB"
    except ValueError:
        mem = mem_bytes
    return "\n".join(
        [
            f"Model:   {sysctl('hw.model')}",
            f"Chip:    {sysctl('machdep.cpu.brand_string')} ({sysctl('hw.ncpu')} cores)",
            f"Memory:  {mem}",
            f"macOS:   {run(['sw_vers', '-productVersion']).out} "
            f"(build {run(['sw_vers', '-buildVersion']).out})",
            f"Uptime:  {run(['uptime']).out}",
            f"Disk /:  {disk_line[3]} free of {disk_line[1]}" if len(disk_line) > 3 else "",
        ]
    ).strip()


@tool(
    "top_processes",
    "Processes using the most CPU or memory right now.",
    {
        "by": enum("Sort key.", ["cpu", "memory"]),
        "count": integer("How many to list, 1-25.", 1, 25),
    },
    risk=READ,
)
def top_processes(by: str = "cpu", count: int = 8) -> str:
    out = run(["ps", "-Ao", "comm,%cpu,%mem", "-r" if by == "cpu" else "-m"], timeout=20)
    if not out.ok:
        # Some managed Macs deny `ps` and `top` outright. Say so plainly rather
        # than surfacing a bare errno.
        return (
            "Per-process CPU and memory figures are unavailable on this Mac — "
            f"the process table is restricted by policy ({out.err or 'permission denied'}). "
            "Use list_running_apps for what is open, or system_info for overall load."
        )
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ToolError("the process list came back empty")
    kept = []
    for row in lines[1:]:
        parts = row.rsplit(None, 2)
        if len(parts) == 3:
            name = parts[0].rsplit("/", 1)[-1]
            kept.append(f"{name[:44]:<44} cpu {parts[1]:>6}  mem {parts[2]:>5}")
        if len(kept) >= count:
            break
    return f"Top {len(kept)} processes by {by}:\n" + "\n".join(kept)


@tool(
    "notify",
    "Post a macOS notification banner.",
    {
        "title": string("Banner title."),
        "message": string("Banner body text."),
        "subtitle": string("Optional subtitle."),
    },
    required=["title", "message"],
    risk=WRITE,
)
def notify_tool(title: str, message: str, subtitle: str = "") -> str:
    notify(title, message, subtitle)
    return f"Notification posted: {title} — {message}"


@tool(
    "shutdown_or_restart",
    "Log out, restart, shut down or sleep the Mac. Always requires confirmation.",
    {"action": enum("What to do.", ["logout", "restart", "shutdown", "sleep"])},
    required=["action"],
    risk=RISKY,
    needs_automation=True,
    preview=lambda action: (
        f"  {action} the Mac now — unsaved work in other applications may be lost"
    ),
)
def shutdown_or_restart(action: str) -> str:
    result = osascript(
        "on run argv\n"
        "  set a to item 1 of argv\n"
        '  tell application "System Events"\n'
        '    if a is "logout" then log out\n'
        '    if a is "restart" then restart\n'
        '    if a is "shutdown" then shut down\n'
        '    if a is "sleep" then sleep\n'
        "  end tell\n"
        "end run\n",
        action,
        timeout=15,
    )
    if not result.ok:
        raise ToolError(
            f"could not {action}: {result.err}. This needs Automation permission "
            "for System Events — run `jeeves doctor`."
        )
    return f"Asked macOS to {action}."
