"""A small local HTTP API, so Shortcuts, Raycast, scripts or your phone can ask.

Bound to 127.0.0.1 by default and protected by a bearer token generated on first
run. Endpoints:

    GET  /health                      -> {"ok": true, ...}
    POST /ask     {"prompt", "session"?, "voice"?}  -> {"reply", ...}
    GET  /ask?prompt=...              -> same, convenient from Shortcuts
    GET  /memory?q=...                -> remembered facts
    GET  /audit?limit=n               -> recent actions

Every request must carry `Authorization: Bearer <token>`, or `?token=<token>`
for callers that cannot set headers (Shortcuts).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import agent, config, memory

MAX_BODY = 256 * 1024
_sessions: dict[str, agent.Agent] = {}
_lock = threading.Lock()


def _get_agent(name: str) -> agent.Agent:
    """Reuse a named conversation so follow-up questions keep context."""
    with _lock:
        existing = _sessions.get(name)
        if existing is not None:
            return existing
        created = agent.Agent(interface="http")
        created.start()
        _sessions[name] = created
        return created


def _close_all() -> None:
    with _lock:
        for conversation in _sessions.values():
            conversation.close()
        _sessions.clear()


class Handler(BaseHTTPRequestHandler):
    server_version = "Jeeves/1.0"
    token = ""

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, default=str, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = ""
        if header.startswith("Bearer "):
            supplied = header[7:].strip()
        elif "token" in query:
            supplied = query["token"][0]
        # Constant-time compare so the token cannot be probed by timing.
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, OSError):
            return {}

    # -------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send(200, {"ok": True, "service": "jeeves", "time": time.time()})
            return
        if not self._authorised(query):
            self._send(401, {"error": "missing or invalid token"})
            return

        if parsed.path == "/ask":
            prompt = (query.get("prompt") or query.get("q") or [""])[0]
            if not prompt.strip():
                self._send(400, {"error": "prompt is required"})
                return
            self._answer(
                prompt,
                (query.get("session") or ["default"])[0],
                (query.get("voice") or ["0"])[0] in {"1", "true", "yes"},
            )
        elif parsed.path == "/memory":
            facts = memory.recall((query.get("q") or [""])[0], limit=50)
            self._send(200, {"facts": [f.render() for f in facts]})
        elif parsed.path == "/audit":
            try:
                limit = int((query.get("limit") or ["25"])[0])
            except ValueError:
                limit = 25
            self._send(200, {"entries": memory.recent_audit(max(1, min(limit, 200)))})
        else:
            self._send(404, {"error": f"no such endpoint: {parsed.path}"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._send(401, {"error": "missing or invalid token"})
            return
        if parsed.path != "/ask":
            self._send(404, {"error": f"no such endpoint: {parsed.path}"})
            return
        payload = self._body()
        prompt = str(payload.get("prompt") or payload.get("q") or "").strip()
        if not prompt:
            self._send(400, {"error": "prompt is required"})
            return
        self._answer(
            prompt,
            str(payload.get("session") or "default"),
            bool(payload.get("voice")),
        )

    def _answer(self, prompt: str, session: str, voice: bool) -> None:
        try:
            conversation = _get_agent(session)
            turn = conversation.ask(prompt)
        except agent.AgentError as exc:
            self._send(503, {"error": str(exc)})
            return

        if voice and turn.reply:
            from .mac import speak
            from .voice import speakable

            speak(speakable(turn.reply), blocking=False)

        self._send(
            200 if turn.ok else 500,
            {
                "reply": turn.reply,
                "ok": turn.ok,
                "error": turn.error or None,
                "tools": turn.tools_used,
                "session": session,
                "session_id": conversation.session_id,
                "cost_usd": round(turn.cost_usd, 6),
                "duration_s": round(turn.duration_s, 2),
            },
        )


def run(host: str = "", port: int = 0) -> int:
    cfg = config.load()
    host = host or str(cfg.get("server.host", "127.0.0.1"))
    port = port or int(cfg.get("server.port", 8787))
    token = config.server_token()
    Handler.token = token

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    print(f"Jeeves API listening on http://{host}:{port}")
    print(f"Token: {token}")
    print("\nTry it:")
    print(f'  curl -s -H "Authorization: Bearer {token}" \\')
    print(f"       --get --data-urlencode 'prompt=what is my battery level' \\")
    print(f"       http://{host}:{port}/ask | python3 -m json.tool")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"\n!! Bound to {host}, which is reachable from your network. Anyone "
            "with the token can drive your Mac. Prefer 127.0.0.1."
        )
    print("\nCtrl-C to stop.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        httpd.shutdown()
        _close_all()
    return 0
