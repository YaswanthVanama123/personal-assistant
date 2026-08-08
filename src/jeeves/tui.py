"""Terminal chat: a readline REPL with live tool activity."""

from __future__ import annotations

import sys
import time

from . import agent, config, memory

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
BLUE = "\033[38;5;75m"
GREEN = "\033[38;5;114m"
YELLOW = "\033[38;5;179m"
RED = "\033[38;5;203m"
GREY = "\033[38;5;245m"

BANNER = f"""{BOLD}{BLUE}
   ╭───────────────────────────────╮
   │   Jeeves — at your service    │
   ╰───────────────────────────────╯{RESET}
{GREY}  Ask for anything. /help for commands, /quit to leave.{RESET}
"""

HELP = f"""{BOLD}Commands{RESET}
  /help              this list
  /quit  /exit       leave (Ctrl-D also works)
  /new               start a fresh conversation (clears context)
  /memory [query]    show what Jeeves remembers
  /audit [n]         show what Jeeves has actually done
  /policy            show the current safety policy
  /voice             speak the last reply out loud
  /session           show the session id and cost so far
  /verbose           toggle showing tool arguments
"""


def _supports_colour() -> bool:
    return sys.stdout.isatty()


class Chat:
    def __init__(self, *, model: str = "") -> None:
        self.cfg = config.load()
        self.agent = agent.Agent(interface="chat", model=model)
        self.verbose = False
        self.last_reply = ""
        self.total_cost = 0.0
        self.colour = _supports_colour()
        self._streamed = False

    def c(self, text: str, colour: str) -> str:
        return f"{colour}{text}{RESET}" if self.colour else text

    # ------------------------------------------------------------ rendering

    def on_event(self, event: agent.Event) -> None:
        if event.kind == "tool":
            name = event.tool.removeprefix("mcp__jeeves__")
            detail = ""
            if self.verbose and event.args:
                items = ", ".join(
                    f"{k}={str(v)[:40]}" for k, v in event.args.items() if k != "confirm"
                )
                detail = f" {DIM}{items}{RESET}" if self.colour else f" {items}"
            print(f"  {self.c('▸', YELLOW)} {self.c(name, GREY)}{detail}", flush=True)
        elif event.kind == "text":
            # Print assistant text as it arrives so long turns feel responsive.
            if not self._streamed:
                print(f"\n{self.c('Jeeves', BLUE)}  ", end="", flush=True)
                self._streamed = True
            print(event.text, end="", flush=True)
        elif event.kind == "error":
            print(f"\n{self.c('✗', RED)} {event.text}", flush=True)

    def ask(self, text: str) -> None:
        self._streamed = False
        started = time.monotonic()
        turn = self.agent.ask(text, on_event=self.on_event)
        self.total_cost += turn.cost_usd

        if turn.reply and not self._streamed:
            print(f"\n{self.c('Jeeves', BLUE)}  {turn.reply}")
        elif self._streamed:
            print()

        if turn.ok:
            self.last_reply = turn.reply
        elapsed = time.monotonic() - started
        meta = f"{elapsed:.1f}s"
        if turn.cost_usd:
            meta += f" · ${turn.cost_usd:.4f}"
        if turn.tools_used:
            meta += f" · {len(turn.tools_used)} tool call(s)"
        print(f"{self.c(meta, GREY)}\n")

    # ------------------------------------------------------------- commands

    def command(self, line: str) -> bool:
        """Handle a /command. Returns False when the session should end."""
        parts = line.split(maxsplit=1)
        verb = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if verb in {"/quit", "/exit", "/q"}:
            return False
        if verb == "/help":
            print(HELP)
        elif verb == "/new":
            self.agent.close()
            self.agent = agent.Agent(interface="chat")
            print(self.c("Started a fresh conversation.\n", GREY))
        elif verb == "/memory":
            facts = memory.recall(arg, limit=25)
            print("\n".join(f.render() for f in facts) or "Nothing remembered yet.")
            print()
        elif verb == "/audit":
            limit = int(arg) if arg.isdigit() else 15
            for entry in memory.recent_audit(limit):
                when = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
                print(f"{when}  {entry['tool']:<22} {entry['outcome']}")
            print()
        elif verb == "/policy":
            from . import policy

            print(policy.describe(), "\n")
        elif verb == "/voice":
            from .mac import speak

            if self.last_reply:
                speak(self.last_reply, blocking=False)
                print(self.c("Speaking…\n", GREY))
            else:
                print(self.c("Nothing to speak yet.\n", GREY))
        elif verb == "/session":
            print(f"session {self.agent.session_id}  ·  ${self.total_cost:.4f} this session\n")
        elif verb == "/verbose":
            self.verbose = not self.verbose
            print(self.c(f"Tool arguments {'shown' if self.verbose else 'hidden'}.\n", GREY))
        else:
            print(self.c(f"Unknown command {verb}. Try /help.\n", GREY))
        return True

    # ----------------------------------------------------------------- loop

    def run(self) -> int:
        try:
            import readline  # noqa: F401  - enables line editing and history

            history = config.STATE_DIR / "chat-history"
            config.ensure_dirs()
            try:
                readline.read_history_file(history)
            except (OSError, ValueError):
                pass
            readline.set_history_length(2000)
        except ImportError:
            history = None

        print(BANNER if self.colour else "Jeeves — at your service. /help for commands.\n")
        try:
            self.agent.start()
        except agent.AgentError as exc:
            print(f"{self.c('✗', RED)} {exc}")
            return 1

        try:
            while True:
                try:
                    line = input(f"{self.c('you', GREEN)}  " if self.colour else "you  ").strip()
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print(f"\n{self.c('(interrupted — /quit to leave)', GREY)}")
                    continue
                if not line:
                    continue
                if line.startswith("/"):
                    if not self.command(line):
                        break
                    continue
                try:
                    self.ask(line)
                except agent.AgentError as exc:
                    print(f"{self.c('✗', RED)} {exc}\n")
                    break
                except KeyboardInterrupt:
                    print(f"\n{self.c('(turn interrupted)', GREY)}\n")
        finally:
            self.agent.close()
            if history is not None:
                try:
                    import readline

                    readline.write_history_file(history)
                except (OSError, ImportError):
                    pass
        print(self.c("Very good, sir.", GREY))
        return 0


def run(model: str = "") -> int:
    return Chat(model=model).run()
