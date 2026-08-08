"""Command-line entry point. Pure argparse, no dependencies."""

from __future__ import annotations

import argparse
import sys

from . import __version__, config

EPILOG = """\
examples:
  jeeves                                 open the terminal chat
  jeeves ask "what's on my calendar today?"
  jeeves ask --notify "summarise my unread mail"
  jeeves voice                           converse out loud
  jeeves voice --once                     answer a single spoken request
  jeeves local "read my messages"         no AI at all, offline, free
  jeeves local --voice                    offline voice assistant
  jeeves local --list                     every phrase local mode knows
  jeeves menubar                          live in the menu bar
  jeeves serve                            start the local HTTP API
  jeeves doctor                           check permissions and wiring
  jeeves policy                           show what needs confirmation
  jeeves memory --search sarah            search what Jeeves remembers
  jeeves audit -n 40                      show what Jeeves has done
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jeeves",
        description="Jeeves — a personal assistant with hands on your Mac.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"jeeves {__version__}")
    parser.add_argument(
        "--model",
        default="",
        help="override the model for this run (e.g. opus, sonnet)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("chat", help="interactive terminal chat (default)")

    ask = sub.add_parser("ask", help="ask a single question and print the reply")
    ask.add_argument("prompt", nargs="+", help="what to ask")
    ask.add_argument("--notify", action="store_true", help="also post a notification")
    ask.add_argument("--speak", action="store_true", help="also read the reply aloud")
    ask.add_argument("--quiet", action="store_true", help="print only the reply text")
    ask.add_argument("--json", action="store_true", help="print the full result as JSON")

    voice = sub.add_parser("voice", help="talk to Jeeves out loud")
    voice.add_argument("--once", action="store_true", help="handle one request then exit")
    voice.add_argument("--notify", action="store_true", help="post replies as notifications")
    voice.add_argument("--silent", action="store_true", help="transcribe but do not speak")

    serve = sub.add_parser("serve", help="run the local HTTP API")
    serve.add_argument("--host", default="", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=0, help="port (default 8787)")

    sub.add_parser("menubar", help="run the menu-bar app")

    local = sub.add_parser(
        "local",
        help="no-AI mode: a fixed command grammar, fully offline",
    )
    local.add_argument("words", nargs="*", help="the command; omit for an interactive prompt")
    local.add_argument("--voice", action="store_true", help="listen and reply out loud, offline")
    local.add_argument("--speak", action="store_true", help="read answers aloud")
    local.add_argument("--list", action="store_true", help="list every phrase it understands")

    doctor = sub.add_parser("doctor", help="check permissions, tools and the runtime")
    doctor.add_argument(
        "--skip-runtime",
        action="store_true",
        help="skip the live model turn (faster, no token cost)",
    )

    sub.add_parser("policy", help="show the safety policy and tool tiers")
    sub.add_parser("tools", help="list every tool Jeeves has")

    mem = sub.add_parser("memory", help="inspect or edit what Jeeves remembers")
    mem.add_argument("--search", default="", help="search text")
    mem.add_argument("--add", default="", help="remember a durable fact about you")
    mem.add_argument("--forget", type=int, default=0, help="delete a fact by id")
    mem.add_argument("-n", "--limit", type=int, default=25, help="how many to show")

    audit = sub.add_parser("audit", help="show what Jeeves has actually done")
    audit.add_argument("-n", "--limit", type=int, default=25, help="how many entries")
    audit.add_argument("--all", action="store_true", help="include read-only lookups")

    return parser


def cmd_ask(args: argparse.Namespace) -> int:
    from . import agent, mac

    prompt = " ".join(args.prompt)
    turn = agent.one_shot(prompt, voice=args.speak, model=args.model)

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "prompt": prompt,
                    "reply": turn.reply,
                    "ok": turn.ok,
                    "error": turn.error or None,
                    "tools": turn.tools_used,
                    "cost_usd": round(turn.cost_usd, 6),
                    "duration_s": round(turn.duration_s, 2),
                },
                indent=2,
            )
        )
        return 0 if turn.ok else 1

    if not turn.ok:
        print(turn.error, file=sys.stderr)
        return 1

    print(turn.reply)
    if not args.quiet and turn.tools_used:
        print(
            f"\n\033[2m{len(turn.tools_used)} tool call(s) · "
            f"{turn.duration_s:.1f}s · ${turn.cost_usd:.4f}\033[0m",
            file=sys.stderr,
        )
    if args.notify:
        mac.notify("Jeeves", turn.reply[:240])
    if args.speak:
        from .voice import speakable

        mac.speak(speakable(turn.reply))
    return 0


def cmd_menubar(args: argparse.Namespace) -> int:
    from . import mac

    if not config.NATIVE_BIN.exists():
        print(
            "The native helper is not built. Run: bash scripts/build_native.sh",
            file=sys.stderr,
        )
        return 1
    launcher = config.REPO_ROOT / "bin" / "jeeves"
    print("Jeeves is in the menu bar (look for 🤵). Ctrl-C here to quit.")
    result = mac.run([str(config.NATIVE_BIN), "bar", str(launcher)], timeout=86400)
    return 0 if result.ok else 1


def cmd_memory(args: argparse.Namespace) -> int:
    from . import memory

    if args.add:
        fact = memory.remember(args.add, kind="profile")
        print(f"Remembered #{fact.id}: {fact.text}")
        return 0
    if args.forget:
        print(
            f"Forgot fact #{args.forget}."
            if memory.forget(args.forget)
            else f"No fact with id {args.forget}."
        )
        return 0
    facts = memory.recall(args.search, limit=args.limit)
    if not facts:
        print("Nothing remembered yet." if not args.search else "No matches.")
        return 0
    for fact in facts:
        print(fact.render())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    import time

    from . import memory

    entries = memory.recent_audit(args.limit)
    if not entries:
        print("Nothing in the audit log yet.")
        return 0
    for entry in entries:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))
        detail = (entry["detail"] or "").splitlines()
        summary = detail[0][:90] if detail else ""
        line = f"{when}  {entry['tool']:<24} {entry['outcome']:<22} {summary}"
        if entry["undo"]:
            line += f"\n{' ' * 21}undo: {entry['undo']}"
        print(line)
    print(f"\nFull log: {config.AUDIT_LOG}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    from .mcp import registry
    from .mcp import tools as _tools  # noqa: F401  (registers everything)

    tiers = registry.by_risk()
    labels = {
        "read": "Read-only — run immediately",
        "write": "Reversible changes — run immediately in guarded mode",
        "risky": "Gated — always ask the user first",
    }
    for tier in ("read", "write", "risky"):
        print(f"\n\033[1m{labels[tier]}\033[0m ({len(tiers[tier])})")
        for name in tiers[tier]:
            spec = registry.REGISTRY[name]
            first_line = spec.description.split("\n")[0]
            print(f"  {name:<24} {first_line[:80]}")
    print(f"\n{len(registry.REGISTRY)} tools total.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "chat"

    try:
        if command == "chat":
            from . import tui

            return tui.run(model=args.model)
        if command == "ask":
            return cmd_ask(args)
        if command == "voice":
            from . import voice

            return voice.run(once=args.once, notify=args.notify, silent=args.silent)
        if command == "serve":
            from . import server

            return server.run(host=args.host, port=args.port)
        if command == "menubar":
            return cmd_menubar(args)
        if command == "local":
            from . import local as local_mode

            if args.list:
                return local_mode.list_rules()
            if args.voice:
                return local_mode.voice_loop()
            if args.words:
                return local_mode.run_once(" ".join(args.words), speak=args.speak)
            return local_mode.repl(speak=args.speak)
        if command == "doctor":
            from . import doctor

            return doctor.run(skip_runtime=args.skip_runtime)
        if command == "policy":
            from . import policy

            print(policy.describe())
            return 0
        if command == "tools":
            return cmd_tools(args)
        if command == "memory":
            return cmd_memory(args)
        if command == "audit":
            return cmd_audit(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard, keep it friendly
        from . import agent

        if isinstance(exc, agent.AgentError):
            print(f"jeeves: {exc}", file=sys.stderr)
            return 1
        raise

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
