#!/bin/bash
# Run the Jeeves test suite. No network, no permissions needed for the first two.
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON="${JEEVES_PYTHON:-python3}"
fails=0

run() {
  local label="$1"; shift
  echo
  echo "─── $label ───"
  if "$@"; then
    echo "PASS: $label"
  else
    echo "FAIL: $label"
    fails=$((fails + 1))
  fi
}

run "unit checks (schemas, auth, prompt, speech shaping)" \
    "$PYTHON" tests/test_units.py
run "shell safety classifier" \
    "$PYTHON" tests/test_shell_policy.py
run "local mode grammar (no AI)" \
    "$PYTHON" tests/test_local_grammar.py
run "MCP protocol over stdio" \
    "$PYTHON" tests/test_mcp_protocol.py

if [[ "${1:-}" == "--all" ]]; then
  # Needs to bind a local socket; some managed Macs deny that.
  run "local HTTP API" "$PYTHON" tests/test_http_api.py
fi

echo
if [[ $fails -eq 0 ]]; then
  echo "All suites passed."
else
  echo "$fails suite(s) failed."
fi
exit $fails
