# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.kb.file_walker`.

Coverage matrix:

* Slug from filename stem (the dominant path) -- works on the
  consumer's dotted versioned filenames.
* Front-matter ``slug`` override wins over filename stem.
* Front-matter ``slug`` non-string / empty falls back to filename.
* Files without front-matter ingest cleanly (metadata = {}).
* Hidden files (``.foo.md`` / ``.git/foo.md``) are skipped.
* ``.kb-ignore`` patterns at root skip matching files; component-only
  patterns skip whole directories; comments / blanks ignored.
* Nested directories surface via recursive walk.
* Strict mode (``errors is None``) propagates per-file failures.
* Best-effort mode (``errors=[...]``) catches per-file failures and
  appends to the list; iteration continues past the bad file.
* Invalid slug from front-matter override surfaces as InvalidKbSlugError.
* Malformed YAML in front-matter surfaces as KbFileParseError chained
  from the underlying yaml.YAMLError.
* Only regular files inside the root are read: a linked file, a linked
  folder, a pipe and a socket are skipped, and so is a file whose real
  location is outside the root. A linked ``.kb-ignore`` is not read.
"""

from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from meho_backplane.kb import file_walker
from meho_backplane.kb.file_walker import (
    KB_IGNORE_FILENAME,
    KbFileParseError,
    walk_kb_directory,
)
from meho_backplane.kb.schemas import InvalidKbSlugError


def _write(path: Path, content: str) -> None:
    """Write *content* to *path*, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _slugs(root: Path, errors: list[str] | None = None) -> list[str]:
    """Helper -- collect slugs from a walk, sorted for stable comparison."""
    return sorted(record.slug for record in walk_kb_directory(root, errors=errors))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_walk_yields_slug_from_filename_stem(tmp_path: Path) -> None:
    """``vcenter-9.0-snapshot-revert.md`` → slug ``vcenter-9.0-snapshot-revert``."""
    _write(
        tmp_path / "vcenter-9.0-snapshot-revert.md",
        "How to revert a vcenter snapshot.",
    )
    records = list(walk_kb_directory(tmp_path))
    assert len(records) == 1
    record = records[0]
    assert record.slug == "vcenter-9.0-snapshot-revert"
    assert "vcenter snapshot" in record.body
    assert record.metadata == {}
    assert record.path == (tmp_path / "vcenter-9.0-snapshot-revert.md").resolve()


def test_front_matter_slug_override_wins_over_filename(tmp_path: Path) -> None:
    """Non-empty string ``slug:`` in front-matter overrides the filename stem."""
    _write(
        tmp_path / "old-name.md",
        "---\nslug: new-canonical-slug\ntag: foo\n---\nBody after front-matter.\n",
    )
    records = list(walk_kb_directory(tmp_path))
    assert len(records) == 1
    record = records[0]
    assert record.slug == "new-canonical-slug"
    # Front-matter stripped from body
    assert record.body.strip() == "Body after front-matter."
    assert record.metadata == {"slug": "new-canonical-slug", "tag": "foo"}


def test_empty_front_matter_slug_falls_back_to_filename(tmp_path: Path) -> None:
    """``slug: <empty>`` falls back to the filename stem."""
    _write(
        tmp_path / "filename-slug.md",
        "---\nslug: ''\n---\nBody.\n",
    )
    records = list(walk_kb_directory(tmp_path))
    assert records[0].slug == "filename-slug"


def test_non_string_front_matter_slug_falls_back_to_filename(tmp_path: Path) -> None:
    """``slug: 123`` (non-string) falls back to the filename stem."""
    _write(
        tmp_path / "filename-slug.md",
        "---\nslug: 123\n---\nBody.\n",
    )
    records = list(walk_kb_directory(tmp_path))
    assert records[0].slug == "filename-slug"


def test_walk_recurses_into_subdirectories(tmp_path: Path) -> None:
    """Files in nested directories surface alongside root-level files."""
    _write(tmp_path / "top.md", "Top-level.")
    _write(tmp_path / "nested" / "deep.md", "Nested.")
    _write(tmp_path / "nested" / "deeper" / "more.md", "Even deeper.")
    assert _slugs(tmp_path) == ["deep", "more", "top"]


# ---------------------------------------------------------------------------
# Hidden-file skip
# ---------------------------------------------------------------------------


def test_hidden_files_are_skipped(tmp_path: Path) -> None:
    """Dot-prefixed files and dot-prefixed directory components are skipped."""
    _write(tmp_path / "kept.md", "Kept.")
    _write(tmp_path / ".swap-file.md", "Editor swap.")
    _write(tmp_path / ".git" / "objects" / "abc.md", "VCS internals.")
    _write(tmp_path / ".trash" / "deleted.md", "Trashed.")
    assert _slugs(tmp_path) == ["kept"]


