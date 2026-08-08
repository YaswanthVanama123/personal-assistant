"""A local-model brain, spoken to over Ollama's HTTP API.

This is the intelligence fallback for local mode: when the command grammar has no
rule for what you said, the utterance is handed to a model running on this Mac.
Nothing leaves the machine, there is no account, no API key and no per-request
cost — the trade is a one-time model download and Ollama being installed.

Only Python's standard library is used; Ollama speaks JSON over HTTP on
127.0.0.1:11434, so no client package is needed.

Safety: by default the model is given the read-only and reversible tools but NOT
the gated ones. A 7B model should not be able to send a message, run a shell
command or delete anything, even by accident. Set brain.allow_risky = true to
change that, and the confirm gate still applies on top.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, memory, prompt
from .mcp import registry
from .mcp import tools as _tools  # noqa: F401  (registers every tool)

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"
MAX_TOOL_ROUNDS = 6


class OllamaError(RuntimeError):
    """Something went wrong talking to the local model. Message is user-facing."""


@dataclass
class Reply:
    text: str = ""
    tools_used: list[str] = field(default_factory=list)
    rounds: int = 0
    duration_s: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _post(path: str, payload: dict, host: str, timeout: float) -> dict:
    request = urllib.request.Request(
        host.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 404:
            raise OllamaError(
                f"the model is not installed. Run:  ollama pull {payload.get('model')}"
            ) from None
        raise OllamaError(f"Ollama returned HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"could not reach Ollama at {host} ({exc.reason}). Start it with "
            "`ollama serve`, or install it with `brew install ollama`."
        ) from None
    except TimeoutError:
        raise OllamaError(f"Ollama did not respond within {timeout:.0f}s") from None
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Ollama sent malformed JSON: {exc}") from None


def available(host: str = "", timeout: float = 4.0) -> tuple[bool, str]:
    """Is Ollama running, and which models does it have?"""
    host = host or str(config.load().get("brain.host", DEFAULT_HOST))
    try:
        with urllib.request.urlopen(  # noqa: S310
            host.rstrip("/") + "/api/tags", timeout=timeout
        ) as response:
            names = [
                m.get("name", "")
                for m in (json.loads(response.read().decode()).get("models") or [])
            ]
        return True, ", ".join(names) if names else "no models pulled yet"
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
        return False, str(exc)[:120]


def tool_specs(allow_risky: bool = False, only: list[str] | None = None) -> list[dict]:
    """The tool catalogue in the function-calling shape Ollama expects."""
    specs: list[dict] = []
    for name, spec in sorted(registry.REGISTRY.items()):
        if only is not None and name not in only:
            continue
        if not allow_risky and spec.risk == registry.RISKY:
            continue
        schema = dict(spec.schema)
        # A local model does not need to see the confirm flag; the gate is ours.
        properties = {k: v for k, v in schema["properties"].items() if k != "confirm"}
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.description.split("\n\n")[0][:400],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": schema["required"],
                    },
                },
            }
        )
    return specs


SYSTEM_SUFFIX = """

You are running as a local model on this Mac, reached only when the fast command
grammar had no rule for what the user said. Two consequences:

* Prefer calling a tool over describing one. If a tool answers the question, call
  it and report the result.
* Answer in one or two short sentences. Your reply may be read aloud.

If no tool fits and you cannot answer from what you know, say so plainly in one
sentence rather than guessing.
"""


class Brain:
    """A conversation with the local model."""

    def __init__(
        self,
        *,
        model: str = "",
        host: str = "",
        voice: bool = False,
        transport: Callable[[str, dict, str, float], dict] | None = None,
    ) -> None:
        # transport is injectable so the tool loop can be tested without Ollama.
        self.transport = transport or _post
        cfg = config.load()
        self.model = model or str(cfg.get("brain.model", DEFAULT_MODEL))
        self.host = host or str(cfg.get("brain.host", DEFAULT_HOST))
        self.timeout = float(cfg.get("brain.timeout", 180))
        self.allow_risky = bool(cfg.get("brain.allow_risky", False))
        self.voice = voice
        self.tools = tool_specs(allow_risky=self.allow_risky)
        self.messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": prompt.build(voice=voice) + SYSTEM_SUFFIX,
            }
        ]

    def ask(self, said: str, on_tool: Callable[[str, dict], None] | None = None) -> Reply:
        started = time.monotonic()
        reply = Reply()
        self.messages.append({"role": "user", "content": said})
        memory.log_message("local-brain", "user", said)

        for round_number in range(1, MAX_TOOL_ROUNDS + 1):
            reply.rounds = round_number
            try:
                response = self.transport(
                    "/api/chat",
                    {
                        "model": self.model,
                        # A copy: the caller must not see later mutations.
                        "messages": list(self.messages),
                        "tools": self.tools,
                        "stream": False,
                        "options": {"temperature": 0.3},
                    },
                    self.host,
                    self.timeout,
                )
            except OllamaError as exc:
                reply.error = str(exc)
                reply.duration_s = time.monotonic() - started
                return reply

            message = response.get("message") or {}
            self.messages.append(message)
            calls = message.get("tool_calls") or []

            if not calls:
                reply.text = (message.get("content") or "").strip()
                reply.duration_s = time.monotonic() - started
                if reply.text:
                    memory.log_message("local-brain", "assistant", reply.text)
                else:
                    reply.error = "the local model returned an empty reply"
                return reply

            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}

                reply.tools_used.append(name)
                if on_tool is not None:
                    on_tool(name, arguments)

                text, _is_error = registry.call(name, arguments)
                self.messages.append(
                    {"role": "tool", "name": name, "content": text[:6000]}
                )

        reply.error = (
            f"the local model kept calling tools after {MAX_TOOL_ROUNDS} rounds "
            "without answering. Try rephrasing, or use a larger model."
        )
        reply.duration_s = time.monotonic() - started
        return reply


def one_shot(said: str, *, voice: bool = False) -> Reply:
    return Brain(voice=voice).ask(said)
