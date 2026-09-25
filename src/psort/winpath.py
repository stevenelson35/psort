"""Showing WSL paths the way File Explorer wants them."""

import os
import re
from pathlib import Path


def windows_path(path: Path) -> str:
    """/mnt/c/Users/x → C:\\Users\\x; other WSL paths → \\\\wsl.localhost\\<distro>\\…"""
    text = str(path)
    if m := re.match(r"^/mnt/([a-z])(/.*)?$", text):
        return f"{m.group(1).upper()}:" + (m.group(2) or "/").replace("/", "\\")
    distro = os.environ.get("WSL_DISTRO_NAME")
    return f"\\\\wsl.localhost\\{distro}{text.replace('/', chr(92))}" if distro else text
