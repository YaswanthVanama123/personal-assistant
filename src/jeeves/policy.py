"""The permission policy handed to the agent runtime.

Two layers protect the Mac, and they are deliberately different in kind:

  1. This module — the runtime's own allow/deny rules. It decides which tools
     exist at all and hard-denies reads of credential stores. Enforced by the
     runtime, outside the model's reach.

  2. The confirm gate in mcp/registry.py — decides which actions need the user
     to say yes. Mediated by the model, so it is a usability feature.

Anything genuinely dangerous is handled here or refused outright inside the tool,
never left to the gate alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config

# Paths no tool may read, whatever else is allowed. These are runtime-enforced
# and complement mac.HARD_FORBIDDEN, which guards Jeeves' own file tools.
SECRET_GLOBS = [
    "~/.ssh/**",
    "~/.aws/**",
    "~/.gnupg/**",
    "~/.config/gh/**",
    "~/Library/Keychains/**",
    "~/.netrc",
    "**/id_rsa",
    "**/id_ed25519",
    "**/*.pem",
    "**/*.p12",
    "/etc/sudoers",
    "/etc/ssh/**",
]


def allowed_tools() -> list[str]:
    """Tools the runtime may use without prompting.

    Headless runs cannot show a permission dialog, so anything not listed here
    fails closed. Jeeves' own tools carry their own confirm gate, which is why
    the whole `jeeves` MCP server is allowed at this layer.
    """
    return [
        "mcp__jeeves",
        "Read",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "TodoWrite",
    ]


def settings_json() -> str:
    """A settings document for --settings."""
    deny = [f"Read({glob})" for glob in SECRET_GLOBS]
    deny += [f"Glob({glob})" for glob in SECRET_GLOBS]
    deny += [f"Grep({glob})" for glob in SECRET_GLOBS]
    # Belt and braces: the Bash tool is not enabled at all, but if a future
    # change enables it, these still apply.
    deny += [
        "Bash(sudo:*)",
        "Bash(rm:*)",
        "Bash(dd:*)",
        "Bash(diskutil:*)",
        "Bash(security:*)",
        "Bash(launchctl:*)",
        "Bash(csrutil:*)",
    ]
    deny += [f"Read({p})" for p in config.load().get("safety.forbidden_paths", [])]

    return json.dumps(
        {
            "permissions": {
                # MCP rules are `mcp__server` for a whole server, or
                # `mcp__server__tool` for one tool. The `Tool(argument)` matcher
                # syntax does NOT apply to them — an invalid rule makes the whole
                # settings document fail validation, and in headless mode that is
                # silently ignored, which would leave every tool call unpermitted.
                "allow": allowed_tools(),
                "deny": deny,
            },
            "includeCoAuthoredBy": False,
        }
    )


def describe() -> str:
    """Human-readable summary for `jeeves doctor` and the README."""
    from .mcp import registry
    from .mcp import tools as _tools  # noqa: F401  (registers tools)

    tiers = registry.by_risk()
    mode = config.load().get("safety.mode", "guarded")
    behaviour = {
        "guarded": "reads freely; safe reversible changes run immediately; "
        "outbound and destructive actions ask first",
        "strict": "every change asks first",
        "open": "nothing asks; hard denies still apply",
    }[mode]

    lines = [
        f"Safety mode: {mode} — {behaviour}",
        "",
        f"Read-only tools ({len(tiers['read'])}): run immediately",
        "  " + ", ".join(tiers["read"]),
        "",
        f"Reversible-change tools ({len(tiers['write'])}): "
        + ("run immediately" if mode == "guarded" else "ask first"),
        "  " + ", ".join(tiers["write"]),
        "",
        f"Gated tools ({len(tiers['risky'])}): always ask first"
        + (" (except in open mode)" if mode == "open" else ""),
        "  " + ", ".join(tiers["risky"]),
        "",
        "Never permitted, even with confirmation:",
        "  reading " + ", ".join(SECRET_GLOBS[:6]) + ", …",
        "  rm -rf, sudo, dd, diskutil, mkfs, launchctl, csrutil, security, "
        "piping downloads into a shell, fork bombs",
        "",
        f"Audit log: {config.AUDIT_LOG}",
        f"Runtime log: {config.AGENT_LOG}",
    ]
    return "\n".join(lines)


def write_user_settings() -> Path:
    """Persist the generated settings so it can be inspected."""
    config.ensure_dirs()
    target = config.STATE_DIR / "runtime-settings.json"
    target.write_text(json.dumps(json.loads(settings_json()), indent=2) + "\n")
    return target
