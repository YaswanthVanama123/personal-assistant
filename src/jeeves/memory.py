"""Persistent memory, conversation history and the audit trail.

Everything lives in one SQLite file so it is trivially inspectable:

    sqlite3 ~/.local/state/jeeves/jeeves.db 'select * from facts'

Full-text search uses FTS5 when the local SQLite provides it, and falls back to
LIKE matching when it does not.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'note',
    text       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'jeeves',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_kind ON facts(kind);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    interface   TEXT NOT NULL DEFAULT 'cli',
    started_at  REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    tool     TEXT NOT NULL,
    args     TEXT NOT NULL DEFAULT '{}',
    outcome  TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT '',
    undo     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    text, tags, content='facts', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, tags)
    VALUES('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text, tags)
    VALUES('delete', old.id, old.text, old.tags);
    INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
"""

_has_fts: bool | None = None


@dataclass(slots=True)
class Fact:
    id: int
    kind: str
    text: str
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def render(self) -> str:
        tag_part = f"  [{', '.join(self.tags)}]" if self.tags else ""
        when = time.strftime("%Y-%m-%d", time.localtime(self.created_at))
        return f"#{self.id} ({self.kind}, {when}) {self.text}{tag_part}"


def connect() -> sqlite3.Connection:
    global _has_fts
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    if _has_fts is None:
        try:
            conn.executescript(FTS_SCHEMA)
            _has_fts = True
        except sqlite3.OperationalError:
            _has_fts = False
    elif _has_fts:
        conn.executescript(FTS_SCHEMA)
    conn.commit()
    return conn


def has_fts() -> bool:
    if _has_fts is None:
        connect().close()
    return bool(_has_fts)


# --------------------------------------------------------------------- facts


def remember(text: str, kind: str = "note", tags: Iterable[str] = ()) -> Fact:
    text = text.strip()
    if not text:
        raise ValueError("nothing to remember")
    tag_str = ",".join(sorted({t.strip().lower() for t in tags if t.strip()}))
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO facts(kind, text, tags, created_at, updated_at) VALUES (?,?,?,?,?)",
            (kind, text, tag_str, now, now),
        )
        return Fact(int(cur.lastrowid or 0), kind, text, tag_str.split(",") if tag_str else [], now)


def recall(query: str = "", limit: int = 12, kind: str = "") -> list[Fact]:
    query = query.strip()
    clauses: list[str] = []
    params: list[Any] = []

    with connect() as conn:
        if query and has_fts():
            # Quote each term so user punctuation can't become FTS syntax.
            terms = " ".join(f'"{t}"' for t in query.replace('"', " ").split() if t)
            sql = (
                "SELECT f.* FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                "WHERE facts_fts MATCH ?"
            )
            params.append(terms or '""')
            if kind:
                sql += " AND f.kind = ?"
                params.append(kind)
            sql += " ORDER BY bm25(facts_fts) LIMIT ?"
            params.append(limit)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                return [_row_to_fact(r) for r in rows]
            params.clear()

        if query:
            clauses.append("(text LIKE ? OR tags LIKE ?)")
            params += [f"%{query}%", f"%{query}%"]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM facts {where} ORDER BY updated_at DESC LIMIT ?", params
        ).fetchall()
    return [_row_to_fact(r) for r in rows]


def forget(fact_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        return cur.rowcount > 0


def _row_to_fact(row: sqlite3.Row) -> Fact:
    tags = [t for t in (row["tags"] or "").split(",") if t]
    return Fact(row["id"], row["kind"], row["text"], tags, row["created_at"])


def profile_block(limit: int = 40) -> str:
    """Durable user facts, injected into the system prompt each session."""
    facts = recall(kind="profile", limit=limit)
    if not facts:
        return ""
    lines = "\n".join(f"- {f.text}" for f in facts)
    return f"Things you know about your user (from earlier sessions):\n{lines}"


# ------------------------------------------------------- sessions / messages


def touch_session(session_id: str, interface: str = "cli", title: str = "") -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, title, interface, started_at, last_seen_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
            "title=CASE WHEN sessions.title='' THEN excluded.title ELSE sessions.title END",
            (session_id, title, interface, now, now),
        )


def log_message(session_id: str, role: str, text: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages(session_id, role, text, created_at) VALUES (?,?,?,?)",
            (session_id, role, text, time.time()),
        )


def history(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, text, created_at FROM messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def recent_sessions(limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY last_seen_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------- audit


def audit(tool: str, args: dict[str, Any], outcome: str, detail: str = "", undo: str = "") -> None:
    """Record a tool invocation to SQLite and to a tail-able JSONL file."""
    record = {
        "ts": time.time(),
        "tool": tool,
        "args": args,
        "outcome": outcome,
        "detail": detail[:2000],
        "undo": undo,
    }
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO audit(ts, tool, args, outcome, detail, undo) VALUES (?,?,?,?,?,?)",
                (
                    record["ts"],
                    tool,
                    json.dumps(args, default=str)[:4000],
                    outcome,
                    record["detail"],
                    undo,
                ),
            )
    except sqlite3.Error:
        pass  # auditing must never break a tool call
    try:
        config.ensure_dirs()
        with Path(config.AUDIT_LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def recent_audit(limit: int = 25) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, tool, outcome, detail, undo FROM audit ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
