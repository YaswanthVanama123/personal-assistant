"""Voice mode: on-device speech in, speech out.

Speech recognition and synthesis both stay on this Mac — the native helper uses
SFSpeechRecognizer with on-device recognition, and replies are spoken with the
system synthesiser. Only the text of a request reaches the model.
"""

from __future__ import annotations

import re
import sys
import time

from . import agent, config, mac

# Exit codes from `jeeves-native listen`
NO_SPEECH = 3
PERMISSION = 77

STOP_WORDS = {"stop", "quit", "exit", "goodbye", "good bye", "that's all", "thanks jeeves"}

# Strip markdown that would be read aloud as punctuation soup.
_MD_PATTERNS = (
    (re.compile(r"```.*?```", re.DOTALL), " (code omitted) "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),
    (re.compile(r"\*([^*]*)\*"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"^[-*]\s+", re.MULTILINE), ""),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://\S+"), "a link"),
    (re.compile(r"\n{2,}"), ". "),
    (re.compile(r"\s{2,}"), " "),
)


def speakable(text: str, limit: int = 700) -> str:
    """Reduce a reply to something that sounds right read aloud."""
    out = text
    for pattern, replacement in _MD_PATTERNS:
        out = pattern.sub(replacement, out)
    out = out.replace("\n", ". ").strip()
    if len(out) > limit:
        cut = out[:limit]
        stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        out = (cut[: stop + 1] if stop > limit * 0.5 else cut) + " That's the short version."
    return out


def listen(silence: float, timeout: float = 30.0) -> str | None:
    """Capture one utterance. Returns None when nothing was said."""
    if not config.NATIVE_BIN.exists():
        raise RuntimeError(
            "the native speech helper is not built. Run: bash scripts/build_native.sh"
        )
    result = mac.run(
        [str(config.NATIVE_BIN), "listen", "--silence", str(silence), "--timeout", str(timeout)],
        timeout=int(timeout) + 20,
    )
    if result.code == PERMISSION:
        raise PermissionError(result.err or "microphone or speech permission denied")
    if result.code == NO_SPEECH or not result.out.strip():
        return None
    if not result.ok:
        raise RuntimeError(result.err or "speech recognition failed")
    return result.out.strip()


class Voice:
    def __init__(self, *, speak_replies: bool = True, notify: bool = False) -> None:
        self.cfg = config.load()
        self.speak_replies = speak_replies and bool(self.cfg.get("voice.speak", True))
        self.notify = notify
        self.silence = float(self.cfg.get("voice.silence_timeout", 1.4))
        self.limit = int(self.cfg.get("voice.speak_limit", 700))
        self.agent = agent.Agent(voice=True, interface="voice")

    def _reply(self, text: str) -> None:
        print(f"\nJeeves  {text}\n", flush=True)
        if self.notify:
            mac.notify("Jeeves", text[:240])
        if self.speak_replies:
            mac.speak(speakable(text, self.limit))

    def turn(self, said: str) -> bool:
        """Handle one utterance. Returns False to end the session."""
        if said.strip().lower().rstrip(".!") in STOP_WORDS:
            self._reply("Very good. Call me when you need me.")
            return False

        shown: list[str] = []

        def on_event(event: agent.Event) -> None:
            if event.kind == "tool":
                name = event.tool.removeprefix("mcp__jeeves__")
                if name not in shown:
                    shown.append(name)
                    print(f"  ▸ {name}", flush=True)
            elif event.kind == "error":
                print(f"  ✗ {event.text}", flush=True)

        turn = self.agent.ask(said, on_event=on_event)
        if turn.error:
            self._reply("Something went wrong. " + turn.error.split("\n")[0])
            return True
        self._reply(turn.reply or "I'm not sure how to answer that.")
        return True

    def once(self) -> int:
        """Listen for a single request, answer it, exit."""
        try:
            self.agent.start()
            mac.speak("Yes?", blocking=False)
            print("Listening…", flush=True)
            said = listen(self.silence)
        except (PermissionError, RuntimeError) as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        if not said:
            print("(heard nothing)")
            return 0
        print(f"you     {said}")
        try:
            self.turn(said)
        finally:
            self.agent.close()
        return 0

    def loop(self) -> int:
        """Converse until the user says stop, or Ctrl-C."""
        wake = str(self.cfg.get("voice.wake_word", "jeeves"))
        print(
            f"Voice mode. Speak after the tone; say “stop” to finish.\n"
            f"(Wake word for hands-free use: “{wake}”)\n"
        )
        try:
            self.agent.start()
        except agent.AgentError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1

        mac.speak("At your service.", blocking=True)
        try:
            while True:
                print("Listening…", flush=True)
                try:
                    said = listen(self.silence)
                except PermissionError as exc:
                    print(f"✗ {exc}", file=sys.stderr)
                    return 1
                except RuntimeError as exc:
                    print(f"✗ {exc}", file=sys.stderr)
                    time.sleep(1)
                    continue
                if not said:
                    continue
                print(f"you     {said}")
                if not self.turn(said):
                    break
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            self.agent.close()
        return 0


def run(once: bool = False, notify: bool = False, silent: bool = False) -> int:
    voice = Voice(speak_replies=not silent, notify=notify)
    return voice.once() if once else voice.loop()
