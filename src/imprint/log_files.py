"""Private JSON-lines logs: append a line, rotate once, read the newest lines."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Mapping

from imprint.permissions import secure_directory, secure_file


def rotated_path(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def append_capped(path: Path, entry: Mapping[str, object], max_bytes: int) -> None:
    """Append one line, first moving the log aside if the line would pass ``max_bytes``."""
    line = (json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    if not path.parent.is_dir():
        secure_directory(path.parent)
    try:
        size = path.stat(follow_symlinks=False).st_size
    except FileNotFoundError:
        size = -1
    if size >= 0 and size + len(line) > max_bytes:
        os.replace(path, rotated_path(path))
        size = -1
    # O_NONBLOCK is for a pipe left at this path, which would hold every hook open.
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)
    if size < 0:
        secure_file(path)


def _tail_lines(path: Path, limit: int) -> tuple[list[bytes], int]:
    """Complete lines from the last ``limit`` bytes of one log file, and the
    number of bytes read. A missing or irregular file has none."""
    if limit <= 0:
        return [], 0
    try:
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode):
            return [], 0
        with path.open("rb") as handle:
            offset = max(0, status.st_size - limit)
            handle.seek(offset)
            data = handle.read(limit)
    except OSError:
        return [], 0
    lines = data.split(b"\n")
    if offset:
        lines = lines[1:]  # the first line of a tail read may be partial
    return lines, len(data)


def newest_lines(path: Path, limit: int) -> list[bytes]:
    """The newest ``limit`` bytes of a log, oldest line first, continuing into the
    rotated file when the current one is shorter, so a rotation cannot hide recent lines."""
    lines, used = _tail_lines(path, limit)
    rotated, _ = _tail_lines(rotated_path(path), limit - used)
    return rotated + lines
