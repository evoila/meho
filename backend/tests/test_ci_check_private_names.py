# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/check_private_names.py``.

The script runs as a subprocess against a small throw-away git repository.
The word list is made up (``forbiddenword-a``). The tests prove that a hit
names the file and the line, and that the output never holds the word.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_private_names.py"
_WORD_A = "forbiddenword-a"
_WORD_B = "forbidden-b[0-9]+"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(
        tmp_path, {"old.txt": f"this old line has {_WORD_A} in it\n", "CHANGELOG.md": "# log\n"}
    )
    return tmp_path


def _run(root: Path, word_list: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PRIVATE_NAME_LIST"}
    if word_list is not None:
        env["PRIVATE_NAME_LIST"] = word_list
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "HEAD~1", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hit_names_file_and_line_but_not_the_word(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"src/app.txt": f"first\nsecond\nthird has {_WORD_A.upper()} here\n"})
    result = _run(root, f"{_WORD_A}\n{_WORD_B}\n")
    assert result.returncode == 1
    assert "src/app.txt:3: this line holds a word of the private name list" in result.stdout
    output = result.stdout + result.stderr
    assert "forbidden" not in output.lower()
    assert "third has" not in output


def test_pattern_entry_matches_and_line_numbers_follow_hunks(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "a.txt").write_text("keep\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base file")
    _commit(root, {"a.txt": "keep\nclean\nforbidden-b42 here\n", "b.txt": "ok\n"})
    result = _run(root, _WORD_B)
    assert result.returncode == 1
    assert "a.txt:3:" in result.stdout
    assert "b.txt" not in result.stdout


def test_clean_change_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"src/app.txt": "nothing special\n"})
    result = _run(root, f"{_WORD_A}\n")
    assert result.returncode == 0
    assert "No added line holds a word" in result.stdout


def test_old_lines_and_removed_lines_do_not_count(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"old.txt": "the word is gone now\n"})
    assert _run(root, f"{_WORD_A}\n").returncode == 0


def test_changelog_is_not_checked(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"CHANGELOG.md": f"# log\n- {_WORD_A}\n"})
    assert _run(root, f"{_WORD_A}\n").returncode == 0


def test_added_line_that_looks_like_a_file_header_does_not_hide_later_lines(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _commit(root, {"new.txt": f"++ b/CHANGELOG.md\nnext line {_WORD_A}\n"})
    done = _run(root, f"{_WORD_A}\n")
    assert done.returncode == 1
    assert "new.txt:2:" in done.stdout


def test_path_with_special_characters_is_reported_by_its_own_name(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {'odd "name".txt': f"line {_WORD_A}\n"})
    done = _run(root, f"{_WORD_A}\n")
    assert done.returncode == 1
    assert 'odd "name".txt:1:' in done.stdout


def test_quoted_path_of_changelog_look_alike_is_still_checked(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {'a"b.txt': "clean\n", "other.txt": f"x {_WORD_A}\n"})
    done = _run(root, f"{_WORD_A}\n")
    assert done.returncode == 1
    assert "other.txt:1:" in done.stdout


def test_missing_list_passes_with_a_note(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"src/app.txt": f"{_WORD_A}\n"})
    for value in (None, "", "\n \n"):
        result = _run(root, value)
        assert result.returncode == 0
        assert "not set" in result.stdout


def test_missing_list_fails_when_it_is_required(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"src/app.txt": "clean\n"})
    result = _run(root, None, "--require-list")
    assert result.returncode == 1
    assert "PRIVATE_NAME_LIST" in result.stderr


def test_bad_pattern_fails_without_showing_the_list(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"src/app.txt": "clean\n"})
    result = _run(root, "forbidden(unclosed\n")
    assert result.returncode == 2
    assert "forbidden" not in (result.stdout + result.stderr)


def test_unknown_base_is_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "no-such-rev"],
        cwd=root,
        env={**os.environ, "PRIVATE_NAME_LIST": _WORD_A},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    "separator",
    ["\x0c", "\x1c", "\x1d", "\x1e", "\x85", chr(0x2028), chr(0x2029), "\x0b", "\r"],
)
def test_line_separator_characters_do_not_hide_a_later_added_line(
    tmp_path: Path, separator: str
) -> None:
    root = _repo(tmp_path)
    text = f"ok{separator}more\nclean line\nnext line {_WORD_A}\n"
    _commit(root, {"sep.txt": text})
    done = _run(root, f"{_WORD_A}\n")
    assert done.returncode == 1
    assert "sep.txt:3:" in done.stdout


def test_separator_in_a_later_hunk_keeps_line_numbers_right(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"two.txt": "a\nb\nc\nd\ne\nf\n"})
    _commit(root, {"two.txt": f"a\nx\x0cy\nc\nd\ne\nf {_WORD_A}\n"})
    done = _run(root, f"{_WORD_A}\n")
    assert done.returncode == 1
    assert "two.txt:6:" in done.stdout


def test_no_newline_at_end_of_file_and_invalid_bytes_are_handled(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "bin.txt").write_bytes(b"\xff\xfe bad\nlast " + _WORD_A.encode())
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    done = _run(root, f"{_WORD_A}\n")
    assert done.returncode == 1
    assert "bin.txt:2:" in done.stdout


def test_rename_with_edit_and_noprefix_config_are_checked(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "config", "diff.noprefix", "true")
    _git(root, "mv", "old.txt", "moved.txt")
    (root / "moved.txt").write_text("keep\nnew forbidden-b7\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    done = _run(root, f"{_WORD_B}\n")
    assert done.returncode == 1
    assert "moved.txt:2:" in done.stdout
