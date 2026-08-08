"""Test the local-model brain's agent loop with a fake transport.

No Ollama, no network, no model download — the transport is injected, so this
exercises tool-call parsing, tool execution, error handling and the loop cap
deterministically.
"""

import sys

sys.path.insert(0, "src")

from jeeves import brain  # noqa: E402
from jeeves.mcp import registry  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"  {label}\n    expected {want!r}\n    got      {got!r}")


def check_true(label: str, value) -> None:
    if not value:
        failures.append(f"  {label}: expected truthy, got {value!r}")


def scripted(*responses):
    """A transport that returns each response in turn, recording requests."""
    queue = list(responses)
    sent: list[dict] = []

    def transport(path, payload, host, timeout):
        sent.append(payload)
        if not queue:
            raise AssertionError("transport called more times than scripted")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    transport.sent = sent  # type: ignore[attr-defined]
    return transport


def say(text):
    return {"message": {"role": "assistant", "content": text}}


def call(name, arguments):
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    }


# ------------------------------------------------------- plain answer, no tools
transport = scripted(say("It is a Tuesday."))
reply = brain.Brain(transport=transport).ask("what sort of day is it")
check("plain answer text", reply.text, "It is a Tuesday.")
check("plain answer uses no tools", reply.tools_used, [])
check("plain answer is one round", reply.rounds, 1)
check_true("plain answer ok", reply.ok)
# The request must carry the tool catalogue and the system prompt.
check_true("tools were offered", len(transport.sent[0]["tools"]) > 30)
check("system message first", transport.sent[0]["messages"][0]["role"], "system")
check_true("Jeeves persona present", "Jeeves" in transport.sent[0]["messages"][0]["content"])
check("streaming disabled", transport.sent[0]["stream"], False)

# --------------------------------------------------- one tool call, then answer
transport = scripted(
    call("battery", {}),
    say("You are at 93 percent."),
)
reply = brain.Brain(transport=transport).ask("how much charge is left")
check("tool was invoked", reply.tools_used, ["battery"])
check("final text after tool", reply.text, "You are at 93 percent.")
check("two rounds", reply.rounds, 2)
# The tool result must be fed back in a tool-role message.
followup = transport.sent[1]["messages"]
check("tool result role", followup[-1]["role"], "tool")
check("tool result named", followup[-1]["name"], "battery")
check_true("tool result has content", "Battery" in followup[-1]["content"])

# ------------------------------------------- arguments arriving as a JSON string
transport = scripted(
    call("volume_get", '{"unused": 1}'),  # Ollama sometimes sends a string
    say("Volume is 40 percent."),
)
reply = brain.Brain(transport=transport).ask("how loud is it")
check_true("string arguments parsed without crashing", reply.ok)

# ----------------------------------------------- malformed arguments are survived
transport = scripted(
    call("battery", "not json at all"),
    say("93 percent."),
)
reply = brain.Brain(transport=transport).ask("charge")
check_true("unparseable arguments do not crash", reply.ok)

# ------------------------------------------------ a failing tool is reported back
transport = scripted(
    call("read_file", {"path": "~/.ssh/id_rsa"}),  # blocked by the path guard
    say("I cannot read that file."),
)
reply = brain.Brain(transport=transport).ask("read my ssh key")
check_true("blocked tool still returns to the model", reply.ok)
check_true(
    "guard message reached the model",
    "protected location" in transport.sent[1]["messages"][-1]["content"],
)

# --------------------------------------------------------------- transport error
transport = scripted(brain.OllamaError("could not reach Ollama at http://x (refused)"))
reply = brain.Brain(transport=transport).ask("anything")
check_true("transport error surfaces", not reply.ok)
check_true("error mentions Ollama", "Ollama" in reply.error)

# ------------------------------------------------------------- empty model reply
transport = scripted(say("   "))
reply = brain.Brain(transport=transport).ask("anything")
check_true("empty reply is an error", not reply.ok)

# ------------------------------------------------------------------- loop ceiling
transport = scripted(*[call("battery", {}) for _ in range(brain.MAX_TOOL_ROUNDS)])
reply = brain.Brain(transport=transport).ask("loop forever")
check_true("runaway loop is capped", not reply.ok)
check("stopped at the cap", reply.rounds, brain.MAX_TOOL_ROUNDS)
check_true("cap error explains itself", "rounds" in reply.error)

# ------------------------------------------------------- gated tools are withheld
safe = {s["function"]["name"] for s in brain.tool_specs(allow_risky=False)}
everything = {s["function"]["name"] for s in brain.tool_specs(allow_risky=True)}
for gated in registry.by_risk()["risky"]:
    if gated in safe:
        failures.append(f"  gated tool {gated!r} was offered to the local model")
    if gated not in everything:
        failures.append(f"  gated tool {gated!r} missing even with allow_risky")
check_true("safe set is smaller", len(safe) < len(everything))
check_true("safe set is still useful", len(safe) > 40)

# The confirm flag is ours to set, never the model's.
for spec in brain.tool_specs(allow_risky=True):
    if "confirm" in spec["function"]["parameters"]["properties"]:
        failures.append(f"  {spec['function']['name']} exposed the confirm flag")

# Every advertised tool must exist, and its schema must be well formed.
for spec in brain.tool_specs(allow_risky=True):
    function = spec["function"]
    if function["name"] not in registry.REGISTRY:
        failures.append(f"  advertised unknown tool {function['name']!r}")
    if spec.get("type") != "function":
        failures.append(f"  {function['name']} has the wrong spec type")
    if function["parameters"].get("type") != "object":
        failures.append(f"  {function['name']} parameters are not an object")
    if not function["description"]:
        failures.append(f"  {function['name']} has no description")

# ------------------------------------------------------- availability probe safety
reachable, detail = brain.available(host="http://127.0.0.1:9")  # nothing listens there
check("unreachable host reported", reachable, False)
check_true("failure detail given", bool(detail))

print(f"brain checks complete — {len(failures)} failure(s)")
if failures:
    print("\n".join(failures))
    sys.exit(1)
print(f"agent loop, tool dispatch and safety filtering all correct "
      f"({len(safe)} tools offered, {len(everything) - len(safe)} withheld)")
