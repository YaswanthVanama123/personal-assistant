"""A gated shell, and the tools Jeeves uses to remember things between sessions."""

from __future__ import annotations

import re
import shlex

from ... import memory as mem
from ...mac import run
from ..registry import (
    READ,
    RISKY,
    ToolError,
    array,
    boolean,
    integer,
    string,
    tool,
)

# Binaries that only ever report state. Anything capable of installing,
# building, writing files or executing arbitrary code is deliberately absent —
# it still runs, it just needs the user's confirmation first. Note the
# distinction: this list is about "can this change my Mac", not "do I trust it".
READ_ONLY = {
    "arch", "basename", "cal", "cat", "column", "date", "df", "dirname", "du",
    "echo", "env", "file", "find", "grep", "head", "hostname", "id", "ifconfig",
    "jq", "less", "md5", "mdfind", "nproc", "nslookup", "pgrep", "printenv",
    "pwd", "shasum", "sort", "stat", "sw_vers", "sysctl", "system_profiler",
    "tail", "tr", "type", "uname", "uniq", "uptime", "vm_stat", "wc", "which",
    "who", "whoami", "awk", "cut", "diff", "tree", "lsappinfo", "ioreg",
    "networksetup", "pmset", "ps", "top", "scutil", "ipconfig", "route",
    "codesign", "otool", "lipo", "plutil", "defaults", "git", "sed", "ls",
    "man", "ping", "host", "dig", "traceroute", "last", "groups", "locale",
}

# Subcommands that make an otherwise-read-only binary mutating, so `git status`
# runs immediately while `git push` asks first.
MUTATING_SUBCOMMANDS = {
    "git": {
        "push", "reset", "clean", "rebase", "commit", "merge", "checkout",
        "switch", "restore", "cherry-pick", "revert", "am", "apply", "stash",
        "filter-branch", "gc", "prune", "remote", "config", "tag", "branch",
        "submodule", "worktree", "init", "clone", "fetch", "pull", "mv", "rm",
    },
    "defaults": {"write", "delete", "import", "rename"},
    "plutil": {"-replace", "-insert", "-remove", "-convert"},
}

# Flags that turn a reporting tool into a mutating one.
MUTATING_FLAGS = {
    "-i", "--in-place", "-delete", "-exec", "-execdir", "-ok",
    "-o", "--output", "-w", "--write", "-setairportpower", "-setairportnetwork",
}

# Never run, whatever the user or the model says.
FORBIDDEN_BINARIES = {
    "rm", "rmdir", "dd", "mkfs", "newfs", "diskutil", "fdisk", "shutdown",
    "reboot", "halt", "sudo", "su", "doas", "chown", "chmod", "launchctl",
    "csrutil", "spctl", "nvram", "kextload", "security", "systemsetup",
    "networkQuality", "tmutil", "asr", "pkgutil", "installer", "softwareupdate",
    "killall", "pkill", "kill", "purge", "mount", "umount", "chflags",
}

# Substrings that indicate an attempt to escape the allow list or exfiltrate.
FORBIDDEN_PATTERNS = (
    (r"\brm\s+-[rRf]", "recursive or forced deletion"),
    (r">\s*/dev/(disk|rdisk)", "writing to a raw disk device"),
    (r"\bcurl\b[^|;]*\|\s*(ba)?sh", "piping a download straight into a shell"),
    (r"\bwget\b[^|;]*\|\s*(ba)?sh", "piping a download straight into a shell"),
    # `eval`/`exec` as a command, not `find -exec` or a filename containing them.
    (r"(?<![-\w])(eval|exec)\s", "eval/exec indirection"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"/etc/(passwd|shadow|sudoers)", "reading system credential files"),
    (r"\.ssh/id_|\.aws/credentials|\.gnupg", "reading private keys or credentials"),
    (r"\bhistory\b.*\|", "harvesting shell history"),
    (r"\bbase64\b.*\|\s*(ba)?sh", "obfuscated execution"),
    (r"\bnc\b\s+-l", "opening a listening socket"),
    (r"\bchmod\s+[0-7]*777", "world-writable permissions"),
)


