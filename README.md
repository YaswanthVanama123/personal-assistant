# Jeeves

A personal assistant that lives on your Mac and can actually do things on it.

It reads and writes your files, searches your whole disk, drives your apps, reads
what's on your screen, manages your calendar and reminders, and reads and sends
WhatsApp and iMessage — asking first whenever an action leaves the Mac or
destroys something.

**It runs in two modes, and you choose per-command which one you use.**

```
$ jeeves local "read my messages"          ← no AI, no network, no cost
3 chat(s) with unread messages:
- Sarah — 2 messages
    are we still on for lunch?
- Family — 1 message
    dinner at 8?

$ jeeves local "reply to Sarah: yes, see you at one"

  send a WhatsApp message now — it leaves the Mac and cannot be recalled
  To:   Sarah
  Text: yes, see you at one

Go ahead? [y/N] y
Sent to Sarah: yes, see you at one
```

```
$ jeeves chat                              ← AI-driven, understands anything
you  find the invoice I downloaded last week and tell me the total
  ▸ find_files
  ▸ screen_text
Jeeves  Acme-Invoice-8821.pdf in Downloads, dated 2 August. Total £1,240.50,
        due 14 August.
```

## The two modes

| | `jeeves local` | `jeeves chat` / `voice` |
|---|---|---|
| Needs an AI | **No** | Yes — the `claude` binary |
| Needs a network | **No** | Yes |
| Needs an API key | No | No (uses your Claude Code sign-in) |
| Cost per request | **Nothing** | Your existing Claude subscription |
| Understands | 53 fixed phrasings | anything you say |
| Predictable | **Completely** | Usually |
| Confirmation | **A terminal prompt you answer** | The model asks you |

**Local mode is a deterministic command engine** — regular expressions plus
macOS, calling the same 70 tools. It works with `claude` uninstalled, on a plane,
at zero cost, and the same phrase always does exactly the same thing.

Its limit is honest: it only knows the phrasings in its table. It cannot
summarise, cannot compose a reply for you, and cannot work out what you meant.
**Dictation closes most of that gap** — when you say *"reply to Sarah: I'll be
ten minutes late"*, on-device speech-to-text already produced the exact words, so
no intelligence is needed to send them.

```bash
jeeves local --list          # every phrase it understands
jeeves local --voice         # fully offline voice assistant
jeeves local "battery level"
```

Add a phrasing by appending one line to `RULES` in `src/jeeves/local.py`.

### When the grammar misses

Local mode normalises what you say before matching — politeness stripped
("can you please", "hey jeeves"), trailing filler dropped ("now", "today"),
contractions tried both ways, missing apostrophes restored ("whats" → "what's"),
synonyms mapped ("launch" → "open"). So these all work despite not being in the
table verbatim:

```
Guess what time is it          →  It's 15:57 on Saturday 08 August.
Can you please open WhatsApp   →  reads your unread chats
What is the date today         →  Today is Saturday, 08 August 2026.
```

That is normalisation, not understanding, and it has a ceiling. For anything
past it, set an **intelligence fallback** — a model that handles whatever the
grammar could not:

```bash
brew install ollama && brew services start ollama
ollama pull qwen2.5:7b
```

then in `~/.config/jeeves/jeeves.toml`:

```toml
[brain]
fallback = "ollama"
```

Now the fast path stays fast and free, and anything unusual still gets answered:

```
"whats the time"                    →  grammar, instant, no model
"battery level"                     →  grammar, instant, no model
"which of my folders grew the most
 this week and why"                 →  grammar misses → local model, with tools
```

No account, no API key, no per-request cost, nothing leaves the Mac. `fallback =
"claude"` uses the agent runtime instead if you prefer.

**The local model is not given the gated tools.** It can read, search, open apps
and manage your calendar, but it cannot send a message, run a shell command or
delete anything — 61 of 70 tools, with the 9 destructive ones withheld. A 7B
model should not have that reach; `brain.allow_risky = true` changes it, and the
confirmation prompt still applies on top.

## Why it's built this way

**No Python packages.** Not "few" — none. Zero non-stdlib imports across 7,900
lines. No pip, no npm, no virtualenv. On a managed Mac where PyPI is firewalled,
this is the difference between working and not.

**Local mode has no AI dependency whatsoever.** Speech recognition, text-to-speech,
OCR, UI reading, calendar, contacts — all Apple frameworks already on your Mac.
Nothing leaves the machine.

**AI mode borrows the `claude` binary you already have.** Claude Code *is* the
Claude Agent SDK runtime, so the model loop, context management and
authentication come from a component that's already installed and signed in.
`claude` appears in exactly two places in the codebase; swapping it out is one
file, not a rewrite.

