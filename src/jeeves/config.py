"""Configuration and filesystem layout.

Config resolution order (later wins):
  1. built-in defaults below
  2. <repo>/config/jeeves.toml
  3. ~/.config/jeeves/jeeves.toml
  4. JEEVES_* environment variables
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- locations

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

HOME = Path.home()
STATE_DIR = Path(os.environ.get("JEEVES_STATE_DIR", HOME / ".local/state/jeeves"))
CONFIG_DIR = Path(os.environ.get("JEEVES_CONFIG_DIR", HOME / ".config/jeeves"))
CACHE_DIR = Path(os.environ.get("JEEVES_CACHE_DIR", HOME / ".cache/jeeves"))

DB_PATH = STATE_DIR / "jeeves.db"
AUDIT_LOG = STATE_DIR / "audit.jsonl"
AGENT_LOG = STATE_DIR / "agent.log"
SCREENSHOT_DIR = CACHE_DIR / "screenshots"

# The helper lives inside an app bundle: TCC only reads usage-description strings
# from a real Contents/Info.plist for some services (Speech Recognition among
# them), and kills the process without one. The bare path is kept as a fallback
# for older builds.
_NATIVE_APP = REPO_ROOT / "native" / "build" / "Jeeves.app" / "Contents" / "MacOS" / "jeeves-native"
_NATIVE_BARE = REPO_ROOT / "native" / "build" / "jeeves-native"
NATIVE_BIN = _NATIVE_APP if _NATIVE_APP.exists() else _NATIVE_BARE

DEFAULTS: dict[str, Any] = {
    "agent": {
        # Model alias or full name. "" => whatever the CLI is configured to use.
        "model": "",
        "effort": "high",
        # Seconds to wait for a single turn to complete before giving up.
        "turn_timeout": 900,
        "workdir": str(HOME),
        # Extra directories the agent may touch, beyond workdir.
        "extra_dirs": [str(HOME / "Documents"), str(HOME / "Desktop"), str(HOME / "Downloads")],
    },
    "voice": {
        # Empty string = the macOS system voice. Set a name from `say -v '?'`
        # (e.g. "Samantha", or a downloaded premium voice) to override.
        # An unavailable voice silently falls back to the system default.
        "tts_voice": "",
        "tts_rate": 190,
        "wake_word": "jeeves",
        # Seconds of silence that ends an utterance.
        "silence_timeout": 1.4,
        # Speak replies out loud in voice mode.
        "speak": True,
        # Trim spoken replies to this many characters (full text still shown).
        "speak_limit": 700,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8787,
        # Shared secret for the local HTTP API. Empty => generated on first run.
        "token": "",
    },
    "safety": {
        # "guarded" | "strict" | "open"
        #   guarded: reads freely, safe writes freely, confirms risky actions
        #   strict:  confirms every mutating action
        #   open:    no confirmation gates (still honours the hard deny list)
        "mode": "guarded",
        # Send file deletions to Trash instead of unlinking.
        "trash_not_delete": True,
        # Paths the assistant must never read or write, in addition to built-ins.
        "forbidden_paths": [],
    },
}

_ENV_MAP = {
    "JEEVES_MODEL": ("agent", "model", str),
    "JEEVES_EFFORT": ("agent", "effort", str),
    "JEEVES_WORKDIR": ("agent", "workdir", str),
    "JEEVES_TTS_VOICE": ("voice", "tts_voice", str),
    "JEEVES_WAKE_WORD": ("voice", "wake_word", str),
    "JEEVES_PORT": ("server", "port", int),
    "JEEVES_TOKEN": ("server", "token", str),
    "JEEVES_SAFETY": ("safety", "mode", str),
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:  # pragma: no cover
        print(f"jeeves: ignoring bad config {path}: {exc}")
        return {}


class Config:
    """Dotted-path read-only view over the merged config."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, dotted: str) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            raise KeyError(dotted)
        return value

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


_MISSING = object()
_cached: Config | None = None


def load(reload: bool = False) -> Config:
    global _cached
    if _cached is not None and not reload:
        return _cached

    data = copy.deepcopy(DEFAULTS)
    _deep_merge(data, _read_toml(REPO_ROOT / "config" / "jeeves.toml"))
    _deep_merge(data, _read_toml(CONFIG_DIR / "jeeves.toml"))

    for env_key, (section, key, caster) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw:
            try:
                data[section][key] = caster(raw)
            except ValueError:
                print(f"jeeves: ignoring invalid {env_key}={raw!r}")

    _cached = Config(data)
    return _cached


def ensure_dirs() -> None:
    for directory in (STATE_DIR, CONFIG_DIR, CACHE_DIR, SCREENSHOT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def server_token() -> str:
    """Return the local API token, generating and persisting one if needed."""
    configured = load().get("server.token") or ""
    if configured:
        return configured

    ensure_dirs()
    token_file = STATE_DIR / "api-token"
    if token_file.exists():
        return token_file.read_text().strip()

    import secrets

    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    return token