# ---------------------------------------------------------------------------
# .kb-ignore
# ---------------------------------------------------------------------------


def test_kb_ignore_skips_matching_files(tmp_path: Path) -> None:
    """Glob patterns in ``.kb-ignore`` exclude matching files from the walk."""
    _write(tmp_path / "kept.md", "Kept.")
    _write(tmp_path / "todo.md", "TODO.")
    _write(tmp_path / "drafts" / "draft-one.md", "Draft.")
    _write(tmp_path / "drafts" / "draft-two.md", "Draft.")
    _write(
        tmp_path / KB_IGNORE_FILENAME,
        "# operator config\ntodo.md\ndrafts\n",
    )
    assert _slugs(tmp_path) == ["kept"]


def test_kb_ignore_full_path_pattern_works(tmp_path: Path) -> None:
    """Patterns like ``drafts/*`` match files under the named directory."""
    _write(tmp_path / "kept.md", "Kept.")
    _write(tmp_path / "drafts" / "x.md", "X.")
    _write(tmp_path / KB_IGNORE_FILENAME, "drafts/*\n")
    assert _slugs(tmp_path) == ["kept"]


def test_kb_ignore_comments_and_blanks_are_skipped(tmp_path: Path) -> None:
    """``#`` comments and blank lines in ``.kb-ignore`` produce no patterns."""
    _write(tmp_path / "kept.md", "Kept.")
    _write(
        tmp_path / KB_IGNORE_FILENAME,
        "# this is a comment\n\n  \n#another\n",
    )
    assert _slugs(tmp_path) == ["kept"]


def test_kb_ignore_missing_means_no_filtering(tmp_path: Path) -> None:
    """Walking a directory without ``.kb-ignore`` works unchanged."""
    _write(tmp_path / "a.md", "A.")
    _write(tmp_path / "b.md", "B.")
    assert _slugs(tmp_path) == ["a", "b"]


# ---------------------------------------------------------------------------
# Strict vs best-effort
# ---------------------------------------------------------------------------


def test_strict_mode_propagates_invalid_slug(tmp_path: Path) -> None:
    """Without an errors list the bad slug raises out of the walker."""
    # File name with mixed case → slug validation fails.
    _write(tmp_path / "BadCase.md", "Body.")
    with pytest.raises(InvalidKbSlugError):
        list(walk_kb_directory(tmp_path))


def test_strict_mode_propagates_yaml_parse_error(tmp_path: Path) -> None:
    """Without an errors list a malformed front-matter raises out of the walker."""
    _write(
        tmp_path / "bad-yaml.md",
        "---\n: : : :\n---\nBody.\n",
    )
    with pytest.raises(KbFileParseError):
        list(walk_kb_directory(tmp_path))


def test_best_effort_mode_continues_past_bad_files(tmp_path: Path) -> None:
    """With an errors list, per-file failures are appended and iteration continues."""
    _write(tmp_path / "good-one.md", "Good.")
    _write(tmp_path / "BadCase.md", "Body.")  # invalid slug
    _write(tmp_path / "bad-yaml.md", "---\n: : : :\n---\nBody.\n")  # parse error
    _write(tmp_path / "good-two.md", "Good.")

    errors: list[str] = []
    records = list(walk_kb_directory(tmp_path, errors=errors))
    slugs = sorted(r.slug for r in records)

    assert slugs == ["good-one", "good-two"]
    assert len(errors) == 2
    assert any("BadCase" in e for e in errors)
    assert any("bad-yaml" in e for e in errors)


def test_best_effort_mode_handles_binary_file(tmp_path: Path) -> None:
    """A binary file with .md extension is caught + skipped in best-effort mode."""
    (tmp_path / "binary.md").write_bytes(b"\x00\x01\x02\xff\xfe")
    _write(tmp_path / "text.md", "Text.")

    errors: list[str] = []
    records = list(walk_kb_directory(tmp_path, errors=errors))
    slugs = sorted(r.slug for r in records)

    assert slugs == ["text"]
    assert len(errors) == 1
    assert "binary.md" in errors[0]