def classify(command: str) -> tuple[str, str]:
    """Return (verdict, reason) where verdict is 'deny', 'ask' or 'allow'.

    'allow' is claimed only when every stage of the pipeline is a known
    reporting command with no mutating flag and no output redirection. Anything
    unrecognised falls to 'ask' — the safe default — and the hard deny list wins
    over everything, including an explicit user confirmation.
    """
    text = command.strip()
    if not text:
        return "deny", "empty command"

    for pattern, why in FORBIDDEN_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return "deny", why

    # Writing to a file needs confirmation whatever the binary is. `>` and `>>`
    # (optionally with a leading fd, as in `2>log`) write to files; `>&` and
    # `2>&1` only re-point existing descriptors, so they are not writes.
    if re.search(r"&>", text) or re.search(r"(?<!&)>{1,2}(?!&)", text):
        return "ask", "redirects output to a file"

    if "$(" in text or "`" in text:
        return "ask", "contains command substitution"

    # Mask descriptor redirections before splitting: an unmasked `2>&1` would be
    # torn in half by the `&` separator and its `1` mistaken for a command.
    masked = re.sub(r"\d?>&\d?|&>", " ", text)
    stages = re.split(r"\|\||&&|[|;&]", masked)
    for stage in stages:
        stage = stage.strip()
        if not stage:
            continue
        try:
            parts = shlex.split(stage)
        except ValueError as exc:
            return "deny", f"unparseable shell syntax: {exc}"
        if not parts:
            continue

        # Skip leading VAR=value assignments.
        index = 0
        while index < len(parts) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[index]):
            index += 1
        if index >= len(parts):
            continue

        binary = parts[index].rsplit("/", 1)[-1]
        arguments = parts[index + 1:]

        if binary in FORBIDDEN_BINARIES:
            return "deny", f"{binary} is on the hard deny list"
        if binary not in READ_ONLY:
            return "ask", f"{binary} is not a known read-only command"

        mutating = MUTATING_SUBCOMMANDS.get(binary, set())
        first_argument = next((a for a in arguments if not a.startswith("-")), "")
        if first_argument and first_argument in mutating:
            return "ask", f"{binary} {first_argument} changes state"
        for argument in arguments:
            if argument in mutating or argument in MUTATING_FLAGS:
                return "ask", f"{binary} {argument} changes state"
            if argument.startswith(("-set", "--set")):
                return "ask", f"{binary} {argument} changes state"

    return "allow", "read-only"


def _preview_shell(command: str, workdir: str = "", timeout: int = 60) -> str:
    _verdict, reason = classify(command)
    return (
        f"  run a shell command ({reason})\n"
        f"  $ {command}\n"
        f"  in: {workdir or 'the working directory'}"
    )


def _shell_risk(command: str = "", **_: object) -> str:
    """Read-only commands run immediately; everything else asks first."""
    return READ if classify(command)[0] == "allow" else RISKY


@tool(
    "shell",
    "Run a shell command and return its output. Read-only commands (ls, git "
    "status, grep, df…) run immediately; anything that could change state needs "
    "the user's confirmation. Destructive commands are refused outright.",
    {
        "command": string("The command line to run, e.g. 'git status --short'."),
        "workdir": string("Optional directory to run in."),
        "timeout": integer("Seconds before giving up, 1-600.", 1, 600),
    },
    required=["command"],
    risk=RISKY,
    preview=_preview_shell,
    risk_fn=_shell_risk,
)
def shell(command: str, workdir: str = "", timeout: int = 60, **_: object) -> str:
    from ...mac import check_path

    verdict, reason = classify(command)
    if verdict == "deny":
        raise ToolError(
            f"refused: {reason}. This command is on Jeeves' hard deny list and "
            "cannot be run even with confirmation."
        )

    cwd = str(check_path(workdir)) if workdir else None
    result = run(["/bin/zsh", "-lc", command], timeout=timeout, cwd=cwd)
    body = result.out or ""
    if result.err:
        body += ("\n" if body else "") + f"[stderr]\n{result.err}"
    if len(body) > 40_000:
        body = body[:40_000] + "\n… output truncated at 40 000 characters"
    status = "ok" if result.ok else f"exit {result.code}"
    return f"$ {command}\n[{status}]\n{body or '(no output)'}"


