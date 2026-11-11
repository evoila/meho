# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/check_tested_live_claims.py``.

The script fails when a page or the release notes say a connector version was
tested live and the live test record has no such test. The tests run it on
small throw-away trees, plus once on this checkout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_tested_live_claims.py"
_CHANGELOG = "## [Unreleased]\n\n## [0.46.0] - 2026-11-09\n\n## [0.45.0] - 2026-11-06\n"
_RECORD_HEAD = "schema_version: 1\nlevels:\n  read: r\n  change: c\nstale_after_minor_releases: 5\n"
_RECORD_WITH_ROW = (
    _RECORD_HEAD + "connectors:\n"
    "  nsx-rest-4.2:\n"
    "    product: NSX\n"
    "    tests:\n"
    '      - product_version: "4.2.1"\n'
    "        level: read\n"
    "        date: 2026-11-07\n"
    '        meho_release: "0.45.0"\n'
)
_CLAIM = "We tested live on `nsx-rest-4.2` 4.2.1 last week.\n"


def _tree(tmp_path: Path, *, record: str, page: str, changelog: str = _CHANGELOG) -> Path:
    (tmp_path / "docs/compatibility").mkdir(parents=True)
    (tmp_path / "docs/compatibility/live-tests.yaml").write_text(record, encoding="utf-8")
    (tmp_path / "docs-site").mkdir()
    (tmp_path / "docs-site/page.md").write_text(page, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_claim_with_a_record_row_passes(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path, record=_RECORD_WITH_ROW, page=f"# Page\n\n{_CLAIM}"))
    assert result.returncode == 0, result.stderr


def test_claim_without_a_record_row_fails_with_file_and_line(tmp_path: Path) -> None:
    result = _run(
        _tree(tmp_path, record=_RECORD_HEAD + "connectors: {}\n", page=f"# Page\n\n{_CLAIM}")
    )
    assert result.returncode == 1
    assert "docs-site/page.md:3" in result.stderr
    assert "add a row to docs/compatibility/live-tests.yaml" in result.stderr


def test_claim_with_the_wrong_version_fails(tmp_path: Path) -> None:
    page = "Tested live on `nsx-rest-4.2` 4.3.\n"
    assert _run(_tree(tmp_path, record=_RECORD_WITH_ROW, page=page)).returncode == 1


@pytest.mark.parametrize(
    "page",
    [
        "Tested live on `nsx-rest-4.2` 4.2.1 and 4.2.2.\n",
        "Tested live on `nsx-rest-4.2` 4.2.1-rc1.\n",
        "Tested live on `nsx-rest-4.2` 4.2.1.5.\n",
        "Tested live on `nsx-rest-4.20` 4.2.1.\n",
        "Tested live on `my-nsx-rest-4.2` 4.2.1.\n",
        "Tested live on `nsx-rest-4.2` 4.2.1 and `vmware-rest-9.0` 9.0.1.\n",
        "Tested live on `nsx-rest-4.2`.\n",
    ],
)
def test_claim_with_an_unrecorded_pair_fails(tmp_path: Path, page: str) -> None:
    assert _run(_tree(tmp_path, record=_RECORD_WITH_ROW, page=page)).returncode == 1


@pytest.mark.parametrize(
    "page",
    [
        "Tested live on `nsx-rest-4.2` v4.2.1.\n",
        "Tested live: 4.2.1 on `nsx-rest-4.2`.\n",
        "Tested live on `nsx-rest-4.2` 4.2.1 and 4.2.2.\n",
    ],
)
def test_claim_with_only_recorded_pairs_passes(tmp_path: Path, page: str) -> None:
    record = _RECORD_WITH_ROW + (
        '      - product_version: "4.2.2"\n'
        "        level: read\n"
        "        date: 2026-11-07\n"
        '        meho_release: "0.45.0"\n'
    )
    result = _run(_tree(tmp_path, record=record, page=page))
    assert result.returncode == 0, result.stderr


def test_claim_with_a_row_without_tests_fails(tmp_path: Path) -> None:
    record = _RECORD_HEAD + "connectors:\n  nsx-rest-4.2:\n    product: NSX\n    tests: []\n"
    assert _run(_tree(tmp_path, record=record, page=_CLAIM)).returncode == 1


def test_claim_inside_a_generated_block_is_skipped(tmp_path: Path) -> None:
    page = f"<!-- live-tests:start -->\n{_CLAIM}<!-- live-tests:end -->\nDone.\n"
    result = _run(_tree(tmp_path, record=_RECORD_HEAD + "connectors: {}\n", page=page))
    assert result.returncode == 0, result.stderr


def test_claim_after_a_generated_block_is_scanned(tmp_path: Path) -> None:
    page = f"<!-- live-tests:start -->\nx\n<!-- live-tests:end -->\n{_CLAIM}"
    result = _run(_tree(tmp_path, record=_RECORD_HEAD + "connectors: {}\n", page=page))
    assert result.returncode == 1


def test_generated_inventory_page_is_skipped(tmp_path: Path) -> None:
    root = _tree(tmp_path, record=_RECORD_HEAD + "connectors: {}\n", page="Fine.\n")
    (root / "docs-site/reference").mkdir()
    (root / "docs-site/reference/connectors.md").write_text(_CLAIM, encoding="utf-8")
    assert _run(root).returncode == 0


@pytest.mark.parametrize(
    "phrase",
    ["tested live", "Live-tested", "LIVE TESTED", "tested on a real system"],
)
def test_every_phrase_is_a_claim(tmp_path: Path, phrase: str) -> None:
    root = _tree(tmp_path, record=_RECORD_HEAD + "connectors: {}\n", page=f"It was {phrase}.\n")
    assert _run(root).returncode == 1


def test_claim_in_the_newest_changelog_section_fails(tmp_path: Path) -> None:
    changelog = (
        f"## [Unreleased]\n\n## [0.46.0] - 2026-11-09\n\n- {_CLAIM}\n## [0.45.0] - 2026-11-06\n"
    )
    root = _tree(
        tmp_path, record=_RECORD_HEAD + "connectors: {}\n", page="Fine.\n", changelog=changelog
    )
    result = _run(root)
    assert result.returncode == 1
    assert "CHANGELOG.md:5" in result.stderr


def test_claim_in_an_old_changelog_section_is_not_scanned(tmp_path: Path) -> None:
    changelog = _CHANGELOG + f"\n- {_CLAIM}\n"
    root = _tree(
        tmp_path, record=_RECORD_HEAD + "connectors: {}\n", page="Fine.\n", changelog=changelog
    )
    assert _run(root).returncode == 0


def test_a_bad_record_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, record="schema_version: 2\n", page="Fine.\n")
    assert _run(root).returncode == 1


def test_a_missing_record_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, record="", page="Fine.\n")
    (root / "docs/compatibility/live-tests.yaml").unlink()
    result = _run(root)
    assert result.returncode == 1
    assert "cannot read" in result.stderr


def test_this_checkout_passes() -> None:
    result = _run(_REPO_ROOT)
    assert result.returncode == 0, result.stderr
