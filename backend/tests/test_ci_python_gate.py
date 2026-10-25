# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The required Python check runs one complete sweep for each event."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_JOBS = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())["jobs"]


def _step(job: str, name: str) -> dict[str, Any]:
    return cast(
        "dict[str, Any]", next(step for step in _JOBS[job]["steps"] if step.get("name") == name)
    )


@pytest.mark.parametrize("event", ["push", "pull_request", "merge_group", "fork"])
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
def test_selected_sweep_must_pass(event: str, result: str) -> None:
    queue = event == "merge_group"
    fork = event == "fork"
    env = {
        **os.environ,
        "EVENT": "pull_request" if fork else event,
        "IS_FORK": "true" if fork else "false",
        "DETECT_RESULT": "skipped" if fork else "success",
        "DOCS_ONLY": "false",
        "LINT_RESULT": "skipped" if fork else "success",
        "UNIT_RESULT": result if queue else "skipped",
        "COVERAGE_RESULT": "skipped" if queue else result,
    }
    script = _step("python-unit-lane", "Assert lint + typecheck + every pytest shard passed")["run"]
    checked = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert (checked.returncode == 0) is (result == "success"), checked.stdout


@pytest.mark.parametrize(
    ("changes", "lint", "docs_only", "unused", "passes"),
    [
        ("success", "success", "false", "skipped", True),
        ("success", "skipped", "true", "skipped", True),
        ("failure", "success", "true", "skipped", False),
        ("cancelled", "success", "false", "skipped", False),
        ("skipped", "success", "false", "skipped", False),
        ("success", "skipped", "false", "skipped", False),
        ("success", "failure", "true", "skipped", False),
        ("success", "success", "false", "success", False),
        ("success", "success", "false", "failure", False),
    ],
)
def test_gate_preserves_detector_lint_and_one_sweep_contract(
    changes: str,
    lint: str,
    docs_only: str,
    unused: str,
    passes: bool,
) -> None:
    script = _step("python-unit-lane", "Assert lint + typecheck + every pytest shard passed")["run"]
    checked = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "EVENT": "pull_request",
            "IS_FORK": "false",
            "DETECT_RESULT": changes,
            "DOCS_ONLY": docs_only,
            "LINT_RESULT": lint,
            "UNIT_RESULT": unused,
            "COVERAGE_RESULT": "success",
        },
        capture_output=True,
        text=True,
    )
    assert (checked.returncode == 0) is passes, checked.stdout


def test_coverage_and_queue_select_exact_same_unit_files() -> None:
    steps = [
        _step("python-unit-shard", "Pytest (unit shard ${{ matrix.shard }}/4)"),
        _step("python-coverage", "Pytest (unit) with offline coverage data"),
    ]
    selected = []
    for step in steps:
        command = step["run"].split("find tests ", 1)[1].split(" | sort", 1)[0]
        result = subprocess.run(
            ["bash", "-c", "find tests " + command],
            cwd=_ROOT / "backend",
            check=True,
            capture_output=True,
            text=True,
        )
        selected.append(set(result.stdout.splitlines()))
    expected = {
        str(path.relative_to(_ROOT / "backend"))
        for path in (_ROOT / "backend/tests").rglob("test_*.py")
        if not path.is_relative_to(_ROOT / "backend/tests/integration")
        and not path.is_relative_to(_ROOT / "backend/tests/migrations")
    }
    assert selected[0] == selected[1] == expected


def test_coverage_keeps_required_unit_prerequisites_and_fork_guards() -> None:
    queue = "python-unit-shard"
    coverage = "python-coverage"
    for name in [
        "Install Helm",
        "Install PHP CLI",
        "Provision spaCy NLP model for Presidio Tier-2 (#1072)",
        "Embedding-model drift guard (evoila/meho#574)",
    ]:
        assert _step(queue, name) == _step(coverage, name)
    for name in [
        "Checkout vendor spec-shelf (secret-gated,",
        "Verify spec-shelf provisioned the pinned specs",
    ]:
        original = _step(queue, name)
        copied = _step(coverage, name)
        assert {k: v for k, v in original.items() if k != "if"} == {
            k: v for k, v in copied.items() if k != "if"
        }
        assert copied["if"] == original["if"] + (
            " && (github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)"
        )
    for key in ["SPEC_SHELF_TOKEN_PRESENT", "MEHO_CONSUMER_DOCS_ROOT"]:
        assert _JOBS[queue]["env"][key] == _JOBS[coverage]["env"][key]
    normal_env = _step(queue, "Pytest (unit shard ${{ matrix.shard }}/4)")["env"]
    measured_env = _step(coverage, "Pytest (unit) with offline coverage data")["env"]
    for key, value in normal_env.items():
        assert measured_env[key] == value
    assert _JOBS[queue]["if"] == "github.event_name == 'merge_group'"
    assert _JOBS[coverage]["if"] == "github.event_name != 'merge_group'"
    assert _JOBS[coverage].get("continue-on-error", False) is False
    assert _JOBS["python-unit-lane"]["if"] == "always()"
    assert "python-coverage" in _JOBS["python-unit-lane"]["needs"]
    assert "'ubuntu-latest'" in _JOBS[coverage]["runs-on"]
