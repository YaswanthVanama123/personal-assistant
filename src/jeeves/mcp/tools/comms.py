"""Notes, Mail, Messages and music playback.

These apps have no public framework equivalent, so they are driven with
AppleScript. Every script is an ``on run argv`` handler and all user data arrives
through ``argv``, never through string interpolation.
"""

from __future__ import annotations

from ...mac import Result, osascript, tell_literal
from ..registry import READ, RISKY, WRITE, enum, integer, string, tool


def _need_automation(result: Result, app: str) -> None:
    if not result.ok:
        raise PermissionError(
            f"could not talk to {app}: {result.err or 'no response'}. Grant your "
            f"terminal permission to control {app} under System Settings → "
            "Privacy & Security → Automation, then try again."
        )


# --------------------------------------------------------------------- Notes


@tool(
    "notes_search",
    "Search Apple Notes by title and body text.",
    {
        "query": string("Words to look for."),
        "limit": integer("Maximum notes to return, 1-25.", 1, 25),
    },
    required=["query"],
    risk=READ,
    needs_automation=True,
)
def notes_search(query: str, limit: int = 8) -> str:
    script = (
        "on run argv\n"
        "  set q to item 1 of argv\n"
        "  set cap to (item 2 of argv) as integer\n"
        "  set out to {}\n"
        '  tell application "Notes"\n'
        "    set hits to (every note whose name contains q or plaintext contains q)\n"
        "    repeat with n in hits\n"
        "      if (count of out) ≥ cap then exit repeat\n"
        "      set body to plaintext of n\n"
        "      if (length of body) > 400 then set body to (text 1 thru 400 of body)\n"
        '      set end of out to (name of n) & "\\n" & body\n'
        "    end repeat\n"
        "  end tell\n"
        '  return my join(out, "\\n---\\n")\n'
        "end run\n"
        "on join(lst, sep)\n"
        "  set {tid, AppleScript's text item delimiters} to {AppleScript's text item delimiters, sep}\n"
        "  set s to lst as text\n"
        "  set AppleScript's text item delimiters to tid\n"
        "  return s\n"
        "end join\n"
    )
    result = osascript(script, query, str(limit), timeout=90)
    _need_automation(result, "Notes")
    return result.out or f"No notes matched {query!r}."


@tool(
    "notes_create",
    "Create a new note in Apple Notes.",
    {
        "title": string("Note title."),
        "body": string("Note body text."),
        "folder": string("Optional folder name; defaults to the first folder."),
    },
    required=["title", "body"],
    risk=WRITE,
    undo="delete the note in Notes.app",
    needs_automation=True,
)
def notes_create(title: str, body: str, folder: str = "") -> str:
    script = (
        "on run argv\n"
        "  set t to item 1 of argv\n"
        "  set b to item 2 of argv\n"
        "  set f to item 3 of argv\n"
        '  tell application "Notes"\n'
        '    set html to "<div><h1>" & t & "</h1><div>" & b & "</div></div>"\n'
        '    if f is "" then\n'
        "      make new note at folder 1 of default account with properties {body:html}\n"
        "    else\n"
        "      make new note at folder f of default account with properties {body:html}\n"
        "    end if\n"
        "  end tell\n"
        '  return "ok"\n'
        "end run\n"
    )
    result = osascript(script, title, body, folder, timeout=45)
    _need_automation(result, "Notes")
    return f"Created note “{title}”" + (f" in folder {folder}." if folder else ".")


# ---------------------------------------------------------------------- Mail


@tool(
    "mail_unread",
    "Summarise unread messages in Mail: sender, subject and date.",
    {"limit": integer("Maximum messages, 1-40.", 1, 40)},
    risk=READ,
    needs_automation=True,
)
def mail_unread(limit: int = 15) -> str:
    script = (
        "on run argv\n"
        "  set cap to (item 1 of argv) as integer\n"
        "  set out to {}\n"
        '  tell application "Mail"\n'
        "    repeat with box in (every mailbox of every account)\n"
        "      try\n"
        "        set msgs to (messages of box whose read status is false)\n"
        "        repeat with m in msgs\n"
        "          if (count of out) ≥ cap then exit repeat\n"
        "          set end of out to ((date received of m) as text) & \"  |  \" & "
        "(sender of m) & \"  |  \" & (subject of m)\n"
        "        end repeat\n"
        "      end try\n"
        "      if (count of out) ≥ cap then exit repeat\n"
        "    end repeat\n"
        "  end tell\n"
        '  return my join(out, "\\n")\n'
        "end run\n"
        "on join(lst, sep)\n"
        "  set {tid, AppleScript's text item delimiters} to {AppleScript's text item delimiters, sep}\n"
        "  set s to lst as text\n"
        "  set AppleScript's text item delimiters to tid\n"
        "  return s\n"
        "end join\n"
    )
    result = osascript(script, str(limit), timeout=120)
    _need_automation(result, "Mail")
    return result.out or "No unread mail."


