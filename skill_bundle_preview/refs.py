"""Heuristic relative-path checks for skill markdown.

Reads SKILL.md and other ``*.md`` files. Extracts inline links, image links,
reference definitions, and backtick spans that look like relative paths.
External URLs are ignored. Fenced code blocks are ignored. Secret-like
filenames are never opened.
"""

from __future__ import annotations

import re
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


def markdown_files(root: Path, relative_paths: list[str]) -> list[str]:
    """``*.md`` paths whose basenames are safe to open."""
    chosen: list[str] = []
    for relative in relative_paths:
        name = relative.rsplit("/", 1)[-1]
        if not name.endswith(".md"):
            continue
        if matching_secret_rule(name):
            continue
        chosen.append(relative)
    return chosen


def find_ref_errors(root: Path, relative_paths: list[str]) -> list[RefError]:
    errors: list[RefError] = []
    seen: set[tuple[str, int, str, str]] = set()
    for relative in markdown_files(root, relative_paths):
        text = _read_markdown(root / relative)
        for line_no, ref in _iter_refs(text):
            located = _locate(root, root / relative, ref)
            if located is None:
                continue
            target, problem = located
            if problem is None:
                if target is not None and (target.is_file() or target.is_dir()):
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
    # Basename gate is applied again so a future caller cannot open a denylist file.
    if matching_secret_rule(path.name):
        raise PreviewIOError(f"refused to open secret-like filename: {path.name}")
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read()
    except OSError as err:
        raise PreviewIOError(f"cannot read markdown {path}: {err.strerror}") from err


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
