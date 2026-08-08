# Jeeves

A personal assistant that lives on your Mac and can actually do things on it.

Ask it in the terminal, out loud, from the menu bar, or over HTTP. It reads and
writes your files, searches your whole disk, drives your apps, reads what's on
your screen, manages your calendar and reminders, and sends messages — asking
first whenever an action leaves the Mac or destroys something.

```
you  what's on my calendar tomorrow, and am I free for lunch?
  ▸ calendar_agenda
  ▸ calendar_free_slots
Jeeves  Three things tomorrow: standup at 9:30, a design review 11–12, and
        Priya's 1:1 at 16:00. You're free 12:00–16:00, so lunch is wide open.

you  find the invoice PDF I downloaded last week and tell me the total
  ▸ find_files
  ▸ screen_text
Jeeves  Found Acme-Invoice-8821.pdf in Downloads, dated 2 August. Total is
        £1,240.50, due 14 August.
```

## Why it's built this way

**No dependencies.** Not "few" — none. No pip, no npm, no virtualenv. Everything
is Python's standard library plus what macOS already ships. This is deliberate: a
personal assistant you have to repair every few months isn't useful, and on a
managed Mac you may not be able to install packages at all.

**The brain is the `claude` binary you already have.** Claude Code *is* the
Claude Agent SDK runtime. Jeeves drives it in headless mode over its stream-JSON
protocol, which means the model loop, context management, prompt caching and
authentication are all handled by a component that's already installed, already
signed in, and maintained by someone else. Jeeves supplies the tools, the
persona and the safety policy.

**The hands are native.** Speech recognition is on-device `SFSpeechRecognizer`;
screen reading is the Vision framework; calendar and reminders are EventKit;
contacts are the Contacts framework. That's one small Swift binary instead of a
gigabyte of Python ML wheels — and your voice never leaves the machine.

```
       you ──▶ chat / voice / menu bar / HTTP
                        │
                   agent.py ──── spawns ───▶ claude (agent runtime, your auth)
                                                    │
                                     MCP over stdio │ 63 tools
                                                    ▼
                              ┌─────────────────────┴──────────────────┐
                              │  registry.py — risk tiers, confirm     │
                              │               gate, audit log          │
                              └─────────────────────┬──────────────────┘
                                                    │
                    osascript ── CLI tools ── jeeves-native (Swift)
                    Notes         mdfind        Speech · Vision
                    Mail          pbcopy        EventKit · Contacts
                    Messages      screencapture menu bar · brightness
                    Music         networksetup
```

## Setup

```bash
cd ~/Documents/jeeves
bash scripts/install.sh
jeeves doctor
```

`install.sh` builds the Swift helper, writes a config file to
`~/.config/jeeves/`, and links `jeeves` onto your PATH. `doctor` then checks
every moving part and tells you exactly which macOS permission to grant, and
where, for anything that isn't working yet.

Run it from **Terminal or iTerm**, not from inside a Claude Code session — the
agent runtime can't start nested inside another one.

### macOS permissions

macOS will prompt the first time Jeeves touches something private. Each prompt
is one-time. `jeeves doctor` shows what's still outstanding.

| Permission | Needed for | Granted to |
|---|---|---|
| Automation | Notes, Mail, Messages, Music, Trash | your terminal |
| Screen Recording | `screenshot`, `screen_text` | your terminal |
| Calendars / Reminders / Contacts | agenda, reminders, contact lookup | `jeeves-native` |
| Microphone + Speech Recognition | voice mode | `jeeves-native` |
| Accessibility | reading the selection, typing into apps | your terminal |

Nothing here is required to start. Without Automation you lose Notes and Mail;
everything else still works.

## Using it

```bash
jeeves                              # terminal chat (the default)
jeeves ask "how much disk is left?" # one question, print the answer, exit
jeeves ask --speak "summarise my unread mail"
jeeves voice                        # converse out loud until you say "stop"
jeeves voice --once                 # answer one spoken request
jeeves menubar                      # 🤵 in the menu bar
jeeves serve                        # local HTTP API on 127.0.0.1:8787
```

Inside the chat: `/help`, `/new`, `/memory`, `/audit`, `/policy`, `/voice`,
`/verbose`, `/quit`.

```bash
jeeves tools                        # every tool, grouped by risk
jeeves policy                       # what asks permission and what doesn't
jeeves audit -n 40                  # what Jeeves has actually done
jeeves memory --search sarah        # what Jeeves remembers
jeeves memory --add "I prefer 24-hour time"
```

### From Shortcuts, Raycast or your phone

```bash
jeeves serve
```

Then POST to `/ask` with a bearer token (printed on startup, stored in
`~/.local/state/jeeves/api-token`). Shortcuts can't set headers easily, so
`?token=…` works too:

```
http://127.0.0.1:8787/ask?token=YOUR_TOKEN&prompt=what%20is%20my%20battery%20level
```

Pass `session=kitchen` to keep a named conversation going across requests.
`GET /health`, `/memory`, `/audit` are also available.

## What it can do

63 tools. `jeeves tools` lists them all with descriptions.

- **Files** — Spotlight search by name or content, list, read, write, move,
  copy, Trash, disk usage, what changed recently
- **Apps** — launch, quit, focus, force-quit, open URLs and files, reveal in Finder
- **Screen** — screenshot the display, a window or a selection; OCR it; read the
  current selection; type into the frontmost app
- **Calendar & Reminders** — agenda, find free slots, create events, list, add
  and complete reminders
- **Notes, Mail, Messages** — search and create notes; read and search mail;
  draft or send email; send iMessage
- **System** — volume, mute, battery, Wi-Fi, network, brightness, displays,
  Focus modes via Shortcuts, notifications, sleep, restart