@tool(
    "mail_search",
    "Search Mail by sender, subject or body text.",
    {
        "query": string("Text to search for."),
        "field": enum("Which field to search.", ["subject", "sender", "content"]),
        "limit": integer("Maximum messages, 1-30.", 1, 30),
    },
    required=["query"],
    risk=READ,
    needs_automation=True,
)
def mail_search(query: str, field: str = "subject", limit: int = 12) -> str:
    predicate = {
        "subject": "subject contains q",
        "sender": "sender contains q",
        "content": "content contains q",
    }[field]
    script = (
        "on run argv\n"
        "  set q to item 1 of argv\n"
        "  set cap to (item 2 of argv) as integer\n"
        "  set out to {}\n"
        '  tell application "Mail"\n'
        "    repeat with box in (every mailbox of every account)\n"
        "      try\n"
        f"        set msgs to (messages of box whose {predicate})\n"
        "        repeat with m in msgs\n"
        "          if (count of out) ≥ cap then exit repeat\n"
        "          set end of out to ((date received of m) as text) & \"  |  \" & "
        "(sender of m) & \"  |  \" & (subject of m)\n"
        "        end repeat\n"
        "      end try\n"
        "      if (count of out) ≥ cap then exit repeat\n"
        "    end repeat\n"
        "  end tell\n"
        '  return my join(out, "\\n")\n'
        "end run\n"
        "on join(lst, sep)\n"
        "  set {tid, AppleScript's text item delimiters} to {AppleScript's text item delimiters, sep}\n"
        "  set s to lst as text\n"
        "  set AppleScript's text item delimiters to tid\n"
        "  return s\n"
        "end join\n"
    )
    result = osascript(script, query, str(limit), timeout=150)
    _need_automation(result, "Mail")
    return result.out or f"No mail matched {query!r} in {field}."


@tool(
    "mail_draft",
    "Compose an email and leave it OPEN as a draft for the user to review and "
    "send themselves. Nothing is sent. Prefer this over mail_send.",
    {
        "to": string("Recipient address."),
        "subject": string("Subject line."),
        "body": string("Message body."),
        "cc": string("Optional CC address."),
    },
    required=["to", "subject", "body"],
    risk=WRITE,
    undo="close the draft window without sending",
    needs_automation=True,
)
def mail_draft(to: str, subject: str, body: str, cc: str = "") -> str:
    script = (
        "on run argv\n"
        "  set rcpt to item 1 of argv\n"
        "  set subj to item 2 of argv\n"
        "  set bdy to item 3 of argv\n"
        "  set ccAddr to item 4 of argv\n"
        '  tell application "Mail"\n'
        "    set msg to make new outgoing message with properties "
        "{subject:subj, content:bdy, visible:true}\n"
        "    tell msg\n"
        "      make new to recipient at end of to recipients with properties {address:rcpt}\n"
        '      if ccAddr is not "" then make new cc recipient at end of cc recipients '
        "with properties {address:ccAddr}\n"
        "    end tell\n"
        "    activate\n"
        "  end tell\n"
        '  return "ok"\n'
        "end run\n"
    )
    result = osascript(script, to, subject, body, cc, timeout=45)
    _need_automation(result, "Mail")
    return (
        f"Opened a draft to {to} — “{subject}”. It is NOT sent; the user can "
        "review and send it from Mail."
    )


@tool(
    "mail_send",
    "Send an email immediately, without review. Requires confirmation.",
    {
        "to": string("Recipient address."),
        "subject": string("Subject line."),
        "body": string("Message body."),
        "cc": string("Optional CC address."),
    },
    required=["to", "subject", "body"],
    risk=RISKY,
    needs_automation=True,
    preview=lambda to, subject, body, cc="": (
        f"  send email now — this leaves the Mac and cannot be recalled\n"
        f"  To:      {to}\n"
        f"  CC:      {cc or '(none)'}\n"
        f"  Subject: {subject}\n"
        f"  Body:    {body[:400]}{'…' if len(body) > 400 else ''}"
    ),
)
def mail_send(to: str, subject: str, body: str, cc: str = "") -> str:
    script = (
        "on run argv\n"
        "  set rcpt to item 1 of argv\n"
        "  set subj to item 2 of argv\n"
        "  set bdy to item 3 of argv\n"
        "  set ccAddr to item 4 of argv\n"
        '  tell application "Mail"\n'
        "    set msg to make new outgoing message with properties "
        "{subject:subj, content:bdy, visible:false}\n"
        "    tell msg\n"
        "      make new to recipient at end of to recipients with properties {address:rcpt}\n"
        '      if ccAddr is not "" then make new cc recipient at end of cc recipients '
        "with properties {address:ccAddr}\n"
        "      send\n"
        "    end tell\n"
        "  end tell\n"
        '  return "ok"\n'
        "end run\n"
    )
    result = osascript(script, to, subject, body, cc, timeout=90)
    _need_automation(result, "Mail")
    return f"Sent email to {to} — “{subject}”."


