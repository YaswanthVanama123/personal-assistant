"""Test the local HTTP API using a direct localhost socket."""

import http.client
import json
import re
import subprocess
import sys
import time

PORT = 8799
proc = subprocess.Popen(
    ["./bin/jeeves", "serve", "--port", str(PORT)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
)

token = ""
deadline = time.time() + 25
while time.time() < deadline:
    line = proc.stdout.readline()
    if not line:
        break
    match = re.match(r"^Token: (\S+)", line)
    if match:
        token = match.group(1)
    if "Ctrl-C to stop" in line:
        break
print(f"server up, token length {len(token)}")


def request(method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read().decode()
    conn.close()
    return response.status, payload


auth = {"Authorization": f"Bearer {token}"}
checks = [
    ("health, no auth needed",      "GET", "/health", None, 200),
    ("no token rejected",           "GET", "/memory", None, 401),
    ("bad token rejected",          "GET", "/memory", {"Authorization": "Bearer wrong"}, 401),
    ("good token accepted",         "GET", "/memory", auth, 200),
    ("query-string token accepted", "GET", f"/memory?token={token}", None, 200),
    ("unknown path 404",            "GET", "/nope", auth, 404),
    ("empty prompt 400",            "GET", "/ask?prompt=", auth, 400),
    ("audit readable",              "GET", "/audit?limit=2", auth, 200),
]

failures = 0
for label, method, path, headers, expected in checks:
    try:
        status, payload = request(method, path, headers)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERR  {label}: {type(exc).__name__} {exc}")
        failures += 1
        continue
    mark = "ok  " if status == expected else "FAIL"
    if status != expected:
        failures += 1
    snippet = payload.replace("\n", " ")[:70]
    print(f"  {mark} {label:<30} {status} {snippet}")

# POST form
status, payload = request(
    "POST", "/ask",
    {**auth, "Content-Type": "application/json"},
    json.dumps({"prompt": ""}),
)
print(f"  {'ok  ' if status == 400 else 'FAIL'} POST empty prompt              {status}")
failures += status != 400

proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()

print(f"\n{'all HTTP checks passed' if not failures else f'{failures} HTTP check(s) failed'}")
sys.exit(1 if failures else 0)
