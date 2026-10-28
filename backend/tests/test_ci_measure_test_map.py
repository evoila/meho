# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/measure_test_map.py`` (#5298).

The script counts how many tests a "run only the tests for the changed
code" map would pick. These tests check the rules that decide what a
changed file picks, and that the map is read right from coverage data.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from coverage import CoverageData

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "measure_test_map.py"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_test_map", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_NODEIDS = [
    "tests/test_a.py::test_one",
    "tests/test_a.py::test_two[x]",
    "tests/test_a.py::test_two[y]",
    "tests/sub/test_b.py::TestB::test_three",
]


def test_function_key_matches_the_coverage_context_name(tool: ModuleType) -> None:
    assert (
        tool.function_key("tests/sub/test_b.py::TestB::test_three")
        == "tests.sub.test_b.TestB.test_three"
    )
    assert tool.function_key("tests/test_a.py::test_two[x-1]") == "tests.test_a.test_two"


def test_read_collected_keeps_only_node_ids(tool: ModuleType) -> None:
    text = "\n".join([*_NODEIDS, "", "4 tests collected in 0.1s"])
    assert tool.read_collected(text) == _NODEIDS


def test_select_applies_the_rules_per_changed_file(tool: ModuleType) -> None:
    suite = tool.Suite.from_nodeids(_NODEIDS)
    file_map = {
        "backend/src/pkg/a.py": {"tests.test_a.test_two"},
        "backend/src/pkg/unused.py": set(),
    }

    docs_only = tool.select(
        ["docs/x.md", "README.md", "backend/tests/integration/test_i.py"], file_map, suite
    )
    assert docs_only.map_only == set()
    assert not docs_only.safe_full

    source = tool.select(["backend/src/pkg/a.py", "backend/tests/sub/test_b.py"], file_map, suite)
    assert source.map_only == {
        "tests/test_a.py::test_two[x]",
        "tests/test_a.py::test_two[y]",
        "tests/sub/test_b.py::TestB::test_three",
    }
    assert not source.safe_full

    unknown = tool.select(
        [
            "backend/src/pkg/a.py",
            "backend/src/pkg/unused.py",
            "backend/tests/conftest.py",
            "backend/uv.lock",
        ],
        file_map,
        suite,
    )
    assert len(unknown.map_only) == 2
    assert unknown.safe_full
    assert unknown.cannot_judge == [
        "backend/src/pkg/unused.py",
        "backend/tests/conftest.py",
        "backend/uv.lock",
    ]


def test_load_map_reads_test_contexts_and_drops_the_empty_one(
    tool: ModuleType, tmp_path: Path
) -> None:
    data = CoverageData(str(tmp_path / ".coverage"))
    data.set_context("")
    data.add_lines({"src/pkg/a.py": [1, 2], "src/pkg/b.py": [1]})
    data.set_context("tests.test_a.test_one")
    data.add_lines({"src/pkg/a.py": [3]})
    data.set_context("tests.test_a.test_two")
    data.add_lines({"src/pkg/a.py": [3, 4]})
    data.write()

    file_map = tool.load_map(str(tmp_path / ".coverage"))

    assert file_map == {
        "backend/src/pkg/a.py": {"tests.test_a.test_one", "tests.test_a.test_two"},
        "backend/src/pkg/b.py": set(),
    }
    total, shares = tool.touched_by_most(file_map, top=1)
    assert total == 2
    assert shares == [("backend/src/pkg/a.py", 1.0)]
