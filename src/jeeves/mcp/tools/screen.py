"""Clipboard access and screen capture with on-device OCR."""

from __future__ import annotations

import time

from ... import config
from ...mac import check_path, osascript, run
from ..registry import READ, WRITE, ToolError, boolean, enum, integer, string, tool


@tool(
    "clipboard_read",
    "Read the current text contents of the clipboard.",
    {"max_chars": integer("Truncate after this many characters, 1-100000.", 1, 100_000)},
    risk=READ,
)
def clipboard_read(max_chars: int = 20_000) -> str:
    result = run(["pbpaste"])
    if not result.ok:
        raise ToolError(f"could not read the clipboard: {result.err}")
    text = result.out
    if not text:
        return "The clipboard is empty (or holds non-text data such as an image)."
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n… truncated at {max_chars} of {len(text)} characters"
    return text


@tool(
    "clipboard_write",
    "Replace the clipboard contents with text.",
    {"text": string("Text to place on the clipboard.")},
    required=["text"],
    risk=WRITE,
    undo="clipboard_write with the previous contents",
)
def clipboard_write(text: str) -> str:
    result = run(["pbcopy"], stdin=text)
    if not result.ok:
        raise ToolError(f"could not write the clipboard: {result.err}")
    preview = text if len(text) <= 60 else text[:57] + "…"
    return f"Copied {len(text)} character(s) to the clipboard: {preview!r}"


@tool(
    "screenshot",
    "Capture the screen to a PNG file and return its path. Combine with OCR or "
    "the built-in Read tool to look at the image.",
    {
        "mode": enum(
            "What to capture: the whole display, the frontmost window, or an "
            "interactive selection the user drags.",
            ["screen", "window", "selection"],
        ),
        "delay": integer("Seconds to wait before capturing, 0-30.", 0, 30),
    },
    risk=WRITE,
)
def screenshot(mode: str = "screen", delay: int = 0) -> str:
    config.ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = config.SCREENSHOT_DIR / f"{mode}-{stamp}.png"
    argv = ["screencapture", "-x"]  # -x: no shutter sound
    if mode == "window":
        argv += ["-o", "-W"]
    elif mode == "selection":
        argv += ["-i", "-s"]
    if delay:
        argv += ["-T", str(delay)]
    argv += [str(target)]

    result = run(argv, timeout=180 if mode == "selection" else 60)
    if not target.exists():
        if "not authorized" in (result.err or "").lower() or "could not create image" in (
            result.err or result.out or ""
        ).lower():
            raise PermissionError(
                "Screen Recording permission is required. Grant it to your terminal "
                "under System Settings → Privacy & Security → Screen Recording."
            )
        if mode == "selection":
            return "Screenshot cancelled — no selection was made."
        raise ToolError(f"screencapture failed: {result.err or 'unknown error'}")
    size_kb = target.stat().st_size / 1024
    return (
        f"Saved {mode} screenshot to {target} ({size_kb:.0f} KB). "
        "Read that path to see the image, or call screen_text to OCR it."
    )


@tool(
    "screen_text",
    "Read text off the screen, or out of an image file, using on-device OCR. "
    "Use this to answer questions about whatever the user is looking at.",
    {
        "path": string("Image file to OCR. Omit to capture the screen first."),
        "fast": boolean("Trade a little accuracy for speed.", default=False),
    },
    risk=READ,
)
def screen_text(path: str = "", fast: bool = False) -> str:
    if not config.NATIVE_BIN.exists():
        raise ToolError(
            "the native OCR helper is not built yet. Run scripts/build_native.sh."
        )
    if path:
        image = check_path(path)
        if not image.exists():
            raise ToolError(f"{image} does not exist")
    else:
        config.ensure_dirs()
        image = config.SCREENSHOT_DIR / "ocr-scratch.png"
        capture = run(["screencapture", "-x", str(image)], timeout=60)
        if not image.exists():
            raise PermissionError(
                "Screen Recording permission is required to read the screen. Grant "
                "it to your terminal under System Settings → Privacy & Security → "
                f"Screen Recording. ({capture.err})"
            )

    argv = [str(config.NATIVE_BIN), "ocr", str(image)]
    if fast:
        argv.append("--fast")
    result = run(argv, timeout=90)
    if not result.ok:
        raise ToolError(f"OCR failed: {result.err or result.out}")
    if not result.out.strip():
        return f"No text was found in {image}."
    return result.out


@tool(
    "get_selected_text",
    "Get the text the user currently has selected in the frontmost application. "
    "Works by copying the selection, so it briefly replaces the clipboard.",
    risk=READ,
    needs_automation=True,
)
def get_selected_text() -> str:
    before = run(["pbpaste"]).out
    result = osascript(
        "on run argv\n"
        '  tell application "System Events" to keystroke "c" using {command down}\n'
        "  delay 0.25\n"
        "end run\n",
        timeout=15,
    )
    if not result.ok:
        raise PermissionError(
            "Accessibility permission is required to read the selection. Grant it "
            "to your terminal under System Settings → Privacy & Security → "
            f"Accessibility. ({result.err})"
        )
    after = run(["pbpaste"]).out
    if after == before:
        return "Nothing appears to be selected in the frontmost application."
    return after
