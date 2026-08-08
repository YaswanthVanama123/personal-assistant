"""Importing this package registers every tool.

Order is irrelevant; each module calls @tool at import time.
"""

from . import apps, comms, files, pim, screen, shell, system  # noqa: F401

__all__ = ["apps", "comms", "files", "pim", "screen", "shell", "system"]
