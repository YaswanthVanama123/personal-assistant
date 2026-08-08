#!/bin/bash
# Set Jeeves up: build the native helper, create the config, put `jeeves` on PATH.
# Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "Jeeves setup"
echo "============"
echo

# ------------------------------------------------- transferred-copy hygiene
# A folder that arrived by AirDrop, zip or a USB stick carries three problems:
# a quarantine flag that Gatekeeper uses to block the Swift helper, lost
# executable bits, and bytecode plus a binary built for another machine.
if xattr -pr com.apple.quarantine . >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine . 2>/dev/null || true
  echo "✓ cleared the quarantine flag (this copy arrived from another Mac)"
fi

chmod +x bin/jeeves scripts/*.sh 2>/dev/null || true

if find . -name '__pycache__' -type d -print -quit 2>/dev/null | grep -q .; then
  find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  echo "✓ removed bytecode cached by another Python"
fi

if [[ -f native/build/jeeves-native ]]; then
  built_arch="$(lipo -archs native/build/jeeves-native 2>/dev/null || echo unknown)"
  if [[ "$built_arch" != *"$(uname -m)"* ]]; then
    rm -f native/build/jeeves-native
    echo "✓ discarded the helper built for $built_arch (this Mac is $(uname -m))"
  fi
fi
echo

# ---------------------------------------------------------------- python
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys,tomllib; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "✗ Python 3.11+ is required and was not found."
  echo "  Install it with: brew install python@3.13"
  exit 1
fi
echo "✓ python: $("$PYTHON" --version) at $(command -v "$PYTHON")"

# ---------------------------------------------------------------- runtime
if command -v claude >/dev/null 2>&1; then
  echo "✓ agent runtime: $(command -v claude)"
else
  echo "✗ the 'claude' command was not found."
  echo "  Jeeves uses Claude Code as its agent runtime. Install it, then re-run."
  exit 1
fi

# ---------------------------------------------------------------- native
echo
if ! command -v swiftc >/dev/null 2>&1; then
  echo "! swiftc not found, so the native helper cannot be built."
  echo "  Install the Xcode command line tools:  xcode-select --install"
  echo "  Then re-run this script. Chat still works without it; voice, OCR,"
  echo "  calendar, reminders and contacts do not."
else
  echo "Building the native helper (voice, OCR, calendar, reminders, contacts)…"
  if bash scripts/build_native.sh >/tmp/jeeves-build.log 2>&1; then
    echo "✓ native helper built for $(uname -m)"
  else
    echo "! native helper failed to build — voice, OCR, calendar, reminders and"
    echo "  contacts will be unavailable. Last lines of /tmp/jeeves-build.log:"
    tail -5 /tmp/jeeves-build.log | sed 's/^/    /'
  fi
fi

# ---------------------------------------------------------------- config
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jeeves"
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/jeeves.toml" ]]; then
  cat > "$CONFIG_DIR/jeeves.toml" <<'TOML'
# Your personal Jeeves settings. Overrides config/jeeves.toml in the repo.
# See that file for every available option, with comments.

[agent]
# model = "opus"

[voice]
# tts_voice = "Samantha"

[safety]
mode = "guarded"
TOML
  echo "✓ created $CONFIG_DIR/jeeves.toml"
else
  echo "✓ config already exists at $CONFIG_DIR/jeeves.toml"
fi

# ---------------------------------------------------------------- PATH
LINK_DIR=""
for candidate in "$HOME/.local/bin" "/usr/local/bin" "$HOME/bin"; do
  if [[ -d "$candidate" && -w "$candidate" ]]; then LINK_DIR="$candidate"; break; fi
done
if [[ -z "$LINK_DIR" ]]; then
  mkdir -p "$HOME/.local/bin" && LINK_DIR="$HOME/.local/bin"
fi

ln -sf "$ROOT/bin/jeeves" "$LINK_DIR/jeeves"
echo "✓ linked $LINK_DIR/jeeves -> $ROOT/bin/jeeves"

if ! printf '%s' ":$PATH:" | grep -q ":$LINK_DIR:"; then
  echo
  echo "! $LINK_DIR is not on your PATH. Add this to ~/.zshrc:"
  echo "    export PATH=\"$LINK_DIR:\$PATH\""
fi

# ---------------------------------------------------------------- done
# ------------------------------------------------------- optional local brain
echo
if command -v ollama >/dev/null 2>&1; then
  echo "✓ ollama found — local mode can fall back to a local model"
  echo "  Enable it: set brain.fallback = \"ollama\" in $CONFIG_DIR/jeeves.toml"
else
  echo "· ollama not installed (optional)"
  echo "  Without it, local mode only understands its 58 fixed phrasings."
  echo "  For real understanding on-device — no account, no API key, no cost:"
  echo "      brew install ollama && brew services start ollama"
  echo "      ollama pull qwen2.5:7b"
  echo "  then set brain.fallback = \"ollama\" in $CONFIG_DIR/jeeves.toml"
fi

echo
echo "Setup complete. Next:"
echo
echo "  jeeves doctor      check permissions and wiring"
echo "  jeeves chat        talk to Jeeves in the terminal"
echo "  jeeves voice       talk to Jeeves out loud"
echo
echo "macOS will ask permission the first time Jeeves reads your calendar,"
echo "listens to you, reads the screen or drives an app. Approving each prompt"
echo "once is enough; 'jeeves doctor' shows what is still missing."
