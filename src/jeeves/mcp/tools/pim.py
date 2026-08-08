"""Calendar, Reminders and Contacts via EventKit/Contacts in the native helper."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from ... import config
from ...mac import run
from ..registry import READ, WRITE, ToolError, boolean, integer, string, tool

PERMISSION_EXIT = 77


def native(args: list[str], timeout: int = 60) -> str:
    """Invoke the native helper, translating its exit codes into good errors."""
    if not config.NATIVE_BIN.exists():
        raise ToolError(
            "the native helper is not built. Run: bash scripts/build_native.sh"
        )
    result = run([str(config.NATIVE_BIN), *args], timeout=timeout)
    if result.code == PERMISSION_EXIT:
        raise PermissionError(result.err or result.out or "permission denied")
    if not result.ok:
        raise ToolError(result.err or result.out or "native helper failed")
    return result.out


def native_json(args: list[str], timeout: int = 60) -> Any:
    raw = native(args, timeout)
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError as exc:
        raise ToolError(f"native helper returned malformed JSON: {exc}") from None


def _pretty_time(iso_text: str, all_day: bool = False) -> str:
    if not iso_text:
        return "no date"
    try:
        moment = datetime.fromisoformat(iso_text)
    except ValueError:
        return iso_text
    today = datetime.now().astimezone().date()
    delta = (moment.date() - today).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(delta) or moment.strftime("%a %d %b")
    return day if all_day else f"{day} {moment.strftime('%H:%M')}"


# ------------------------------------------------------------------ calendar


@tool(
    "calendar_agenda",
    "Read upcoming calendar events. Use this for 'what's on today', 'am I free "
    "at 3', 'what's my week look like'.",
    {"days": integer("How many days ahead to include, 1-60.", 1, 60)},
    risk=READ,
)
def calendar_agenda(days: int = 1) -> str:
    events = native_json(["events", str(days)])
    if not events:
        return f"Nothing scheduled in the next {days} day(s)."
    lines = [f"{len(events)} event(s) in the next {days} day(s):"]
    for event in events:
        when = _pretty_time(event.get("start", ""), event.get("all_day", False))
        until = ""
        if not event.get("all_day") and event.get("end"):
            try:
                until = "–" + datetime.fromisoformat(event["end"]).strftime("%H:%M")
            except ValueError:
                until = ""
        where = f"  @ {event['location']}" if event.get("location") else ""
        cal = f"  [{event['calendar']}]" if event.get("calendar") else ""
        lines.append(f"- {when}{until}  {event.get('title', '(untitled)')}{where}{cal}")
    return "\n".join(lines)


@tool(
    "calendar_add_event",
    "Create a calendar event. Times must be ISO-8601 in the user's local zone, "
    "e.g. 2026-08-09T15:00:00. Work out concrete times from phrases like "
    "'tomorrow at 3' yourself before calling.",
    {
        "title": string("Event title."),
        "start": string("Start time, ISO-8601 local, e.g. 2026-08-09T15:00:00."),
        "duration_minutes": integer("Length in minutes, 1-1440.", 1, 1440),
        "end": string("Optional explicit end time, ISO-8601. Overrides duration."),
        "location": string("Optional location."),
        "notes": string("Optional notes."),
        "calendar": string("Optional calendar name. Defaults to your default calendar."),
        "all_day": boolean("Make this an all-day event.", default=False),
        "alarm_minutes_before": integer("Optional alert, minutes before, 0-10080.", 0, 10080),
    },
    required=["title", "start"],
    risk=WRITE,
    undo="delete the event in Calendar.app",
)
def calendar_add_event(**kwargs: Any) -> str:
    payload = {k: v for k, v in kwargs.items() if v not in ("", None)}
    payload.setdefault("duration_minutes", 60)
    created = native_json(["add-event", json.dumps(payload)])
    when = _pretty_time(created.get("start", ""), payload.get("all_day", False))
    return (
        f"Created “{created.get('title')}” {when} "
        f"in {created.get('calendar') or 'your default calendar'}."
    )


@tool(
    "calendar_free_slots",
    "Find gaps in the schedule between two times on a given day, so you can "
    "suggest when the user is free.",
    {
        "date": string("Day to inspect, YYYY-MM-DD. Defaults to today."),
        "from_hour": integer("Earliest hour to consider, 0-23.", 0, 23),
        "to_hour": integer("Latest hour to consider, 1-24.", 1, 24),
        "min_minutes": integer("Ignore gaps shorter than this, 5-480.", 5, 480),
    },
    risk=READ,
)
def calendar_free_slots(
    date: str = "", from_hour: int = 9, to_hour: int = 18, min_minutes: int = 30
) -> str:
    if to_hour <= from_hour:
        raise ToolError("to_hour must be later than from_hour")
    day = date or time.strftime("%Y-%m-%d")
    try:
        target = datetime.fromisoformat(day).date()
    except ValueError:
        raise ToolError("date must look like YYYY-MM-DD") from None

    horizon = max(1, (target - datetime.now().astimezone().date()).days + 1)
    events = native_json(["events", str(min(horizon, 60))])

    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        if event.get("all_day"):
            continue
        try:
            start = datetime.fromisoformat(event["start"])
            end = datetime.fromisoformat(event["end"])
        except (KeyError, ValueError):
            continue
        if start.date() == target:
            busy.append((start, end))
    busy.sort()

    tz = datetime.now().astimezone().tzinfo
    midnight = datetime.combine(target, datetime.min.time(), tzinfo=tz)
    cursor = midnight.replace(hour=from_hour)
    # hour=24 is not representable, so clamp the window to 23:59 on that day.
    limit = midnight.replace(hour=23, minute=59) if to_hour == 24 else midnight.replace(
        hour=to_hour
    )

    gaps: list[str] = []
    for start, end in busy:
        if start > cursor:
            minutes = (min(start, limit) - cursor).total_seconds() / 60
            if minutes >= min_minutes:
                gaps.append(f"{cursor.strftime('%H:%M')}–{min(start, limit).strftime('%H:%M')}")
        cursor = max(cursor, end)
        if cursor >= limit:
            break
    if cursor < limit and (limit - cursor).total_seconds() / 60 >= min_minutes:
        gaps.append(f"{cursor.strftime('%H:%M')}–{limit.strftime('%H:%M')}")

    label = "today" if target == datetime.now().astimezone().date() else target.isoformat()
    if not gaps:
        return f"No free block of {min_minutes}+ minutes {label} between {from_hour}:00 and {to_hour}:00."
    return f"Free {label} ({min_minutes}+ min): " + ", ".join(gaps)


# ----------------------------------------------------------------- reminders


@tool("reminders_list", "List all open (incomplete) reminders.", risk=READ)
def reminders_list() -> str:
    items = native_json(["reminders"])
    if not items:
        return "No open reminders."
    lines = [f"{len(items)} open reminder(s):"]
    for item in items:
        due = f"  (due {_pretty_time(item['due'])})" if item.get("due") else ""
        lst = f"  [{item['list']}]" if item.get("list") else ""
        lines.append(f"- {item.get('title')}{due}{lst}\n    id: {item.get('id')}")
    return "\n".join(lines)


@tool(
    "reminders_add",
    "Add a reminder, optionally with a due date and alert.",
    {
        "title": string("What to be reminded about."),
        "due": string("Optional due time, ISO-8601 local, e.g. 2026-08-09T17:30:00."),
        "notes": string("Optional notes."),
        "list": string("Optional Reminders list name."),
    },
    required=["title"],
    risk=WRITE,
    undo="delete the reminder in Reminders.app",
)
def reminders_add(title: str, due: str = "", notes: str = "", list: str = "") -> str:  # noqa: A002
    payload = {"title": title}
    if due:
        payload["due"] = due
    if notes:
        payload["notes"] = notes
    if list:
        payload["list"] = list
    created = native_json(["add-reminder", json.dumps(payload)])
    when = f" due {_pretty_time(created.get('due', ''))}" if created.get("due") else ""
    return f"Added reminder “{created.get('title')}”{when} to {created.get('list') or 'your default list'}."


@tool(
    "reminders_complete",
    "Mark a reminder as done. Get the id from reminders_list first.",
    {"id": string("Reminder identifier from reminders_list.")},
    required=["id"],
    risk=WRITE,
    undo="untick it in Reminders.app",
)
def reminders_complete(id: str) -> str:  # noqa: A002
    return native(["complete-reminder", id])


# ------------------------------------------------------------------ contacts


@tool(
    "find_contact",
    "Look up a person's phone numbers and email addresses by name. Use this "
    "before sending a message or an email to someone named informally.",
    {"name": string("Full or partial name to search for.")},
    required=["name"],
    risk=READ,
)
def find_contact(name: str) -> str:
    people = native_json(["contacts", name])
    if not people:
        return f"No contact matched {name!r}."
    lines = [f"{len(people)} contact(s) matching {name!r}:"]
    for person in people:
        lines.append(f"- {person.get('name') or '(no name)'}")
        if person.get("organization"):
            lines.append(f"    org:    {person['organization']}")
        for phone in person.get("phones", []):
            lines.append(f"    phone:  {phone}")
        for email in person.get("emails", []):
            lines.append(f"    email:  {email}")
    return "\n".join(lines)
