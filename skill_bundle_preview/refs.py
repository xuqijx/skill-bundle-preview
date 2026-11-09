"""Heuristic relative-path checks for the root SKILL.md.

Only ``<skill-dir>/SKILL.md`` is opened. Other markdown files are not opened,
hashed, or scanned. Extracts inline links, image links, reference definitions,
and backtick spans that look like relative paths. External URLs are ignored.
Fenced code blocks are ignored. Secret-like filenames are never opened.
Symlinks are not followed.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from skill_bundle_preview.inventory import PreviewIOError
from skill_bundle_preview.secrets import matching_secret_rule

_LINK_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+\"[^\"]*\"|\s+'[^']*')?\s*\)"
)
_REFDEF_RE = re.compile(r"^[ ]{0,3}\[[^\]]+\]:[ \t]+(\S+)")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# Filename-only. Paths that contain a slash are accepted before this regex runs.
_EXT_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9._+-]*"
    r"\.(?:md|markdown|txt|py|sh|bash|json|yaml|yml|toml|png|jpg|jpeg|gif|svg|webp|html|css|js|ts|csv|xml|ini|cfg|conf)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RefError:
    path: str
    line: int
    ref: str
    kind: str
    message: str


def markdown_files(relative_paths: list[str]) -> list[str]:
    """Return markdown paths whose contents may be read.

    The only selectable path is the skill-root ``SKILL.md``. Nested files such
    as ``notes.md`` and ``references/notes.md`` stay unread even if the
    inventory lists them.
    """
    if "SKILL.md" in relative_paths:
        return ["SKILL.md"]
    return []


def find_ref_errors(root: Path) -> list[RefError]:
    """Check relative refs in the root SKILL.md and nowhere else."""
    errors: list[RefError] = []
    seen: set[tuple[str, int, str, str]] = set()
    for relative in markdown_files(["SKILL.md"]):
        text = _read_markdown(root / relative)
        for line_no, ref in _iter_refs(text):
            located = _locate(root, root / relative, ref)
            if located is None:
                continue
            target, problem = located
            if problem is None:
                if target is not None and _unfollowed_exists(root, target):
                    continue
                problem = "missing"
            if problem == "missing":
                kind = "missing-ref"
                message = "missing relative ref"
            elif problem == "escapes":
                kind = "escaped-ref"
                message = "relative ref escapes the skill directory"
            else:
                continue
            key = (relative, line_no, ref, kind)
            if key in seen:
                continue
            seen.add(key)
            errors.append(RefError(relative, line_no, ref, kind, message))
    return errors


def _read_markdown(path: Path) -> str:
    """Open a regular root SKILL.md. Never follow a symlink or read any other file."""
    if path.name != "SKILL.md":
        raise PreviewIOError(
            f"refusing to read {path.name}; only the root SKILL.md is opened"
        )
    if matching_secret_rule(path.name):
        raise PreviewIOError(f"refused to open secret-like filename: {path.name}")
    try:
        info = path.lstat()
    except OSError as err:
        raise PreviewIOError(f"cannot read markdown {path}: {err.strerror}") from err
    if stat.S_ISLNK(info.st_mode):
        raise PreviewIOError(f"refusing to follow symlink SKILL.md: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise PreviewIOError(f"refusing to follow symlink SKILL.md: {path}") from err
        raise PreviewIOError(f"cannot read markdown {path}: {err.strerror}") from err
    try:
        with os.fdopen(fd, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read()
    except OSError as err:
        raise PreviewIOError(f"cannot read markdown {path}: {err.strerror}") from err


def _unfollowed_exists(root: Path, target: Path) -> bool:
    """True when every path component inside ``root`` exists via lstat.

    Intermediate symlinks are not traversed, so a directory symlink cannot
    pull existence checks outside the skill directory. A final symlink counts
    as an existing directory entry and is not opened.
    """
    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    current = root
    parts = relative.parts
    if not parts:
        try:
            mode = root.lstat().st_mode
        except OSError:
            return False
        return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return False
        last = index == len(parts) - 1
        if stat.S_ISLNK(mode):
            return last
        if last:
            return stat.S_ISREG(mode) or stat.S_ISDIR(mode)
        if not stat.S_ISDIR(mode):
            return False
    return False


def _iter_refs(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    in_fence = False
    fence_mark = ""
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip(" ")
        if stripped.startswith("```") or stripped.startswith("~~~"):
            mark = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_mark = mark
            elif mark == fence_mark:
                in_fence = False
                fence_mark = ""
            continue
        if in_fence:
            continue
        spans = [(match.start(), match.end(), match.group(1)) for match in _CODE_RE.finditer(line)]
        masked = _mask_spans(line, spans)
        for _start, _end, raw in spans:
            cleaned = _clean_target(raw)
            if cleaned is not None and _is_backtick_path(raw):
                found.append((line_no, cleaned))
        for match in _LINK_RE.finditer(masked):
            cleaned = _clean_target(match.group(1))
            if cleaned is not None:
                found.append((line_no, cleaned))
        for match in _REFDEF_RE.finditer(masked):
            cleaned = _clean_target(match.group(1))
            if cleaned is not None:
                found.append((line_no, cleaned))
    return found


def _mask_spans(line: str, spans: list[tuple[int, int, str]]) -> str:
    chars = list(line)
    for start, end, _raw in spans:
        for index in range(start, end):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _is_backtick_path(raw: str) -> bool:
    text = raw.strip()
    if not text or len(text) > 240:
        return False
    if any(ch.isspace() for ch in text):
        return False
    if any(ch in text for ch in "*?$=|;<>(){}[]'\"\\`"):
        return False
    if text.startswith(("-", "~")):
        return False
    shape = text.split("#", 1)[0].split("?", 1)[0]
    if not shape or "://" in shape or shape.startswith("#"):
        return False
    if shape.startswith(("./", "../")) or "/" in shape:
        return ":" not in shape
    return _EXT_RE.match(shape) is not None


def _clean_target(raw: str) -> str | None:
    """Return a relative path to check, or None when the target is not local."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == "<" and text[-1] == ">":
        text = text[1:-1].strip()
    if not text or text.startswith(("#", "~")):
        return None
    path = text.split("#", 1)[0].split("?", 1)[0].strip()
    if not path:
        return None
    lowered = path.lower()
    if "://" in path or lowered.startswith("mailto:") or path.startswith("//"):
        return None
    if _SCHEME_RE.match(path):
        return None
    if path.startswith("/"):
        return None
    return path


def _locate(root: Path, source: Path, ref: str) -> tuple[Path | None, str | None] | None:
    """Return ``(target, None)`` or ``(None, problem)``.

    ``None`` as the whole result means the string is not a relative ref.
    """
    parts: list[str] = []
    base = source.parent
    try:
        parts.extend(base.relative_to(root).parts)
    except ValueError:
        return None, "escapes"
    for piece in ref.replace("\\", "/").split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                return None, "escapes"
            parts.pop()
            continue
        parts.append(piece)
    return root.joinpath(*parts), None