# ------------------------------------------------------------------ Messages


@tool(
    "imessage_send",
    "Send an iMessage or SMS. Requires confirmation. Look the number up with "
    "find_contact first if the user named a person rather than a number.",
    {
        "recipient": string("Phone number, Apple ID email, or exact contact handle."),
        "text": string("Message text."),
    },
    required=["recipient", "text"],
    risk=RISKY,
    needs_automation=True,
    preview=lambda recipient, text: (
        f"  send a message now — this leaves the Mac and cannot be recalled\n"
        f"  To:   {recipient}\n"
        f"  Text: {text[:400]}{'…' if len(text) > 400 else ''}"
    ),
)
def imessage_send(recipient: str, text: str) -> str:
    script = (
        "on run argv\n"
        "  set who to item 1 of argv\n"
        "  set msg to item 2 of argv\n"
        '  tell application "Messages"\n'
        "    set svc to 1st account whose service type = iMessage\n"
        "    send msg to participant who of svc\n"
        "  end tell\n"
        '  return "ok"\n'
        "end run\n"
    )
    result = osascript(script, recipient, text, timeout=60)
    if not result.ok:
        # Fall back to SMS relay if the handle is not on iMessage.
        fallback = osascript(
            "on run argv\n"
            '  tell application "Messages"\n'
            "    send (item 2 of argv) to participant (item 1 of argv)\n"
            "  end tell\n"
            '  return "ok"\n'
            "end run\n",
            recipient,
            text,
            timeout=60,
        )
        _need_automation(fallback, "Messages")
    return f"Sent to {recipient}: {text[:80]}{'…' if len(text) > 80 else ''}"


# --------------------------------------------------------------------- Music


@tool(
    "music_control",
    "Control playback in Apple Music (or Spotify).",
    {
        "action": enum(
            "What to do.",
            ["play", "pause", "next", "previous", "status"],
        ),
        "app": enum("Which player to control.", ["Music", "Spotify"]),
    },
    required=["action"],
    risk=WRITE,
    needs_automation=True,
)
def music_control(action: str, app: str = "Music") -> str:
    if action == "status":
        body = (
            "if player state is playing then\n"
            '        return "playing: " & (name of current track) & " — " & (artist of current track)\n'
            "      else\n"
            '        return "not playing"\n'
            "      end if"
        )
    else:
        verb = {
            "play": "play",
            "pause": "pause",
            "next": "next track",
            "previous": "previous track",
        }[action]
        body = f"{verb}\n      return \"ok\""
    # `player state`, `current track` and `next track` are app terminology, so the
    # application name has to be a literal for AppleScript to compile it.
    result = tell_literal(app, body, timeout=30)
    _need_automation(result, app)
    if action == "status":
        return result.out
    return f"{app}: {action}."


@tool(
    "music_play_search",
    "Search the user's Apple Music library and play the first match.",
    {"query": string("Song, album or artist to play.")},
    required=["query"],
    risk=WRITE,
    needs_automation=True,
)
def music_play_search(query: str) -> str:
    script = (
        "on run argv\n"
        "  set q to item 1 of argv\n"
        '  tell application "Music"\n'
        "    set hits to (every track of library playlist 1 whose name contains q "
        "or artist contains q or album contains q)\n"
        '    if (count of hits) is 0 then return "none"\n'
        "    play (item 1 of hits)\n"
        '    return (name of current track) & " — " & (artist of current track)\n'
        "  end tell\n"
        "end run\n"
    )
    result = osascript(script, query, timeout=60)
    _need_automation(result, "Music")
    if result.out == "none":
        return f"Nothing in your library matched {query!r}."
    return f"Playing {result.out}"


@tool(
    "dictate_to_frontmost",
    "Type text into whatever application is frontmost, as if the user typed it. "
    "Useful for filling a field the user is looking at.",
    {"text": string("Text to type.")},
    required=["text"],
    risk=RISKY,
    needs_automation=True,
    preview=lambda text: (
        "  type this into the frontmost application (it may submit a form or "
        f"send a message depending on what has focus):\n  {text[:300]}"
    ),
)
def dictate_to_frontmost(text: str) -> str:
    result = osascript(
        "on run argv\n"
        '  tell application "System Events" to keystroke (item 1 of argv)\n'
        "end run\n",
        text,
        timeout=45,
    )
    if not result.ok:
        raise PermissionError(
            "Accessibility permission is required to type. Grant it to your "
            f"terminal under System Settings → Privacy & Security → Accessibility. ({result.err})"
        )
    return f"Typed {len(text)} character(s) into the frontmost application."
