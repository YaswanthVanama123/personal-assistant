"""Importing this package registers every tool.

Order is irrelevant; each module calls @tool at import time.
"""

from . import (  # noqa: F401
    apps,
    browser,
    comms,
    files,
    pim,
    screen,
    shell,
    system,
    whatsapp,
)

__all__ = [
    "apps", "browser", "comms", "files", "pim", "screen", "shell", "system",
    "whatsapp",
]
