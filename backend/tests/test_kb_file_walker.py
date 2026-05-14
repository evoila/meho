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
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
