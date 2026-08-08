"""The brain: a long-lived `claude` process driven over its stream-JSON protocol.

The installed `claude` binary is the Claude Agent SDK runtime — it owns the model
loop, tool dispatch, context management and authentication. Jeeves drives it in
headless mode and supplies its own MCP tool server, system prompt and permission
policy.

One Agent instance == one process == one conversation, so context is preserved
across turns without paying re-spawn latency.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import config, memory, policy, prompt

# Built-in Claude Code tools Jeeves keeps. Every mutating operation goes through
# Jeeves' own audited MCP tools instead, so Write/Edit/Bash are deliberately out.
BUILTIN_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite"]


# Environment variables that tie a process to a *parent* Claude Code session:
# monitoring sockets, proxy ports, session ids. A child runtime that inherits
# them tries to attach to the parent's session and fails at startup. Jeeves
# always starts a fresh runtime, so these are stripped.
INHERITED_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SHELL_PREFIX",
    "APPLE_CLAUDE_CODE_LOG_SOCKET",
    "APPLE_CLAUDE_CODE_TOOL_SOCKET",
    "APPLE_CLAUDE_CODE_SECURITY_SOCKET",
    "APPLE_CLAUDE_CODE_SESSION_PID",
    "APPLE_CLAUDE_CODE_PORT",
    "APPLE_CLAUDE_CODE_PROXY_URL",
)


def runtime_env() -> dict[str, str]:
    """A clean environment for the agent runtime."""
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_SESSION_VARS}
    env["PYTHONPATH"] = str(config.SRC_ROOT)
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "16000")
    return env


class AgentError(RuntimeError):
    pass


@dataclass
class Event:
    """One thing that happened during a turn."""

    kind: str  # "text" | "thinking" | "tool" | "tool_result" | "result" | "error"
    text: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn:
    reply: str = ""
    tools_used: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    duration_s: float = 0.0
    turns: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def find_claude() -> str:
    binary = os.environ.get("JEEVES_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise AgentError(
            "the `claude` command was not found on PATH. Jeeves uses it as its "
            "agent runtime. Install Claude Code, or set JEEVES_CLAUDE_BIN."
        )
    return binary


def mcp_config() -> str:
    """The MCP server block, as a JSON string for --mcp-config."""
    return json.dumps(
        {
            "mcpServers": {
                "jeeves": {
                    "command": sys.executable,
                    "args": ["-m", "jeeves.mcp.server"],
                    "env": {
                        "PYTHONPATH": str(config.SRC_ROOT),
                        "JEEVES_STATE_DIR": str(config.STATE_DIR),
                        "JEEVES_CONFIG_DIR": str(config.CONFIG_DIR),
                        "JEEVES_CACHE_DIR": str(config.CACHE_DIR),
                        "HOME": str(Path.home()),
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
                    },
                }
            }
        }
    )


class Agent:
    """A conversation with Jeeves."""

    def __init__(
        self,
        *,
        voice: bool = False,
        session_id: str | None = None,
        interface: str = "cli",
        extra_prompt: str = "",
        model: str = "",
        turn_timeout: float = 0.0,
    ) -> None:
        self.cfg = config.load()
        self.voice = voice
        self.interface = interface
        self.session_id = session_id or str(uuid.uuid4())
        self.model = model or str(self.cfg.get("agent.model") or "")
        self.extra_prompt = extra_prompt
        # 0 means "use the configured default".
        self.turn_timeout = turn_timeout or float(self.cfg.get("agent.turn_timeout", 900))
        self.proc: subprocess.Popen[str] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._stderr_thread: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    def argv(self) -> list[str]:
        cfg = self.cfg
        argv = [
            find_claude(),
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--session-id", self.session_id,
            "--system-prompt", prompt.build(voice=self.voice, extra=self.extra_prompt),
            "--mcp-config", mcp_config(),
            "--strict-mcp-config",
            "--settings", policy.settings_json(),
            "--permission-mode", "acceptEdits",
            "--tools", ",".join(BUILTIN_TOOLS),
            "--allowedTools", *policy.allowed_tools(),
            "--effort", str(cfg.get("agent.effort", "high")),
        ]
        if self.model:
            argv += ["--model", self.model]
        for extra in cfg.get("agent.extra_dirs", []) or []:
            argv += ["--add-dir", str(extra)]
        return argv

    def start(self) -> None:
        if self.proc is not None:
            return
        config.ensure_dirs()
        memory.touch_session(self.session_id, self.interface)

        try:
            self.proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
                self.argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.cfg.get("agent.workdir") or Path.home()),
                env=runtime_env(),
                start_new_session=True,  # detach from the caller's process group
            )
        except OSError as exc:
            raise AgentError(f"could not start the agent runtime: {exc}") from None

        self._selector = selectors.DefaultSelector()
        self._selector.register(self.proc.stdout, selectors.EVENT_READ)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            with Path(config.AGENT_LOG).open("a", encoding="utf-8") as log:
                log.write(f"\n--- session {self.session_id} {time.ctime()} ---\n")
                for line in self.proc.stderr:
                    log.write(line)
                    log.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self._closed = True
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            self.proc.kill()
        finally:
            if self._selector is not None:
                self._selector.close()
                self._selector = None
            self.proc = None

    def __enter__(self) -> Agent:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- turns

    def _write(self, text: str) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        frame = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        try:
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AgentError(
                "the agent runtime stopped accepting input. See "
                f"{config.AGENT_LOG} for its output. ({exc})"
            ) from None

    def _readline(self, deadline: float) -> str | None:
        """Read one line from the agent, honouring an absolute deadline."""
        assert self.proc is not None and self.proc.stdout is not None
        assert self._selector is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the agent did not finish in time")
            if not self._selector.select(timeout=min(remaining, 1.0)):
                if self.proc.poll() is not None:
                    return None
                continue
            line = self.proc.stdout.readline()
            if line == "":
                return None
            if line.strip():
                return line

    def _diagnose_exit(self) -> str:
        """Turn a runtime crash into an error the user can act on."""
        code = self.proc.poll() if self.proc else None
        tail = ""
        try:
            lines = Path(config.AGENT_LOG).read_text(errors="replace").splitlines()
            tail = "\n".join(lines[-25:])
        except OSError:
            pass

        if "sandbox_apply" in tail or "sandbox-exec" in tail:
            return (
                "The agent runtime could not start because it is already running "
                "inside another Claude Code session — nested sandboxes are not "
                "permitted.\n\n"
                "Run Jeeves from a normal Terminal window rather than from inside "
                "Claude Code."
            )
        if "not logged in" in tail.lower() or "authentication" in tail.lower():
            return (
                "The agent runtime could not authenticate. Run `claude` once "
                "interactively to sign in, then try Jeeves again."
            )
        if "Duplicate" in tail and code != 0:
            return (
                "The agent runtime reported duplicate Claude Code installations "
                "and exited. Run `claude diagnose` and remove the stale copies."
            )
        return (
            f"The agent runtime exited (status {code}) before finishing.\n"
            f"Last lines of {config.AGENT_LOG}:\n{tail[-800:] or '(log empty)'}"
        )

    def stream(self, text: str) -> Iterator[Event]:
        """Send a message and yield events until the turn completes."""
        if self._closed:
            raise AgentError("this agent has been closed")
        self.start()
        memory.log_message(self.session_id, "user", text)
        self._write(text)

        timeout = self.turn_timeout
        deadline = time.monotonic() + timeout
        reply_parts: list[str] = []

        while True:
            try:
                line = self._readline(deadline)
            except TimeoutError:
                yield Event(
                    "error",
                    text=(
                        f"the agent runtime did not finish within {timeout:.0f}s.\n"
                        f"Check {config.AGENT_LOG} — it captures everything the "
                        "runtime prints. A first run can be slow while it starts "
                        "the tool server; a permanent stall usually means the "
                        "runtime is waiting for something it cannot ask for."
                    ),
                )
                return
            if line is None:
                yield Event("error", text=self._diagnose_exit())
                return

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue

            kind = message.get("type")

            if kind == "system" and message.get("subtype") == "init":
                reported = message.get("session_id")
                if reported:
                    self.session_id = reported
                continue

            if kind == "assistant":
                for block in (message.get("message") or {}).get("content") or []:
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        reply_parts.append(block["text"])
                        yield Event("text", text=block["text"], raw=block)
                    elif btype == "thinking" and block.get("thinking"):
                        yield Event("thinking", text=block["thinking"], raw=block)
                    elif btype == "tool_use":
                        yield Event(
                            "tool",
                            tool=str(block.get("name", "")),
                            args=block.get("input") or {},
                            raw=block,
                        )
                continue

            if kind == "user":
                for block in (message.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_result":
                        body = block.get("content")
                        if isinstance(body, list):
                            body = " ".join(
                                part.get("text", "")
                                for part in body
                                if isinstance(part, dict)
                            )
                        yield Event("tool_result", text=str(body or ""), raw=block)
                continue

            if kind == "result":
                final = (message.get("result") or "").strip() or "\n".join(reply_parts).strip()
                if message.get("is_error"):
                    yield Event("error", text=final or "the agent reported an error", raw=message)
                    return
                memory.log_message(self.session_id, "assistant", final)
                yield Event("result", text=final, raw=message)
                return

    def ask(self, text: str, on_event=None) -> Turn:
        """Send a message and block until the reply is complete."""
        turn = Turn()
        started = time.monotonic()
        for event in self.stream(text):
            if on_event is not None:
                on_event(event)
            if event.kind == "tool":
                turn.tools_used.append(event.tool)
            elif event.kind == "result":
                turn.reply = event.text
                turn.cost_usd = float(event.raw.get("total_cost_usd") or 0.0)
                turn.turns = int(event.raw.get("num_turns") or 0)
            elif event.kind == "error":
                turn.error = event.text
        turn.duration_s = time.monotonic() - started
        return turn


def one_shot(text: str, *, voice: bool = False, model: str = "") -> Turn:
    """Convenience: run a single question in a throwaway session."""
    with Agent(voice=voice, interface="one-shot", model=model) as agent:
        return agent.ask(text)