@tool(
    "shell_check",
    "Ask whether a shell command would be allowed, without running it. Use this "
    "when you are unsure before proposing a command to the user.",
    {"command": string("The command line to classify.")},
    required=["command"],
    risk=READ,
)
def shell_check(command: str) -> str:
    verdict, reason = classify(command)
    explain = {
        "allow": "runs immediately without confirmation",
        "ask": "needs the user's confirmation first",
        "deny": "is refused and cannot be run at all",
    }[verdict]
    return f"`{command}` {explain} ({reason})."


# -------------------------------------------------------------------- memory


@tool(
    "remember",
    "Store something durable about the user or their setup, so future sessions "
    "know it. Use kind='profile' for lasting facts about the person (their "
    "preferences, their people, their projects) — those are loaded into every "
    "future session automatically.",
    {
        "text": string("The fact to remember, written as a standalone sentence."),
        "kind": string("Category: 'profile' for lasting user facts, else 'note'."),
        "tags": array("Optional tags for retrieval."),
    },
    required=["text"],
    risk=READ,  # writing to Jeeves' own notebook is not a system change
)
def remember(text: str, kind: str = "note", tags: list[str] | None = None) -> str:
    fact = mem.remember(text, kind=kind, tags=tags or [])
    scope = "loaded into every future session" if kind == "profile" else "searchable with recall"
    return f"Remembered as #{fact.id} ({kind}, {scope}): {fact.text}"


@tool(
    "recall",
    "Search everything Jeeves has been asked to remember.",
    {
        "query": string("Words to search for. Omit to list the most recent."),
        "kind": string("Optional category filter, e.g. 'profile'."),
        "limit": integer("Maximum results, 1-50.", 1, 50),
    },
    risk=READ,
)
def recall(query: str = "", kind: str = "", limit: int = 12) -> str:
    facts = mem.recall(query, limit=limit, kind=kind)
    if not facts:
        return "Nothing remembered matches that." if query else "Nothing remembered yet."
    return "\n".join(f.render() for f in facts)


@tool(
    "forget",
    "Delete a remembered fact by its id (shown by recall).",
    {"id": integer("Fact id to delete.", 1)},
    required=["id"],
    risk=READ,
)
def forget(id: int) -> str:  # noqa: A002
    return f"Forgot fact #{id}." if mem.forget(id) else f"No fact with id {id}."


@tool(
    "audit_trail",
    "Show what Jeeves has actually done recently — every tool call that changed "
    "anything, with how to undo it. Use this when the user asks what you did.",
    {
        "limit": integer("How many entries, 1-100.", 1, 100),
        "changes_only": boolean("Hide read-only lookups.", default=True),
    },
    risk=READ,
)
def audit_trail(limit: int = 20, changes_only: bool = True) -> str:
    import time as _time

    entries = mem.recent_audit(limit * 3 if changes_only else limit)
    lines: list[str] = []
    for entry in entries:
        if changes_only and entry["outcome"] not in {"ok", "awaiting-confirmation"}:
            if entry["outcome"] not in {"error", "denied", "crash"}:
                continue
        when = _time.strftime("%H:%M:%S", _time.localtime(entry["ts"]))
        undo = f"  (undo: {entry['undo']})" if entry["undo"] else ""
        detail = (entry["detail"] or "").splitlines()
        summary = detail[0][:110] if detail else ""
        lines.append(f"{when}  {entry['tool']:<22} {entry['outcome']:<22} {summary}{undo}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) if lines else "Nothing in the audit log yet."
