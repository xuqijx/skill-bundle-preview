"""Command-line entry for the skill bundle dry-run."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from skill_bundle_preview.inventory import FileEntry, PreviewIOError, list_files
from skill_bundle_preview.refs import RefError, find_ref_errors
from skill_bundle_preview.secrets import matching_secret_rule

SECRET_MESSAGE = "secret-like filename; contents not read"
USAGE = "usage: python3 -m skill_bundle_preview <skill-dir> [--json] [--strict-secrets]"


@dataclass(frozen=True)
class SecretHit:
    path: str
    rule: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m skill_bundle_preview",
        description=(
            "Offline dry-run before sharing or zipping an Agent Skill folder. "
            "Lists every file with its size, warns on secret-like filenames "
            "without reading them, and reports relative markdown refs that "
            "are missing inside the skill directory."
        ),
    )
    parser.add_argument(
        "skill_dir",
        help="Skill directory with SKILL.md at its root",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON",
    )
    parser.add_argument(
        "--strict-secrets",
        action="store_true",
        help="Treat secret-like filenames as errors instead of warnings",
    )
    return parser


def inspect_skill(root: Path) -> tuple[list[FileEntry], list[SecretHit], list[RefError]]:
    if not root.exists():
        raise PreviewIOError(f"skill directory not found: {root}")
    if not root.is_dir():
        raise PreviewIOError(f"skill path is not a directory: {root}")
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise PreviewIOError(
            f"SKILL.md is required at the root of the skill directory: {root}"
        )

    files = list_files(root)
    secrets: list[SecretHit] = []
    for entry in files:
        basename = entry.path.rsplit("/", 1)[-1]
        rule = matching_secret_rule(basename)
        if rule is not None:
            secrets.append(SecretHit(entry.path, rule))
    ref_errors = find_ref_errors(root, [entry.path for entry in files])
    return files, secrets, ref_errors


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.skill_dir).expanduser()
    try:
        files, secrets, ref_errors = inspect_skill(root)
    except PreviewIOError as err:
        print(f"error: {err}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    warnings = [] if args.strict_secrets else secrets
    errors: list[SecretHit | RefError] = []
    if args.strict_secrets:
        errors.extend(secrets)
    errors.extend(ref_errors)

    if args.json:
        sys.stdout.write(
            render_json(args.skill_dir, files, warnings, errors, args.strict_secrets)
        )
    else:
        sys.stdout.write(
            render_text(args.skill_dir, files, warnings, errors)
        )
    return 1 if errors else 0


def render_text(
    skill_dir: str,
    files: list[FileEntry],
    warnings: list[SecretHit],
    errors: list[SecretHit | RefError],
) -> str:
    path_width = max([len("path"), *[len(item.path) for item in files]])
    byte_width = max([len("bytes"), *[len(str(item.size)) for item in files]])
    lines = [f"skill-bundle-preview {skill_dir}", "", "inventory"]
    lines.append(f"  {'path'.ljust(path_width)}  {'bytes'.rjust(byte_width)}")
    for item in files:
        lines.append(f"  {item.path.ljust(path_width)}  {str(item.size).rjust(byte_width)}")
    total = sum(item.size for item in files)
    lines.append(f"total_bytes  {total}")
    lines.append(f"file_count  {len(files)}")
    lines.append("")
    lines.append("warnings")
    if warnings:
        for hit in warnings:
            lines.append(
                f"  warning  {hit.path}  rule={hit.rule}  {SECRET_MESSAGE}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("errors")
    if errors:
        for item in errors:
            lines.append(f"  {format_error(item)}")
    else:
        lines.append("  (none)")
    lines.append("")
    return "\n".join(lines)


def format_error(item: SecretHit | RefError) -> str:
    if isinstance(item, SecretHit):
        return f"error  {item.path}  rule={item.rule}  {SECRET_MESSAGE}"
    return f"error  {item.path}:{item.line}  {item.message}  {item.ref}"


def render_json(
    skill_dir: str,
    files: list[FileEntry],
    warnings: list[SecretHit],
    errors: list[SecretHit | RefError],
    strict_secrets: bool,
) -> str:
    payload = {
        "skill_dir": skill_dir,
        "files": [{"path": item.path, "bytes": item.size} for item in files],
        "file_count": len(files),
        "total_bytes": sum(item.size for item in files),
        "warnings": [secret_payload(item) for item in warnings],
        "errors": [error_payload(item) for item in errors],
        "strict_secrets": strict_secrets,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def secret_payload(item: SecretHit) -> dict[str, str]:
    return {
        "kind": "secret-filename",
        "path": item.path,
        "rule": item.rule,
        "message": SECRET_MESSAGE,
    }


def error_payload(item: SecretHit | RefError) -> dict[str, object]:
    if isinstance(item, SecretHit):
        return secret_payload(item)
    return {
        "kind": item.kind,
        "path": item.path,
        "line": item.line,
        "ref": item.ref,
        "message": item.message,
    }
