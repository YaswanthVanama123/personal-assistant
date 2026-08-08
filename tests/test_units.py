"""Unit tests for logic that must be right and needs no macOS permissions."""

import sys

sys.path.insert(0, "src")

from jeeves import config, mac, prompt  # noqa: E402
from jeeves.mcp import registry  # noqa: E402
from jeeves.mcp import tools as _tools  # noqa: E402,F401
from jeeves.server import Handler  # noqa: E402
from jeeves.voice import speakable  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"  {label}\n    expected {want!r}\n    got      {got!r}")


def check_true(label: str, value: bool) -> None:
    if not value:
        failures.append(f"  {label}: expected true")


# ---------------------------------------------------------------- API auth
class FakeHandler:
    """Exercise Handler._authorised without opening a socket."""

    def __init__(self, header: str = "") -> None:
        self.headers = {"Authorization": header} if header else {}

    @property
    def token(self) -> str:
        return Handler.token

    _authorised = Handler._authorised


Handler.token = "s3cret-token"
check_true("bearer token accepted", FakeHandler("Bearer s3cret-token")._authorised({}))
check_true("wrong bearer rejected", not FakeHandler("Bearer nope")._authorised({}))
check_true("missing header rejected", not FakeHandler()._authorised({}))
check_true("empty bearer rejected", not FakeHandler("Bearer ")._authorised({}))
check_true(
    "query token accepted", FakeHandler()._authorised({"token": ["s3cret-token"]})
)
check_true("wrong query token rejected", not FakeHandler()._authorised({"token": ["x"]}))
check_true(
    "header wins over absent query",
    FakeHandler("Bearer s3cret-token")._authorised({"token": ["wrong"]}),
)
# An empty configured token must never authorise anything.
Handler.token = ""
check_true("empty server token rejects all", not FakeHandler("Bearer ")._authorised({}))
Handler.token = "s3cret-token"

# ------------------------------------------------------------ speech shaping
check(
    "markdown stripped",
    speakable("**Bold** and `code` and *em*"),
    "Bold and code and em",
)
check("headings stripped", speakable("## Heading\nBody"), "Heading. Body")
check("bullets stripped", speakable("- one\n- two"), "one. two")
check("links become text", speakable("See [docs](https://x.com/y)"), "See docs")
check("bare url spoken as 'a link'", speakable("Go to https://example.com now"),
      "Go to a link now")
check("code fence omitted", speakable("Before\n```\nx=1\n```\nAfter"),
      "Before (code omitted) After")
long_reply = ("First sentence here. " * 60).strip()
shortened = speakable(long_reply, limit=120)
check_true("long reply truncated", len(shortened) < 200)
check_true("truncation is flagged", shortened.endswith("That's the short version."))
check_true("truncation lands on a sentence", ". That's" in shortened)

# ------------------------------------------------------------- app name guard
for bad in ['Safari"; do shell script "rm -rf ~"', "Foo\nBar", "App'; quit", "x" * 80, ""]:
    try:
        mac.tell_literal(bad, "quit")
        failures.append(f"  app-name guard let through {bad!r}")
    except mac.MacError:
        pass
for good in ["Safari", "Google Chrome", "Visual Studio Code", "IINA", "Music"]:
    check_true(f"app-name guard accepts {good!r}", bool(mac._SAFE_APP_NAME.match(good)))

# ------------------------------------------------------------- tool registry
check_true("tools registered", len(registry.REGISTRY) > 50)
for name, spec in registry.REGISTRY.items():
    schema = spec.schema
    check_true(f"{name} has an object schema", schema.get("type") == "object")
    check_true(f"{name} forbids extra properties", schema.get("additionalProperties") is False)
    check_true(f"{name} has a description", len(spec.description) > 15)
    for required in schema["required"]:
        check_true(f"{name} required {required!r} is declared", required in schema["properties"])
    if spec.risk == registry.RISKY:
        check_true(f"{name} exposes a confirm flag", "confirm" in schema["properties"])
    for prop, node in schema["properties"].items():
        check_true(f"{name}.{prop} has a type", "type" in node)
        check_true(f"{name}.{prop} has a description", bool(node.get("description")))

# Risky tools must not run without confirmation, and must not act when gated.
text, is_error = registry.call("imessage_send", {"recipient": "+1", "text": "hi"})
check_true("gated tool returns a confirmation block", "CONFIRMATION REQUIRED" in text)
check_true("gated tool is not an error", not is_error)

text, is_error = registry.call("shell", {"command": "rm -rf /", "confirm": True})
check_true("hard deny beats confirm=true", is_error and "hard deny list" in text)

text, _ = registry.call("volume_set", {"level": "not-a-number"})
check_true("bad type rejected", "must be a integer" in text or "failed" in text)

# ------------------------------------------------------------------- prompt
built = prompt.build(voice=False)
check_true("prompt names the assistant", "Jeeves" in built)
check_true("prompt states today's date", "Today is" in built)
check_true("prompt explains the confirm protocol", "confirm=true" in built)
voiced = prompt.build(voice=True)
check_true("voice prompt asks for brevity", "spoken" in voiced.lower())
check_true("voice prompt forbids markdown", "markdown" in voiced.lower())

# -------------------------------------------------------------------- config
cfg = config.load()
check_true("config exposes dotted lookup", cfg.get("safety.mode") in {"guarded", "strict", "open"})
check("unknown key returns default", cfg.get("nope.nope", "fallback"), "fallback")
check_true("extra_dirs is a list", isinstance(cfg.get("agent.extra_dirs"), list))

# ----------------------------------------------------------------- reporting
total = 0  # count is implicit; report failures only
print(f"unit checks complete — {len(failures)} failure(s)")
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("all unit checks passed")
