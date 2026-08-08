"""Files: Spotlight search, listing, reading, writing, moving, Trash, disk usage."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from ... import config
from ...mac import check_path, run, trash
from ..registry import (
    READ,
    RISKY,
    WRITE,
    ToolError,
    boolean,
    enum,
    integer,
    string,
    tool,
)

MAX_READ_BYTES = 400_000
TEXTY_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".css", ".py",
    ".js", ".ts", ".tsx", ".jsx", ".swift", ".c", ".h", ".cpp", ".hpp", ".m",
    ".mm", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".sh", ".zsh", ".bash",
    ".sql", ".plist", ".gitignore", ".env.example",
}


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _describe(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return f"{path.name}  (unreadable)"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
    kind = "dir " if path.is_dir() else "file"
    size = "     -" if path.is_dir() else f"{_human(stat.st_size):>6}"
    return f"{kind} {size}  {when}  {path.name}"


# ------------------------------------------------------------------- reading


@tool(
    "find_files",
    "Search the whole Mac by filename or content using the Spotlight index. Fast.",
    {
        "query": string("Words to look for."),
        "match": enum("Search filenames only, or full text content.", ["name", "content"]),
        "folder": string("Optional folder to restrict the search to."),
        "limit": integer("Maximum results, 1-100.", 1, 100),
    },
    required=["query"],
    risk=READ,
)
def find_files(query: str, match: str = "name", folder: str = "", limit: int = 25) -> str:
    if not query.strip():
        raise ToolError("query must not be empty")
    if match == "name":
        expr = f'kMDItemFSName == "*{query}*"cd'
    else:
        expr = f'kMDItemTextContent == "*{query}*"cd'
    argv = ["mdfind", expr]
    if folder:
        argv += ["-onlyin", str(check_path(folder))]
    result = run(argv, timeout=45)
    if not result.ok and not result.out:
        raise ToolError(f"Spotlight search failed: {result.err}")
    hits = [h for h in result.out.splitlines() if h.strip()][:limit]
    if not hits:
        return f"No files matched {query!r} ({match} search)."
    lines = [f"{len(hits)} match(es) for {query!r}:"]
    lines += [f"- {h}" for h in hits]
    return "\n".join(lines)


@tool(
    "list_dir",
    "List the contents of a folder with sizes and modification times.",
    {
        "path": string("Folder path. Defaults to the working directory."),
        "show_hidden": boolean("Include dotfiles.", default=False),
        "sort": enum("Sort order.", ["name", "modified", "size"]),
        "limit": integer("Maximum entries, 1-500.", 1, 500),
    },
    risk=READ,
)
def list_dir(
    path: str = "", show_hidden: bool = False, sort: str = "name", limit: int = 100
) -> str:
    folder = check_path(path or config.load().get("agent.workdir", str(Path.home())))
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder")
    try:
        entries = [e for e in folder.iterdir() if show_hidden or not e.name.startswith(".")]
    except PermissionError as exc:
        raise ToolError(f"cannot read {folder}: {exc}") from None

    def key(item: Path):
        try:
            if sort == "modified":
                return -item.stat().st_mtime
            if sort == "size":
                return -(0 if item.is_dir() else item.stat().st_size)
        except OSError:
            return 0
        return item.name.lower()

    entries.sort(key=key)
    shown = entries[:limit]
    head = f"{folder}  ({len(entries)} entries"
    head += f", showing {len(shown)})" if len(shown) < len(entries) else ")"
    return head + "\n" + "\n".join(_describe(e) for e in shown)


@tool(
    "read_file",
    "Read a text file. Truncates very large files and reports the truncation.",
    {
        "path": string("File path."),
        "max_bytes": integer("Cap on bytes read, 1-400000.", 1, MAX_READ_BYTES),
    },
    required=["path"],
    risk=READ,
)
def read_file(path: str, max_bytes: int = MAX_READ_BYTES) -> str:
    target = check_path(path)
    if not target.exists():
        raise ToolError(f"{target} does not exist")
    if target.is_dir():
        raise ToolError(f"{target} is a folder — use list_dir")
    size = target.stat().st_size
    suffix = target.suffix.lower()
    if suffix and suffix not in TEXTY_SUFFIXES and size > 4096:
        head = target.read_bytes()[:1024]
        if b"\x00" in head:
            raise ToolError(
                f"{target.name} looks binary ({_human(size)}, {suffix}). "
                "Use a dedicated tool or `open_path` instead."
            )
    data = target.read_bytes()[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    if size > len(data):
        text += f"\n\n… truncated: showed {_human(len(data))} of {_human(size)}"
    return text


@tool(
    "disk_usage",
    "Show which items in a folder are using the most space.",
    {
        "path": string("Folder to measure."),
        "limit": integer("How many entries to show, 1-40.", 1, 40),
    },
    required=["path"],
    risk=READ,
)
def disk_usage(path: str, limit: int = 15) -> str:
    folder = check_path(path)
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder")
    result = run(["du", "-sk", *[str(p) for p in folder.iterdir()]], timeout=120)
    rows: list[tuple[int, str]] = []
    for line in result.out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            rows.append((int(parts[0]) * 1024, parts[1]))
    rows.sort(reverse=True)
    total = sum(size for size, _ in rows)
    lines = [f"{folder} — {_human(total)} across {len(rows)} items:"]
    lines += [f"{_human(size):>9}  {Path(name).name}" for size, name in rows[:limit]]
    return "\n".join(lines)


# ------------------------------------------------------------------- writing


@tool(
    "write_file",
    "Create or overwrite a text file. Backs up an existing file first.",
    {
        "path": string("File path to write."),
        "content": string("Full text content of the file."),
        "append": boolean("Append instead of overwriting.", default=False),
    },
    required=["path", "content"],
    risk=WRITE,
    undo="the previous version is kept alongside as <name>.jeeves-backup",
)
def write_file(path: str, content: str, append: bool = False) -> str:
    target = check_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if target.exists() and not append:
        backup = target.with_suffix(target.suffix + ".jeeves-backup")
        shutil.copy2(target, backup)
        note = f" Previous version saved to {backup.name}."
    with target.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)
    verb = "Appended to" if append else "Wrote"
    return f"{verb} {target} ({_human(target.stat().st_size)}).{note}"


@tool(
    "move_file",
    "Move or rename a file or folder.",
    {
        "source": string("Existing path."),
        "destination": string("New path, or a destination folder."),
    },
    required=["source", "destination"],
    risk=WRITE,
    undo="move_file back to the original path",
)
def move_file(source: str, destination: str) -> str:
    src = check_path(source)
    dst = check_path(destination)
    if not src.exists():
        raise ToolError(f"{src} does not exist")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"{dst} already exists — choose another name or trash it first")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} → {dst}"


@tool(
    "copy_file",
    "Copy a file or folder.",
    {"source": string("Existing path."), "destination": string("Target path or folder.")},
    required=["source", "destination"],
    risk=WRITE,
    undo="trash_file on the copy",
)
def copy_file(source: str, destination: str) -> str:
    src = check_path(source)
    dst = check_path(destination)
    if not src.exists():
        raise ToolError(f"{src} does not exist")
    if dst.is_dir() and src.is_file():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"{dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return f"Copied {src} → {dst}"


@tool(
    "make_folder",
    "Create a folder, including any missing parent folders.",
    {"path": string("Folder path to create.")},
    required=["path"],
    risk=WRITE,
    undo="trash_file on the new folder",
)
def make_folder(path: str) -> str:
    target = check_path(path)
    if target.exists():
        return f"{target} already exists."
    target.mkdir(parents=True)
    return f"Created {target}"


@tool(
    "trash_file",
    "Move a file or folder to the Trash. Recoverable from the Trash afterwards.",
    {"path": string("Path to move to the Trash.")},
    required=["path"],
    risk=WRITE,
    undo="restore it from the Trash (Finder → Put Back)",
    needs_automation=True,
)
def trash_file(path: str) -> str:
    return trash(path)


@tool(
    "delete_permanently",
    "Delete a file or folder irreversibly, bypassing the Trash. Prefer trash_file.",
    {"path": string("Path to delete forever.")},
    required=["path"],
    risk=RISKY,
    preview=lambda path: (
        f"  permanently delete {path}\n"
        "  This bypasses the Trash and CANNOT be undone. trash_file is the "
        "recoverable alternative."
    ),
)
def delete_permanently(path: str) -> str:
    target = check_path(path)
    if not target.exists():
        raise ToolError(f"{target} does not exist")
    if target == Path.home() or str(target) == "/":
        raise ToolError("refusing to delete your home folder or the filesystem root")
    if target.is_dir():
        count = sum(1 for _ in target.rglob("*"))
        shutil.rmtree(target)
        return f"Permanently deleted folder {target} and {count} item(s) inside it."
    size = target.stat().st_size
    target.unlink()
    return f"Permanently deleted {target} ({_human(size)})."


@tool(
    "recent_files",
    "Files changed most recently in a folder, newest first.",
    {
        "folder": string("Folder to scan. Defaults to the working directory."),
        "hours": integer("Look back this many hours, 1-8760.", 1, 8760),
        "limit": integer("Maximum results, 1-100.", 1, 100),
    },
    risk=READ,
)
def recent_files(folder: str = "", hours: int = 24, limit: int = 25) -> str:
    root = check_path(folder or config.load().get("agent.workdir", str(Path.home())))
    if not root.is_dir():
        raise ToolError(f"{root} is not a folder")
    cutoff = time.time() - hours * 3600
    found: list[tuple[float, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")][:200]
        for name in filenames:
            if name.startswith("."):
                continue
            item = Path(dirpath) / name
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                found.append((mtime, item))
        if len(found) > 5000:
            break
    found.sort(reverse=True)
    if not found:
        return f"Nothing changed under {root} in the last {hours}h."
    lines = [f"{len(found)} file(s) changed under {root} in the last {hours}h:"]
    for mtime, item in found[:limit]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        lines.append(f"{when}  {item.relative_to(root)}")
    return "\n".join(lines)