- **Contacts** — look someone up before messaging them
- **Shell** — run commands, gated by an allow/deny policy
- **Memory** — remember and recall things across sessions
- **Web** — search and fetch, via the runtime's built-in tools

## Safety

Guarded autonomy, in three independent layers. The order matters: the weakest
layer is the one the model participates in, so it never has to be load-bearing.

**1. Hard refusals — cannot be overridden by anyone.**
`rm -rf`, `sudo`, `dd`, `diskutil`, `mkfs`, `launchctl`, `csrutil`, `security`,
`killall`, piping a download into a shell, fork bombs, reading `~/.ssh`,
`~/.aws`, `~/.gnupg`, keychains, `/etc/passwd` or any `*.pem`. Enforced inside
the tools and again by the runtime's own deny rules. Confirming doesn't help;
`--dangerously` flags don't exist.

**2. Risk tiers — what needs your agreement.**

| Tier | Examples | Behaviour |
|---|---|---|
| Read (28 tools) | search, read, agenda, OCR, recall | runs immediately |
| Reversible (28) | volume, open app, create note, write file, Trash | runs immediately |
| Gated (7) | send iMessage, send mail, shell, permanent delete, restart | asks first |

A gated tool called without approval performs no action. It returns a
description of what *would* happen, which Jeeves has to relay to you and get a
yes for before retrying. `jeeves policy` prints the current tiers; set
`safety.mode = "strict"` to gate reversible changes too, or `"open"` to gate
nothing (layer 1 still applies).

**3. Reversibility and audit.**
Deletions go to the Trash, not `unlink`. Overwrites leave a `.jeeves-backup`.
Every call that changes anything is written to
`~/.local/state/jeeves/audit.jsonl` with its arguments and how to undo it —
`jeeves audit` reads it back.

**Being straight about layer 2:** the confirm gate is mediated by the model, so
it's a usability feature, not a security boundary. A model that decided to set
`confirm=true` on its own could send a message you didn't approve. That's why
the things you genuinely can't take back live in layer 1, where the model has no
say, and why everything is reversible and logged.

The HTTP API binds to localhost and needs a token compared in constant time. If
you bind it to `0.0.0.0`, that token is all that stands between your Mac and
your network — it warns you when you do.

## Layout

```
bin/jeeves                 launcher (finds a suitable python, sets PYTHONPATH)
config/jeeves.toml         documented defaults
native/Jeeves.swift        Speech, Vision, EventKit, Contacts, menu bar
scripts/install.sh         build + link + config
scripts/build_native.sh    compile and sign the Swift helper
scripts/test.sh            run the suites
src/jeeves/
  agent.py                 drives the claude runtime over stream-JSON
  prompt.py                the system prompt
  policy.py                runtime allow/deny rules
  memory.py                SQLite: facts, history, audit
  mac.py                   injection-safe osascript / shell bridge
  tui.py voice.py server.py doctor.py cli.py
  mcp/
    protocol.py            JSON-RPC 2.0 over stdio
    registry.py            tool declaration, risk tiers, confirm gate
    tools/                 system apps files screen pim comms shell
tests/                     unit, shell policy, MCP protocol, HTTP API
```

Config lives in `~/.config/jeeves/jeeves.toml`; state in
`~/.local/state/jeeves/` (database, audit log, runtime log, API token).

Everything is inspectable:

```bash
sqlite3 ~/.local/state/jeeves/jeeves.db 'select * from audit order by ts desc limit 10'
tail -f ~/.local/state/jeeves/audit.jsonl
tail -f ~/.local/state/jeeves/agent.log    # the runtime's own output
```

## Extending it

Adding a tool is one decorated function. It appears in the catalogue, gets
argument validation, the confirm gate and audit logging for free:

```python
# src/jeeves/mcp/tools/system.py
@tool(
    "wallpaper_set",
    "Change the desktop wallpaper.",
    {"path": string("Path to an image file.")},
    required=["path"],
    risk=WRITE,                       # READ | WRITE | RISKY
    undo="wallpaper_set with the previous image",
    needs_automation=True,
)
def wallpaper_set(path: str) -> str:
    image = check_path(path)          # rejects protected locations
    result = tell_literal("System Events",
                          "tell every desktop to set picture to (item 1 of argv)",
                          str(image))
    if not result.ok:
        raise ToolError(f"could not set the wallpaper: {result.err}")
    return f"Wallpaper set to {image.name}"
```

Two rules worth knowing, both learned the hard way:

- User data goes through `argv`, never into the script text. `osascript`,
  `tell()` and `tell_literal()` all take arguments, so a filename containing
  quotes or newlines can't alter the program.
- `tell()` passes the app name as a variable, which means AppleScript can't
  resolve app-specific terminology. Use `tell_literal()` for anything using
  words a particular app defines (Finder's `trash`, Music's `current track`);
  `tell()` is only for `quit`, `launch`, `activate`, `open`.

## Troubleshooting

**"the agent runtime could not start … nested sandboxes"** — you're running
inside a Claude Code session. Use a normal Terminal window.

**A tool says it was denied by macOS** — run `jeeves doctor`; it names the
permission and the exact Settings pane. Screen Recording and Accessibility need
the terminal restarted after granting.

**Voice hears nothing** — check Microphone and Speech Recognition for
`jeeves-native`. Increase `voice.silence_timeout` if it cuts you off mid-sentence.

**Replies are silent** — your configured `tts_voice` isn't installed; Jeeves
falls back to the system voice. `jeeves doctor` lists what you have.

**Everything is slow** — drop `agent.effort` to `"medium"` or `"low"`, or set
`agent.model = "sonnet"`.

## Requirements

macOS 14+ (built and tested on macOS 26, Apple silicon), Python 3.11+, Xcode
command line tools for the Swift helper, and Claude Code signed in.