```
   jeeves local ──▶ local.py ────┐        no AI, no network
                   (53 regex     │
                    rules)       │
                                 ▼
   jeeves chat ──▶ agent.py ─▶ claude ─▶ registry.py — 70 tools
                              (the AI)   risk tiers · confirm gate · audit
                                 │
              ┌──────────────────┴───────────────────┐
              │                                      │
      osascript / CLI tools              jeeves-native (Swift)
      Notes  Mail  Messages              Speech · Vision · EventKit
      mdfind  pbcopy  screencapture      Contacts · Accessibility
      networksetup  pmset  say           menu bar · brightness
```

## Setup

```bash
cd ~/Documents/jeeves
bash scripts/install.sh
jeeves doctor
```

`install.sh` builds the Swift helper for your architecture, clears the AirDrop
quarantine flag if the folder came from another Mac, writes a config to
`~/.config/jeeves/`, and links `jeeves` onto your PATH.

Run it from **Terminal or iTerm** — not inside a Claude Code session, or the AI
runtime can't start nested.

### Requirements

| For | Need |
|---|---|
| Local mode, all 70 tools | Python 3.11+, Xcode command line tools |
| AI mode (`chat`, `voice`, `ask`) | `claude` installed and signed in |

Local mode needs nothing beyond macOS itself.

### macOS permissions

macOS prompts the first time Jeeves touches something private. Each prompt is
one-time; `jeeves doctor` shows what's outstanding and the exact pane to open.

| Permission | Needed for |
|---|---|
| Accessibility | WhatsApp, reading any app's UI, typing into apps |
| Automation | Notes, Mail, Messages, Music, Trash |
| Screen Recording | `screenshot`, `screen_text` |
| Calendars / Reminders / Contacts | agenda, reminders, contact lookup |
| Microphone + Speech Recognition | voice modes |

Fastest way to grant the native ones — just trigger each prompt:

```bash
./native/build/jeeves-native events 1
./native/build/jeeves-native reminders
./native/build/jeeves-native contacts YourName
./native/build/jeeves-native ui-dump Finder --max 3
```

## Using it

```bash
jeeves local "read my messages"     # no AI
jeeves local                        # interactive, no AI
jeeves local --voice                # offline voice assistant
jeeves local --list                 # every phrase local mode knows

jeeves                              # AI chat (the default)
jeeves ask "how much disk is left?"
jeeves voice                        # AI voice, until you say "stop"
jeeves menubar                      # 🤵 in the menu bar
jeeves serve                        # local HTTP API on 127.0.0.1:8787

jeeves tools                        # all 70 tools, grouped by risk
jeeves policy                       # what asks permission
jeeves audit -n 40                  # what Jeeves has actually done
jeeves memory --search sarah        # what Jeeves remembers
jeeves doctor                       # check everything
```

Inside AI chat: `/help`, `/new`, `/memory`, `/audit`, `/policy`, `/voice`,
`/verbose`, `/quit`.

### From Shortcuts, Raycast or your phone

```bash
jeeves serve
```

POST to `/ask` with the bearer token printed on startup (stored in
`~/.local/state/jeeves/api-token`). Shortcuts can't set headers easily, so
`?token=…` works too:

```
http://127.0.0.1:8787/ask?token=YOUR_TOKEN&prompt=what%20is%20my%20battery%20level
```

`session=kitchen` keeps a named conversation going. `GET /health`, `/memory`,
`/audit` are also available.

## What it can do

70 tools. `jeeves tools` lists them all.

- **WhatsApp** — unread summary, list chats, read a conversation, send a message.
  Driven through the Accessibility API, so exact UI text rather than OCR.
- **Files** — Spotlight search by name or content, list, read, write, move, copy,
  Trash, disk usage, what changed recently
- **Apps** — launch, quit, focus, force-quit, open URLs and files, reveal in Finder
- **Any app's UI** — read visible text from apps with no AppleScript support
  (Slack, Discord, Electron), inspect the tree, type into the frontmost window
- **Screen** — screenshot display/window/selection, OCR it, read the selection
- **Calendar & Reminders** — agenda, free slots, create events, add and complete
  reminders
- **Notes, Mail, iMessage** — search and create notes; read and search mail;
  draft or send email; send iMessage
- **System** — volume, mute, battery, Wi-Fi, network, brightness, Focus modes,
  notifications, sleep, restart
- **Contacts** — look someone up before messaging them
- **Shell** — gated by an allow/deny policy
- **Memory** — remember and recall across sessions
- **Web** — search and fetch (AI mode only)

## Safety

Three independent layers, ordered so the weakest is never load-bearing.

**1. Hard refusals — nobody can override these.**
`rm -rf`, `sudo`, `dd`, `diskutil`, `mkfs`, `launchctl`, `csrutil`, `security`,
`killall`, piping a download into a shell, fork bombs, reading `~/.ssh`,
`~/.aws`, `~/.gnupg`, keychains, `/etc/passwd`, any `*.pem`. Enforced in the
tools and again by the runtime's deny rules. Confirming doesn't help.

**2. Risk tiers — what needs your agreement.**

| Tier | Examples | Behaviour |
|---|---|---|
| Read (33) | search, read, agenda, OCR, unread messages | runs immediately |
| Reversible (28) | volume, open app, create note, write file, Trash | runs immediately |
| Gated (9) | send WhatsApp/iMessage/mail, shell, permanent delete, restart | asks first |

