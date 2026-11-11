# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard for the live test record, ``docs/compatibility/live-tests.yaml``."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.generate_reference_docs import _ENTERPRISE_PACK, _eager_import_connectors
from scripts.validate_live_tests import validate_live_tests

from meho_backplane.connectors.registry import all_connectors_v2

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs" / "compatibility" / "live-tests.yaml"
SCRIPT = ROOT / "backend" / "scripts" / "validate_live_tests.py"
TODAY = dt.date(2026, 11, 11)
FAKE_CHANGELOG = "## [Unreleased]\n\n## [0.46.0] - 2026-11-09\n\n## [0.45.0] - 2026-11-06\n"

GOOD: dict[str, Any] = {
    "schema_version": 1,
    "levels": {"read": "r", "change": "c"},
    "stale_after_minor_releases": 5,
    "connectors": {
        "demo-rest-1.0": {
            "product": "Demo Product",
            "tests": [
                {
                    "product_version": "1.2.3",
                    "level": "change",
                    "date": dt.date(2026, 11, 7),
                    "meho_release": "0.45.0",
                }
            ],
        },
        "demo-rest-2.0": {"product": "Demo Product", "tests": []},
    },
}


def _problems(data: dict[str, Any], raw: str = "") -> list[str]:
    return validate_live_tests(data, raw, FAKE_CHANGELOG, TODAY)


def _with_test(**changes: object) -> dict[str, Any]:
    data = deepcopy(GOOD)
    data["connectors"]["demo-rest-1.0"]["tests"][0].update(changes)
    return data


def test_good_file_passes() -> None:
    assert _problems(GOOD) == []


def test_checked_in_record_passes_the_script() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_script_exits_non_zero_on_a_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nconnectors: []\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(bad)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "connectors must be a mapping" in result.stderr


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_with_test(level="write"), "level must be one of"),
        (_with_test(level=None), "level must be one of"),
        (_with_test(date="2026-11-07"), "date must be an ISO date"),
        (_with_test(date=None), "date must be an ISO date"),
        (_with_test(date=dt.date(2026, 11, 12)), "is in the future"),
        (_with_test(meho_release="9.9.9"), "not a '## [X.Y.Z]' heading"),
        (_with_test(meho_release="Unreleased"), "not a '## [X.Y.Z]' heading"),
        (_with_test(product_version=8.0), "product_version must be quoted text"),
        (_with_test(extra="x"), "unknown field"),
    ],
)
def test_bad_test_entry_is_refused(data: dict[str, Any], message: str) -> None:
    assert any(message in problem for problem in _problems(data))


def test_duplicate_product_version_is_refused() -> None:
    data = deepcopy(GOOD)
    tests = data["connectors"]["demo-rest-1.0"]["tests"]
    tests.append({**tests[0], "level": "read"})
    assert any("is listed twice" in problem for problem in _problems(data))


def test_missing_tests_list_is_refused() -> None:
    data = deepcopy(GOOD)
    del data["connectors"]["demo-rest-2.0"]["tests"]
    assert any("tests must be a list" in problem for problem in _problems(data))


@pytest.mark.parametrize(
    "text",
    [
        "10.20.30.40",
        "name.example.org",
        "https://example.org/path",
        "www.example.org",
    ],
)
def test_lab_details_are_refused_in_a_value(text: str) -> None:
    data = deepcopy(GOOD)
    data["connectors"]["demo-rest-1.0"]["product"] = f"Demo {text}"
    assert any("lab detail not allowed" in problem for problem in _problems(data))


def test_lab_details_are_refused_in_a_comment() -> None:
    problems = _problems(GOOD, "connectors: {}  # tested on 10.1.2.3\n")
    assert any("in a comment" in problem for problem in problems)


def test_four_part_product_version_is_not_an_address() -> None:
    data = _with_test(product_version="5.1.1.0")
    assert _problems(data) == []


def test_every_connector_id_exists() -> None:
    """Each id is a public registry connector or an enterprise pack id."""
    _eager_import_connectors()
    known = {
        f"{impl_id}-{version}"
        for (_product, version, impl_id) in all_connectors_v2()
        if version and impl_id
    }
    for _product, ids, _versions, _what in _ENTERPRISE_PACK:
        known.update(part.strip() for part in ids.split(","))
    data = yaml.safe_load(RECORD.read_text(encoding="utf-8"))
    unknown = sorted(set(data["connectors"]) - known)
    assert not unknown, f"connector ids not in the registry or the enterprise pack: {unknown}"


def test_unknown_connector_id_would_be_caught() -> None:
    """The id check above is not vacuous: a made-up id is not in the known set."""
    _eager_import_connectors()
    known = {f"{i}-{v}" for (_p, v, i) in all_connectors_v2() if v and i}
    assert "not-a-connector-0.0" not in known
