# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the "Tested live on" cell text (``scripts/live_test_cells.py``)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from scripts.live_test_cells import NOT_RECORDED, NOT_TESTED, LiveTestRecord

_ROOT = Path(__file__).resolve().parents[2]


def _test(version: str, level: str, release: str = "0.46.0", day: str = "2026-11-07") -> dict:
    return {
        "product_version": version,
        "level": level,
        "date": dt.date.fromisoformat(day),
        "meho_release": release,
    }


def _record(connectors: dict[str, Any], latest: str = "0.46.0") -> LiveTestRecord:
    data = {"stale_after_minor_releases": 5, "connectors": connectors}
    return LiveTestRecord(data, latest)


def test_change_test() -> None:
    record = _record({"a-1.0": {"product": "a", "tests": [_test("8.0.3", "change")]}})
    assert record.tested_live_cell(["a-1.0"]) == "8.0.3: changes (Nov 2026)"


def test_read_test() -> None:
    record = _record({"a-1.0": {"product": "a", "tests": [_test("9.1", "read")]}})
    assert record.tested_live_cell(["a-1.0"]) == "9.1: reads (Nov 2026)"


def test_two_versions_in_one_cell() -> None:
    tests = [_test("8.0.3", "change"), _test("8.0.2", "read", day="2026-03-01")]
    record = _record({"a-1.0": {"product": "a", "tests": tests}})
    assert record.tested_live_cell(["a-1.0"]) == (
        "8.0.3: changes (Nov 2026); 8.0.2: reads (Mar 2026)"
    )


def test_old_test_names_the_release() -> None:
    old = _test("8.0.3", "change", release="0.40.0")
    record = _record({"a-1.0": {"product": "a", "tests": [old]}})
    assert record.tested_live_cell(["a-1.0"]) == (
        "8.0.3: changes (Nov 2026, last tested with MEHO v0.40.0)"
    )


def test_test_exactly_at_the_limit_is_not_old() -> None:
    edge = _test("8.0.3", "change", release="0.41.0")
    record = _record({"a-1.0": {"product": "a", "tests": [edge]}})
    assert "last tested" not in record.tested_live_cell(["a-1.0"])


def test_older_major_release_is_old() -> None:
    old = _test("8.0.3", "change", release="0.99.0")
    record = _record({"a-1.0": {"product": "a", "tests": [old]}}, latest="1.0.0")
    assert "last tested with MEHO v0.99.0" in record.tested_live_cell(["a-1.0"])


def test_empty_tests_means_not_tested() -> None:
    record = _record({"a-1.0": {"product": "a", "tests": []}})
    assert record.tested_live_cell(["a-1.0"]) == NOT_TESTED == "not yet tested live"


def test_missing_connector_means_not_recorded() -> None:
    assert _record({}).tested_live_cell(["a-1.0"]) == NOT_RECORDED == "not yet recorded"


def test_pack_row_with_two_connectors_names_each_id() -> None:
    record = _record(
        {
            "vmware-rest-8.0": {"product": "vcenter", "tests": [_test("8.0.3", "change")]},
            "vmware-rest-9.0": {"product": "vcenter", "tests": []},
        }
    )
    assert record.tested_live_cell(["vmware-rest-8.0", "vmware-rest-9.0"]) == (
        "`vmware-rest-8.0`: 8.0.3: changes (Nov 2026). `vmware-rest-9.0`: not yet tested live."
    )


def test_record_from_repo_loads() -> None:
    record = LiveTestRecord.from_repo(_ROOT)
    assert record.stale_after >= 1
    assert record.latest_release.count(".") == 2


def test_no_built_for_cell_is_empty() -> None:
    from scripts.generate_reference_docs import render_connector_index

    public = render_connector_index().split("## evoila's enterprise pack")[0]
    rows = [line for line in public.splitlines() if line.startswith("| `")]
    assert rows
    for row in rows:
        built_for = row.split(" | ")[3].strip()
        assert built_for not in ("", "—"), row
