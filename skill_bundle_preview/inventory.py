"""Plain file inventory of a skill directory.

Every non-directory entry is included, including dotfiles and secret-like
names. Sizes come from lstat, so file contents are not read and symlink
targets are not followed. Directory symlinks are not descended into.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class PreviewIOError(Exception):
    """A usage or filesystem problem. The CLI maps this to exit code 2."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int


def list_files(root: Path) -> list[FileEntry]:
    """Return relative POSIX paths and lstat sizes, sorted by path."""
    if not root.exists():
        raise PreviewIOError(f"skill directory not found: {root}")
    if not root.is_dir():
        raise PreviewIOError(f"skill path is not a directory: {root}")

    failures: list[OSError] = []

    def onerror(err: OSError) -> None:
        failures.append(err)

    entries: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        # Do not follow directory symlinks. os.walk already leaves them
        # unexpanded when followlinks is false; keep that explicit.
        dirnames[:] = [name for name in dirnames if not (Path(dirpath) / name).is_symlink()]
        current = Path(dirpath)
        for name in filenames:
            path = current / name
            try:
                size = path.lstat().st_size
            except OSError as err:
                failures.append(err)
                continue
            entries.append(FileEntry(path.relative_to(root).as_posix(), size))

    if failures:
        first = failures[0]
        location = getattr(first, "filename", None) or root
        raise PreviewIOError(f"cannot list skill directory ({location}): {first.strerror}")

    entries.sort(key=lambda item: item.path)
    return entries
