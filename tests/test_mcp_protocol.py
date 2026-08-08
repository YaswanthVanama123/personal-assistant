"""Exercise the MCP server exactly as a client would: real JSON-RPC over stdio."""

import json
import subprocess
import sys

FRAMES = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    # read-only: must run immediately
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "shell", "arguments": {"command": "echo hello && whoami"}}},
    # mutating shell: must be gated
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "shell", "arguments": {"command": "git push origin main"}}},
    # hard-denied: refused even with confirm
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "shell", "arguments": {"command": "rm -rf ~/Documents", "confirm": True}}},
    # RISKY tool without confirm -> confirmation block
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "imessage_send",
                "arguments": {"recipient": "+15551234567", "text": "hi there"}}},
    # WRITE tool in guarded mode -> runs
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "clipboard_write", "arguments": {"text": "jeeves smoke test"}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
     "params": {"name": "clipboard_read", "arguments": {}}},
    # memory round trip
    {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
     "params": {"name": "remember",
                "arguments": {"text": "Prefers metric units.", "kind": "profile",
                              "tags": ["prefs"]}}},
    {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
     "params": {"name": "recall", "arguments": {"query": "metric"}}},
    # validation errors
    {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
     "params": {"name": "volume_set", "arguments": {"level": 500}}},
    {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
     "params": {"name": "volume_set", "arguments": {"bogus": 1}}},
    {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
     "params": {"name": "no_such_tool", "arguments": {}}},
    # protected path
    {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
     "params": {"name": "read_file", "arguments": {"path": "~/.ssh/id_rsa"}}},
    # unknown method -> JSON-RPC error
    {"jsonrpc": "2.0", "id": 15, "method": "does/not/exist"},
    {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
     "params": {"name": "system_info", "arguments": {}}},
]

payload = "".join(json.dumps(f) + "\n" for f in FRAMES)
proc = subprocess.run(
    [sys.executable, "-m", "jeeves.mcp.server"],
    input=payload, capture_output=True, text=True, timeout=180,
    env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
         "HOME": subprocess.os.environ["HOME"]},
)

print("=== stderr ===")
print(proc.stderr.strip()[:800] or "(none)")
print("\n=== responses ===")
ok = 0
for line in proc.stdout.splitlines():
    if not line.strip():
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        print(f"!! NON-JSON ON STDOUT: {line[:120]}")
        continue
    ok += 1
    rid = msg.get("id")
    if "error" in msg:
        print(f"[{rid}] JSON-RPC error: {msg['error']['message']}")
        continue
    res = msg["result"]
    if "tools" in res:
        print(f"[{rid}] tools/list -> {len(res['tools'])} tools")
    elif "content" in res:
        text = res["content"][0]["text"].replace("\n", " ⏎ ")
        flag = " [isError]" if res.get("isError") else ""
        print(f"[{rid}]{flag} {text[:150]}")
    else:
        print(f"[{rid}] {json.dumps(res)[:120]}")
print(f"\n{ok} well-formed frames on stdout")