def test_unreadable_file_error_uses_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that cannot be read is reported by its relative path only."""
    _write(tmp_path / "sub" / "locked.md", "Body.")
    _write(tmp_path / "text.md", "Text.")
    locked = (tmp_path / "sub" / "locked.md").resolve()
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == locked:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    errors: list[str] = []
    slugs = _slugs(tmp_path, errors=errors)

    assert slugs == ["text"]
    assert errors == ["sub/locked.md: could not read sub/locked.md: Permission denied"]
    assert str(tmp_path.resolve()) not in errors[0]

    with pytest.raises(PermissionError) as exc_info:
        list(walk_kb_directory(tmp_path))
    assert str(tmp_path.resolve()) not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Regular files inside the root only
# ---------------------------------------------------------------------------


def _make_special_files(folder: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Create a pipe and a socket named ``*.md`` in *folder* where the platform allows.

    Returns the names it created. The socket is bound by a short
    relative name, because socket paths have a small length limit.
    """
    made: list[str] = []
    if hasattr(os, "mkfifo"):
        os.mkfifo(folder / "pipe.md")
        made.append("pipe.md")
    if hasattr(socket, "AF_UNIX"):
        monkeypatch.chdir(folder)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind("socket.md")
        made.append("socket.md")
    return made


def _skipped(logs: list[dict[str, object]]) -> dict[object, object]:
    """Map path to reason for every ``kb_file_skipped`` log line."""
    return {entry["path"]: entry["reason"] for entry in logs if entry["event"] == "kb_file_skipped"}


def test_only_regular_files_inside_the_root_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked file, a linked folder, a pipe and a socket are skipped, not read."""
    root = tmp_path / "kb"
    elsewhere = tmp_path / "elsewhere"
    _write(root / "regular.md", "Regular.")
    _write(elsewhere / "note.md", "Outside the root.")
    _write(elsewhere / "folder" / "deep.md", "Outside the root, in a folder.")
    (root / "linked-file.md").symlink_to(elsewhere / "note.md")
    (root / "linked-folder").symlink_to(elsewhere / "folder", target_is_directory=True)
    special = _make_special_files(root, monkeypatch)

    errors: list[str] = []
    with capture_logs() as logs:
        records = list(walk_kb_directory(root, errors=errors))

    assert [record.relative_path for record in records] == ["regular.md"]
    assert records[0].body == "Regular."
    assert errors == []
    # The linked folder is never entered, so it gets no log line.
    assert _skipped(logs) == {
        "linked-file.md": "link",
        **dict.fromkeys(special, "not a regular file"),
    }


def test_linked_file_pointing_inside_the_root_is_skipped(tmp_path: Path) -> None:
    """A link is skipped even when it points at a file inside the root."""
    _write(tmp_path / "regular.md", "Regular.")
    (tmp_path / "alias.md").symlink_to(tmp_path / "regular.md")
    assert _slugs(tmp_path) == ["regular"]


def test_file_whose_real_path_is_outside_the_root_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regular file whose real location is outside the root is not read.

    The walk does not enter linked folders, so this test replaces the
    listing to hand the walker a path through one, as if the folder had
    turned into a link after it was listed. The file at the end of that
    path is a regular file, so only the real-location check stops it.
    """
    root = tmp_path / "kb"
    _write(root / "regular.md", "Regular.")
    _write(tmp_path / "elsewhere" / "note.md", "Outside the root.")
    (root / "folder").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    real_root = root.resolve()
    through_link = real_root / "folder" / "note.md"
    assert through_link.is_file()
    listed = [("folder/note.md", through_link), ("regular.md", real_root / "regular.md")]
    monkeypatch.setattr(file_walker, "_list_markdown_files", lambda _root: listed)

    errors: list[str] = []
    with capture_logs() as logs:
        records = list(walk_kb_directory(root, errors=errors))

    assert [record.relative_path for record in records] == ["regular.md"]
    assert errors == []
    assert _skipped(logs) == {"folder/note.md": "outside the root"}


def test_linked_kb_ignore_is_not_read(tmp_path: Path) -> None:
    """A ``.kb-ignore`` that is a link is skipped, so its patterns do not apply."""
    root = tmp_path / "kb"
    _write(root / "kept.md", "Kept.")
    _write(root / "drafts" / "draft-one.md", "Draft.")
    _write(tmp_path / "patterns.txt", "drafts\n")
    (root / KB_IGNORE_FILENAME).symlink_to(tmp_path / "patterns.txt")

    with capture_logs() as logs:
        slugs = _slugs(root)

    assert slugs == ["draft-one", "kept"]
    assert _skipped(logs) == {KB_IGNORE_FILENAME: "link"}


# ---------------------------------------------------------------------------
# Root-path validation
# ---------------------------------------------------------------------------


def test_missing_root_directory_raises_not_a_directory(tmp_path: Path) -> None:
    """A non-existent or non-directory root surfaces a :class:`NotADirectoryError`."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(NotADirectoryError):
        list(walk_kb_directory(missing))


def test_file_path_root_raises_not_a_directory(tmp_path: Path) -> None:
    """Pointing the walker at a single file rather than a directory fails fast."""
    file_path = tmp_path / "file.md"
    file_path.write_text("body", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        list(walk_kb_directory(file_path))
