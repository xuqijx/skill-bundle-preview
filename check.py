#!/usr/bin/env python3
"""Run skill-bundle-preview against the fictional fixtures and extra checks.

Uses the Python standard library only. The three fixture commands are the
acceptance checks. The other checks lock filename rules, ref heuristics, and
the promise that denylist files are not opened or printed.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from skill_bundle_preview.cli import inspect_skill
from skill_bundle_preview.inventory import PreviewIOError
from skill_bundle_preview.refs import markdown_files

ROOT = Path(__file__).resolve().parent
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
FAILURES: list[str] = []


def check(condition: bool, message: str, detail: str = "") -> None:
    if condition:
        print(f"ok   {message}")
        return
    FAILURES.append(message)
    print(f"FAIL {message}")
    if detail:
        print(detail.rstrip())


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "skill_bundle_preview", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=ENV,
    )


def snapshot_fixtures() -> list[tuple[str, int, int]]:
    rows: list[tuple[str, int, int]] = []
    fixtures = ROOT / "fixtures"
    for path in fixtures.rglob("*"):
        if path.is_symlink() or path.is_file():
            stat_result = path.lstat()
            rows.append(
                (
                    path.relative_to(fixtures).as_posix(),
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                )
            )
    return sorted(rows)


def test_ok_fixture() -> None:
    proc = run(["./fixtures/ok-skill"])
    check(proc.returncode == 0, f"fixtures/ok-skill exit {proc.returncode} (want 0)", proc.stdout + proc.stderr)
    check(proc.stderr == "", "fixtures/ok-skill stderr empty", proc.stderr)
    check("missing relative ref" not in proc.stdout, "fixtures/ok-skill has no missing-ref errors", proc.stdout)
    check("\n  (none)\n" in proc.stdout, "fixtures/ok-skill reports no warnings or errors", proc.stdout)

    proc_json = run(["./fixtures/ok-skill", "--json"])
    check(proc_json.returncode == 0, f"fixtures/ok-skill --json exit {proc_json.returncode} (want 0)", proc_json.stderr)
    data = json.loads(proc_json.stdout)
    paths = [item["path"] for item in data["files"]]
    check(
        paths == [".fictional-note", "SKILL.md", "references/notes.md", "scripts/hello.py"],
        "fixtures/ok-skill lists dotfiles and nested files",
        "\n".join(paths),
    )
    check(data["warnings"] == [], "fixtures/ok-skill json warnings empty")
    check(data["errors"] == [], "fixtures/ok-skill json errors empty")
    check(data["file_count"] == len(data["files"]), "fixtures/ok-skill file_count matches")
    check(
        data["total_bytes"] == sum(item["bytes"] for item in data["files"]),
        "fixtures/ok-skill total_bytes matches file sizes",
    )
    check(data["strict_secrets"] is False, "fixtures/ok-skill strict_secrets is false")


def test_bad_fixture() -> None:
    proc = run(["./fixtures/bad-missing-ref"])
    check(
        proc.returncode == 1,
        f"fixtures/bad-missing-ref exit {proc.returncode} (want 1)",
        proc.stdout + proc.stderr,
    )
    check("references/missing-guide.md" in proc.stdout, "bad fixture points at references/missing-guide.md", proc.stdout)
    check("scripts/not-here.py" in proc.stdout, "bad fixture points at scripts/not-here.py", proc.stdout)
    check("missing relative ref" in proc.stdout, "bad fixture says missing relative ref", proc.stdout)
    check("references/present.md" not in proc.stdout.split("errors", 1)[-1], "present.md is not reported as missing", proc.stdout)

    proc_json = run(["./fixtures/bad-missing-ref", "--json"])
    check(proc_json.returncode == 1, f"fixtures/bad-missing-ref --json exit {proc_json.returncode} (want 1)")
    data = json.loads(proc_json.stdout)
    errors = data["errors"]
    refs = {item["ref"] for item in errors}
    check(refs == {"references/missing-guide.md", "scripts/not-here.py"}, "bad fixture json refs", str(refs))
    for item in errors:
        source = (ROOT / "fixtures/bad-missing-ref" / item["path"]).read_text(encoding="utf-8").splitlines()
        line = source[item["line"] - 1]
        check(item["ref"] in line, f"bad fixture line {item['line']} contains {item['ref']}", line)
        check(item["kind"] == "missing-ref", f"bad fixture kind for {item['ref']}")


def test_warn_fixture() -> None:
    env_path = ROOT / "fixtures/warn-dotenv/.env"
    raw = env_path.read_bytes()
    check(raw == b"FAKE=1\n", "warn fixture .env is exactly the fake marker", repr(raw))

    proc = run(["./fixtures/warn-dotenv"])
    check(
        proc.returncode == 0,
        f"fixtures/warn-dotenv exit {proc.returncode} (want 0)",
        proc.stdout + proc.stderr,
    )
    check("FAKE" not in proc.stdout and "FAKE" not in proc.stderr, "warn fixture output does not print .env contents", proc.stdout + proc.stderr)
    check(".env" in proc.stdout, "warn fixture lists and warns on .env", proc.stdout)
    check("secret-like filename; contents not read" in proc.stdout, "warn fixture says contents were not read", proc.stdout)
    check("rule=.env" in proc.stdout, "warn fixture names the .env rule", proc.stdout)
    size = str(env_path.lstat().st_size)
    check(size in proc.stdout, f"warn fixture prints .env size {size} without reading it", proc.stdout)

    proc_json = run(["./fixtures/warn-dotenv", "--json"])
    check(proc_json.returncode == 0, f"fixtures/warn-dotenv --json exit {proc_json.returncode} (want 0)")
    data = json.loads(proc_json.stdout)
    check("FAKE" not in proc_json.stdout, "warn fixture json does not print .env contents")
    check(len(data["warnings"]) == 1, "warn fixture has one warning", json.dumps(data["warnings"]))
    warning = data["warnings"][0]
    check(warning["path"] == ".env" and warning["rule"] == ".env", "warn fixture warning path and rule")
    check(warning["kind"] == "secret-filename", "warn fixture warning kind")
    check(data["errors"] == [], "warn fixture json errors empty")
    listed = {item["path"]: item["bytes"] for item in data["files"]}
    check(".env" in listed and "SKILL.md" in listed and "notes.md" in listed, "warn fixture keeps .env in the inventory", str(listed))
    check(listed[".env"] == env_path.lstat().st_size, "warn fixture json size matches lstat")

    strict = run(["./fixtures/warn-dotenv", "--strict-secrets"])
    check(
        strict.returncode == 1,
        f"fixtures/warn-dotenv --strict-secrets exit {strict.returncode} (want 1)",
        strict.stdout + strict.stderr,
    )
    check("FAKE" not in strict.stdout and "FAKE" not in strict.stderr, "strict mode does not print .env contents")
    check("\nerrors\n" in strict.stdout and "rule=.env" in strict.stdout, "strict mode promotes .env to an error", strict.stdout)
    strict_json = run(["./fixtures/warn-dotenv", "--strict-secrets", "--json"])
    strict_data = json.loads(strict_json.stdout)
    check(strict_json.returncode == 1, "strict json exit 1")
    check(strict_data["warnings"] == [], "strict json warnings empty")
    check(
        len(strict_data["errors"]) == 1 and strict_data["errors"][0]["path"] == ".env",
        "strict json has one .env error",
    )


def test_usage_exit_2() -> None:
    missing_args = run([])
    check(missing_args.returncode == 2, f"missing args exit {missing_args.returncode} (want 2)")

    missing_dir = run(["./fixtures/does-not-exist"])
    check(missing_dir.returncode == 2, f"missing directory exit {missing_dir.returncode} (want 2)", missing_dir.stderr)
    check("SKILL.md" in missing_dir.stderr or "not found" in missing_dir.stderr, "missing directory explains the problem", missing_dir.stderr)

    no_skill = run(["./fixtures/ok-skill/scripts"])
    check(no_skill.returncode == 2, f"directory without SKILL.md exit {no_skill.returncode} (want 2)", no_skill.stderr)
    check("SKILL.md" in no_skill.stderr, "missing SKILL.md message names SKILL.md", no_skill.stderr)

    help_proc = run(["--help"])
    check(help_proc.returncode == 0, f"--help exit {help_proc.returncode} (want 0)")
    check("--strict-secrets" in help_proc.stdout and "--json" in help_proc.stdout, "--help documents flags")
    help_text = " ".join(help_proc.stdout.split())
    check(
        "Only the root SKILL.md is opened." in help_text,
        "help says only the root SKILL.md is opened",
        help_text,
    )


def test_secret_rules_and_edges() -> None:
    """Temporary files are empty and fictional. No key material is written."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill = root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "\n".join(
                [
                    "# Fictional rule fixture",
                    "",
                    "This temporary skill is fake.",
                    "See [notes](notes.md#section) and [local](#top).",
                    "Skip [web](https://example.com/missing.md) and [mail](mailto:fake@example.com).",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (skill / "notes.md").write_text("# Fictional\n\nNo secrets here.\n", encoding="utf-8")
        expected = [
            ".env",
            ".env.local",
            "id_rsa",
            "id_rsa.pub",
            "server.pem",
            "client.p12",
            "private.key",
            "credentials.json",
            "release.keystore",
        ]
        innocent = [
            "readme.txt",
            "not_id_rsa",
            "credentials.json.bak",
            "public.pem.txt",
            ".environment",
            "notes.md",
            "SKILL.md",
        ]
        for name in expected + innocent:
            (skill / name).write_bytes(b"")
        proc = run([str(skill), "--json"])
        check(proc.returncode == 0, f"secret-rule temp skill exit {proc.returncode} (want 0)", proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        warned = {item["path"] for item in data["warnings"]}
        check(warned == set(expected), "secret rules match the v0 basename list", f"got {sorted(warned)}")
        check(data["errors"] == [], "fragment and external urls are not missing refs", json.dumps(data["errors"]))
        for name in expected:
            check((skill / name).read_bytes() == b"", f"secret-named file {name} was not modified")

        blocked = skill / ".env"
        blocked.write_text("FAKE_MARKER_DO_NOT_PRINT=1\n", encoding="utf-8")
        os.chmod(blocked, 0)
        try:
            hidden = run([str(skill)])
        finally:
            os.chmod(blocked, stat.S_IRUSR | stat.S_IWUSR)
        check(hidden.returncode == 0, f"unreadable .env still exits {hidden.returncode} (want 0)", hidden.stderr)
        check("FAKE_MARKER_DO_NOT_PRINT" not in hidden.stdout + hidden.stderr, "unreadable .env contents are not printed")
        check("rule=.env" in hidden.stdout, "unreadable .env still warns from the filename")

        outside = root / "outside.md"
        outside.write_text("UNIQUE_OUTSIDE_TOKEN\n", encoding="utf-8")
        (skill / "SKILL.md").write_text("See [out](../outside.md).\n", encoding="utf-8")
        escaped = run([str(skill), "--json"])
        check(escaped.returncode == 1, f"escaped ref exit {escaped.returncode} (want 1)", escaped.stdout + escaped.stderr)
        payload = json.loads(escaped.stdout)
        kinds = {item["kind"] for item in payload["errors"]}
        check("escaped-ref" in kinds, "parent path is an escaped ref", json.dumps(payload["errors"]))
        check("UNIQUE_OUTSIDE_TOKEN" not in escaped.stdout, "escaped target contents are not printed")


def test_combined_strict_and_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        skill = Path(tmp) / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "Fake skill. See [gone](missing.md).\n",
            encoding="utf-8",
        )
        (skill / ".env").write_text("FAKE=1\n", encoding="utf-8")
        default = run([str(skill), "--json"])
        data = json.loads(default.stdout)
        check(default.returncode == 1, f"combined default exit {default.returncode} (want 1)")
        check(len(data["warnings"]) == 1 and len(data["errors"]) == 1, "combined default keeps warning and ref error")
        check("FAKE" not in default.stdout, "combined default does not print .env")
        strict = run([str(skill), "--strict-secrets", "--json"])
        strict_data = json.loads(strict.stdout)
        check(strict.returncode == 1, "combined strict exit 1")
        check(strict_data["warnings"] == [], "combined strict clears warnings")
        check(len(strict_data["errors"]) == 2, "combined strict reports secret and missing ref")


def _norm(path: str | Path) -> str:
    return os.path.normpath(os.fspath(path))


def _was_opened(opened: list[str], path: Path) -> bool:
    wanted = _norm(path)
    return any(_norm(item) == wanted for item in opened)


class _OpenTrace:
    """Record os.open and builtin open paths, including when the body raises."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self._real_os_open = os.open
        self._real_builtin_open = builtins.open

    def __enter__(self) -> "_OpenTrace":
        opened = self.opened
        real_os_open = self._real_os_open
        real_builtin_open = self._real_builtin_open

        def traced_os_open(path, flags, *args, **kwargs):
            if not isinstance(path, int):
                opened.append(os.fsdecode(path))
            return real_os_open(path, flags, *args, **kwargs)

        def traced_builtin_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, Path)):
                opened.append(os.fsdecode(file))
            return real_builtin_open(file, *args, **kwargs)

        os.open = traced_os_open
        builtins.open = traced_builtin_open
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        os.open = self._real_os_open
        builtins.open = self._real_builtin_open
        return False


def _lock(path: Path) -> None:
    os.chmod(path, 0)


def _unlock(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _assert_marker_unread(proc: subprocess.CompletedProcess[str], marker: str, label: str) -> None:
    combined = proc.stdout + proc.stderr
    check(marker not in combined, f"{label} marker is absent from output", combined)
    check("Permission denied" not in combined, f"{label} did not fail by reading an unreadable target", combined)


def test_root_skill_md_only_and_symlinks() -> None:
    """Other markdown is listed, not opened. Symlink targets are not read."""
    selected = markdown_files(
        ["SKILL.md", "notes.md", "references/notes.md", "references/SKILL.md"]
    )
    check(selected == ["SKILL.md"], "markdown_files selects only the root SKILL.md", str(selected))
    check(
        markdown_files(["notes.md", "references/notes.md"]) == [],
        "markdown_files ignores markdown other than the root SKILL.md",
    )

    with tempfile.TemporaryDirectory() as tmp:
        skill = Path(tmp) / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "# Fictional skill\n\nSee [notes](notes.md).\n",
            encoding="utf-8",
        )
        notes = skill / "notes.md"
        notes_marker = "UNIQUE_NOTES_BODY_7f3a"
        notes.write_text(
            f"{notes_marker}\nSee [gone](missing-from-notes-7f3a.md).\n",
            encoding="utf-8",
        )
        _lock(notes)
        try:
            proc = run([str(skill), "--json"])
            with _OpenTrace() as trace:
                result = inspect_skill(skill)
            opened = trace.opened
        finally:
            _unlock(notes)
        check(proc.returncode == 0, f"unreadable notes.md exit {proc.returncode} (want 0)", proc.stdout + proc.stderr)
        _assert_marker_unread(proc, notes_marker, "notes.md")
        check("missing-from-notes-7f3a.md" not in proc.stdout + proc.stderr, "notes.md refs are not scanned", proc.stdout)
        check(not _was_opened(opened, notes), "notes.md path was not opened", "\n".join(opened))
        check(_was_opened(opened, skill / "SKILL.md"), "root SKILL.md was opened", "\n".join(opened))
        listed = [item.path for item in result[0]]
        check("notes.md" in listed and "SKILL.md" in listed, "unread notes.md stays in the inventory", str(listed))
        check(result[2] == [], "root SKILL.md refs resolve without reading notes.md")

    probe: Path | None = None
    try:
        probe = Path(tempfile.mkdtemp())
        os.symlink(probe / "target", probe / "link")
    except (OSError, NotImplementedError) as err:
        check(False, f"symlink fixtures are not supported here: {err}")
        return
    finally:
        if probe is not None and probe.exists():
            import shutil
            shutil.rmtree(probe, ignore_errors=True)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill = root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# Fictional skill\n\nNo local refs.\n", encoding="utf-8")
        outside = root / "outside-notes.md"
        md_marker = "UNIQUE_MD_LINK_BODY_7f3a"
        outside.write_text(
            f"{md_marker}\nSee [gone](missing-via-md-symlink-7f3a.md).\n" + ("x" * 64),
            encoding="utf-8",
        )
        link = skill / "notes.md"
        os.symlink(outside, link)
        _lock(outside)
        try:
            proc = run([str(skill), "--json"])
            with _OpenTrace() as trace:
                result = inspect_skill(skill)
            opened = trace.opened
        finally:
            _unlock(outside)
        check(proc.returncode == 0, f"md symlink exit {proc.returncode} (want 0)", proc.stdout + proc.stderr)
        _assert_marker_unread(proc, md_marker, "md symlink")
        check(
            "missing-via-md-symlink-7f3a.md" not in proc.stdout + proc.stderr,
            "md symlink target was not scanned",
            proc.stdout,
        )
        check(not _was_opened(opened, outside), "md symlink target path was not opened", "\n".join(opened))
        check(not _was_opened(opened, link), "md symlink path was not opened", "\n".join(opened))
        check(_was_opened(opened, skill / "SKILL.md"), "regular root SKILL.md still opens beside an md symlink")
        data = json.loads(proc.stdout)
        listed = {item["path"]: item["bytes"] for item in data["files"]}
        check("notes.md" in listed, "md symlink is listed in the inventory")
        check(
            listed["notes.md"] == link.lstat().st_size,
            "md symlink byte size is the lstat size",
            str(listed),
        )
        check(
            listed["notes.md"] != outside.stat().st_size,
            "md symlink size is not the outside target size",
        )
        check(result[2] == [], "md symlink does not add ref errors")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill = root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# Fictional skill\n\nNo local refs.\n", encoding="utf-8")
        outside_dir = root / "outside-dir"
        outside_dir.mkdir()
        marker_file = outside_dir / "secret.md"
        dir_marker = "UNIQUE_DIR_LINK_BODY_7f3a"
        marker_file.write_text(
            f"{dir_marker}\nSee [gone](missing-via-dir-symlink-7f3a.md).\n",
            encoding="utf-8",
        )
        os.symlink(outside_dir, skill / "ext", target_is_directory=True)
        _lock(marker_file)
        try:
            proc = run([str(skill), "--json"])
            with _OpenTrace() as trace:
                inspect_skill(skill)
            opened = trace.opened
        finally:
            _unlock(marker_file)
        check(proc.returncode == 0, f"directory symlink exit {proc.returncode} (want 0)", proc.stdout + proc.stderr)
        _assert_marker_unread(proc, dir_marker, "directory symlink")
        combined = proc.stdout + proc.stderr
        check("ext/secret.md" not in combined, "directory symlink is not expanded in the inventory", combined)
        check("missing-via-dir-symlink-7f3a.md" not in combined, "directory symlink target was not scanned", combined)
        check(not _was_opened(opened, marker_file), "directory symlink target file was not opened", "\n".join(opened))
        check(not _was_opened(opened, outside_dir), "outside directory was not opened", "\n".join(opened))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill = root / "skill"
        skill.mkdir()
        outside_skill = root / "outside-skill.md"
        skill_marker = "UNIQUE_SKILL_LINK_BODY_7f3a"
        outside_skill.write_text(
            f"{skill_marker}\nSee [gone](missing-via-skill-symlink-7f3a.md).\n",
            encoding="utf-8",
        )
        os.symlink(outside_skill, skill / "SKILL.md")
        _lock(outside_skill)
        raised = ""
        opened: list[str] = []
        try:
            proc = run([str(skill)])
            with _OpenTrace() as trace:
                try:
                    inspect_skill(skill)
                except PreviewIOError as err:
                    raised = str(err)
            opened = trace.opened
        finally:
            _unlock(outside_skill)
        check(proc.returncode == 2, f"symlink SKILL.md exit {proc.returncode} (want 2)", proc.stdout + proc.stderr)
        _assert_marker_unread(proc, skill_marker, "symlink SKILL.md")
        check("symlink" in proc.stderr, "symlink SKILL.md error names the symlink", proc.stderr)
        check(
            "missing-via-skill-symlink-7f3a.md" not in proc.stdout + proc.stderr,
            "symlink SKILL.md target was not scanned",
            proc.stdout + proc.stderr,
        )
        check("symlink" in raised, "inspect_skill rejects a symlink root SKILL.md", raised)
        check(not _was_opened(opened, outside_skill), "symlink SKILL.md target was not opened", "\n".join(opened))
        check(not _was_opened(opened, skill / "SKILL.md"), "symlink SKILL.md path was not opened", "\n".join(opened))


def test_package_has_no_network_imports() -> None:
    banned = {
        "socket",
        "urllib",
        "http",
        "requests",
        "httpx",
        "ftplib",
        "smtplib",
        "telnetlib",
        "webbrowser",
        "xmlrpc",
        "subprocess",
        "hashlib",
    }
    package = ROOT / "skill_bundle_preview"
    found: set[str] = set()
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".", 1)[0])
    used = sorted(found & banned)
    check(not used, "package does not import network, subprocess, or hashlib", ", ".join(used))


def main() -> int:
    before = snapshot_fixtures()
    test_ok_fixture()
    test_bad_fixture()
    test_warn_fixture()
    test_usage_exit_2()
    test_secret_rules_and_edges()
    test_combined_strict_and_missing()
    test_root_skill_md_only_and_symlinks()
    test_package_has_no_network_imports()
    after = snapshot_fixtures()
    check(before == after, "fixture files are not modified or deleted")
    if FAILURES:
        print(f"check.py: {len(FAILURES)} failed")
        return 1
    print("check.py: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
