# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the one-connector unit test run (tests/_unit_connector_select.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests import _unit_connector_select as picker

BACKEND_DIR = Path(__file__).parent.parent
REPO_ROOT = BACKEND_DIR.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SRC = "backend/src/meho_backplane/connectors"


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repository: three connectors, shared code and core code.

    * ``alpha`` is used by no other code.
    * ``beta`` is used by the connector ``gamma`` (relative import).
    * ``delta`` is used by core code (``meho_backplane/core.py``).
    """
    _write(tmp_path, f"{SRC}/__init__.py")
    _write(tmp_path, f"{SRC}/registry.py")
    _write(tmp_path, f"{SRC}/_shared/__init__.py")
    _write(tmp_path, f"{SRC}/adapters/__init__.py")
    _write(tmp_path, f"{SRC}/alpha/__init__.py", "from .ops import run\n")
    _write(tmp_path, f"{SRC}/alpha/ops.py", "from .._shared import helper\n")
    _write(tmp_path, f"{SRC}/beta/__init__.py")
    _write(tmp_path, f"{SRC}/beta/ops.py")
    _write(tmp_path, f"{SRC}/gamma/__init__.py", "from ..beta.ops import thing\n")
    _write(tmp_path, f"{SRC}/delta/__init__.py")
    _write(
        tmp_path,
        "backend/src/meho_backplane/core.py",
        "from meho_backplane.connectors import delta\n",
    )
    _write(tmp_path, "backend/tests/conftest.py", "import meho_backplane.connectors.registry\n")
    _write(
        tmp_path,
        "backend/tests/test_connectors_alpha_ops.py",
        "from meho_backplane.connectors.alpha import run\n",
    )
    _write(tmp_path, "backend/tests/_beta_fixtures.py", "import meho_backplane.connectors.beta\n")
    _write(tmp_path, "backend/tests/test_uses_beta_helper.py", "from tests import _beta_fixtures\n")
    _write(
        tmp_path,
        "backend/tests/test_gamma_patch.py",
        'PATCH = "meho_backplane.connectors.gamma.ops.call"\n',
    )
    _write(tmp_path, "backend/tests/test_unrelated.py", "import os\n")
    for name in picker.CONTRACT_TESTS:
        _write(tmp_path, f"backend/{name}")
    return tmp_path


def _detect(repo: Path, *changed: str) -> str:
    return picker.detect(changed, repo)


def test_one_connector_gives_the_short_run(repo: Path) -> None:
    changed = (
        f"{SRC}/alpha/ops.py",
        "backend/tests/test_connectors_alpha_ops.py",
        "docs/codebase/connectors-alpha.md",
    )

    assert _detect(repo, *changed) == "alpha"


def test_connector_used_only_by_another_connector_gives_the_short_run(repo: Path) -> None:
    assert _detect(repo, f"{SRC}/beta/ops.py", "backend/tests/_beta_fixtures.py") == "beta"


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param((f"{SRC}/alpha/ops.py", f"{SRC}/beta/ops.py"), id="two-connectors"),
        pytest.param((f"{SRC}/alpha/ops.py", f"{SRC}/_shared/helper.py"), id="shared"),
        pytest.param((f"{SRC}/_shared/helper.py",), id="shared-only"),
        pytest.param((f"{SRC}/adapters/http.py",), id="adapters"),
        pytest.param((f"{SRC}/alpha/ops.py", f"{SRC}/registry.py"), id="connectors-core-file"),
        pytest.param((f"{SRC}/alpha/ops.py", "backend/src/meho_backplane/core.py"), id="core"),
        pytest.param((f"{SRC}/alpha/ops.py", ".github/workflows/ci.yml"), id="ci-yml"),
        pytest.param((f"{SRC}/alpha/ops.py", "backend/pyproject.toml"), id="pyproject"),
        pytest.param(
            (f"{SRC}/alpha/ops.py", "backend/tests/test_unrelated.py"), id="other-test-file"
        ),
        pytest.param(
            (f"{SRC}/alpha/ops.py", "backend/tests/test_connectors_beta_x.py"),
            id="other-connector-test",
        ),
        pytest.param(("backend/tests/test_connectors_alpha_ops.py",), id="tests-only"),
        pytest.param((f"{SRC}/delta/__init__.py",), id="used-by-core"),
        pytest.param(("docs/codebase/connectors-alpha.md",), id="docs-only"),
        pytest.param((), id="empty"),
    ],
)
def test_anything_else_gives_the_full_run(repo: Path, changed: tuple[str, ...]) -> None:
    assert _detect(repo, *changed) == ""


def test_connector_named_in_a_conftest_gives_the_full_run(repo: Path) -> None:
    _write(repo, "backend/tests/conftest.py", "from meho_backplane.connectors.alpha import run\n")

    assert _detect(repo, f"{SRC}/alpha/ops.py") == ""


def test_connector_used_through_another_connector_by_core_gives_the_full_run(
    repo: Path,
) -> None:
    _write(repo, "backend/src/meho_backplane/core.py", "import meho_backplane.connectors.gamma\n")

    assert _detect(repo, f"{SRC}/beta/ops.py") == ""


def test_select_picks_the_connector_tests_and_the_contract_set(repo: Path) -> None:
    backend = repo / "backend"
    candidates = sorted(
        str(path.relative_to(backend)) for path in (backend / "tests").glob("test_*.py")
    )

    selected = picker.select("beta", candidates, backend)

    # beta's own helper user, plus gamma (it imports beta), plus the contract set.
    assert set(selected) == {
        "tests/test_uses_beta_helper.py",
        "tests/test_gamma_patch.py",
        *picker.CONTRACT_TESTS,
    }


def test_select_refuses_a_connector_used_by_core(repo: Path) -> None:
    backend = repo / "backend"

    with pytest.raises(ValueError, match="outside the connector folders"):
        picker.select("delta", list(picker.CONTRACT_TESTS), backend)


def test_select_refuses_a_missing_contract_file(repo: Path) -> None:
    backend = repo / "backend"

    with pytest.raises(ValueError, match="contract test files not found"):
        picker.select("alpha", ["tests/test_connectors_alpha_ops.py"], backend)


def test_every_contract_test_file_exists() -> None:
    missing = [name for name in picker.CONTRACT_TESTS if not (BACKEND_DIR / name).is_file()]

    assert missing == []


def test_real_tree_core_change_gives_the_full_run() -> None:
    changed = ["backend/src/meho_backplane/main.py"]

    assert picker.detect(changed, REPO_ROOT) == ""


def test_ci_wires_the_short_run_into_the_unit_shards() -> None:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    changes = jobs["changes"]
    assert changes["outputs"]["single_connector"] == (
        "${{ steps.detect.outputs.single_connector }}"
    )
    detect_step = next(step for step in changes["steps"] if step.get("id") == "detect")
    assert "backend/tests/_unit_connector_select.py detect" in detect_step["run"]

    shard = jobs["python-unit-shard"]
    matrix = shard["strategy"]["matrix"]["shard"]
    assert "needs.changes.outputs.single_connector" in matrix
    assert "[1, 2, 3, 4]" in matrix
    pytest_step = next(step for step in shard["steps"] if step["name"].startswith("Pytest"))
    assert "tests/_unit_connector_select.py select" in pytest_step["run"]
    assert pytest_step["env"]["SINGLE_CONNECTOR"] == (
        "${{ needs.changes.outputs.single_connector }}"
    )
