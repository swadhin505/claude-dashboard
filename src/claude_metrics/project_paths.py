"""Stable project path identity across transcript-producing operating systems."""

import re

WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:(?:/|$)")


def canonical_project_root(cwd: str) -> str:
    """Normalize separators and case only where the source path is Windows-style."""
    root = cwd.replace("\\", "/").rstrip("/") or "/"
    if WINDOWS_DRIVE.match(root) or root.startswith("//"):
        return root.casefold()
    return root
