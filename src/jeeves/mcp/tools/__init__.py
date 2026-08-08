"""Importing this package registers every tool.

Order is irrelevant; each module calls @tool at import time.
"""

from . import apps, comms, files, pim, screen, shell, system, whatsapp  # noqa: F401

__all__ = ["apps", "comms", "files", "pim", "screen", "shell", "system", "whatsapp"]