`safety.mode = "strict"` gates reversible changes too; `"open"` gates nothing
(layer 1 still applies).

**3. Reversibility and audit.**
Deletions go to the Trash. Overwrites leave a `.jeeves-backup`. Every change is
written to `~/.local/state/jeeves/audit.jsonl` with how to undo it.

**Local mode's gate is the stronger one.** It prints what will happen and waits
for you to type `y` at your own terminal — a real human decision. In AI mode the
gate is model-mediated, which makes it a usability feature rather than a security
boundary: a model that set `confirm=true` on its own could send something you
didn't approve. That's exactly why the irreversible things live in layer 1, where
no model has a vote.

### A specific warning about WhatsApp

WhatsApp's terms prohibit automated clients and bulk messaging. Reading your own
chats on your own desktop is a grey area; **unattended auto-replies are the exact
pattern that gets numbers banned.** There is deliberately no "reply to
everything" tool, and every send is gated. Keep it to messages you personally
approve.

WhatsApp also has no AppleScript API, so these tools read its Accessibility tree.
An update that moves things can break them — the tools say so and point you at
`ui_inspect`, which prints the tree so a rule can be re-targeted.

## Layout

```
bin/jeeves                 launcher (finds a suitable python, sets PYTHONPATH)
config/jeeves.toml         documented defaults
native/main.swift          Speech, Vision, EventKit, Contacts, menu bar
native/build/Jeeves.app    the signed app bundle TCC reads permissions from
native/Accessibility.swift UI reading and driving (WhatsApp, any app)
scripts/install.sh         build + link + config
scripts/build_native.sh    compile and sign for this architecture
scripts/test.sh            run the suites
src/jeeves/
  local.py                 the no-AI command grammar
  agent.py                 drives the claude runtime over stream-JSON
  prompt.py                the system prompt
  policy.py                runtime allow/deny rules
  memory.py                SQLite: facts, history, audit
  mac.py                   injection-safe osascript / shell bridge
  tui.py voice.py server.py doctor.py cli.py
  mcp/
    protocol.py            JSON-RPC 2.0 over stdio
    registry.py            tool declaration, risk tiers, confirm gate
    tools/                 system apps files screen pim comms shell whatsapp
tests/                     units, shell policy, local grammar, MCP, HTTP
```

Config in `~/.config/jeeves/jeeves.toml`; state in `~/.local/state/jeeves/`.
Everything is inspectable:

```bash
sqlite3 ~/.local/state/jeeves/jeeves.db 'select * from audit order by ts desc limit 10'
tail -f ~/.local/state/jeeves/audit.jsonl
tail -f ~/.local/state/jeeves/agent.log    # the AI runtime's own output
```

## Extending it

**A new tool** is one decorated function. It gets argument validation, the
confirm gate and audit logging for free, and is immediately available to both
modes:

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

**A new local phrasing** is one line:

```python
# src/jeeves/local.py, in RULES
rule(rf"^{P}set\s+(?:the\s+)?wallpaper\s+to\s+(?P<path>.+)$",
     "wallpaper_set", "set wallpaper to ~/Pictures/hills.jpg", build=_clean_groups),
```

Three rules worth knowing, all learned the hard way:

- User data goes through `argv`, never into script text. `osascript`, `tell()`
  and `tell_literal()` all take arguments, so a filename with quotes or newlines
  can't alter the program.
- `tell()` passes the app name as a variable, so AppleScript can't resolve
  app-specific terminology. Use `tell_literal()` for words a particular app
  defines (Finder's `trash`, Music's `current track`); `tell()` is only for
  `quit`, `launch`, `activate`, `open`.
- In `local.py`, **rule order matters** — put specific phrasings before general
  ones, or `volume up` gets swallowed by `volume <level>`.

## Troubleshooting

**"nested sandboxes"** — you're in a Claude Code session. Use Terminal.

**A tool says macOS denied it** — `jeeves doctor` names the permission and pane.
Screen Recording and Accessibility need the terminal restarted after granting.

**WhatsApp tools find nothing** — is it open and signed in? Then
`jeeves local "..."` won't help; run
`./native/build/jeeves-native ui-dump WhatsApp --roles` and re-target the rule.

**Local mode says "I don't have a rule for that"** — it lists the closest
phrasings it does know. `jeeves local --list` shows all 53.

**Voice hears nothing** — check Microphone and Speech Recognition. Raise
`voice.silence_timeout` if it cuts you off mid-sentence.

**AI mode is slow** — set `agent.effort = "medium"` or `agent.model = "sonnet"`.

## Tests

```bash
bash scripts/test.sh
```

Four suites, no network and no permissions required: tool schemas and API auth,
the shell safety classifier (62 cases), the local grammar (62 cases), and the MCP
protocol over real stdio. `--all` adds the HTTP API, which needs to bind a local
socket.
