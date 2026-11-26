# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The CI test jobs install node and require it.

The console script tests run JavaScript in ``node`` and skip when it is
missing, unless ``MEHO_REQUIRE_NODE=1`` is set. These tests pin, in
``.github/workflows/ci.yml``, that the unit shards and the job for the
tests that read files outside backend have an "Install Node" step before
their pytest step, and that the pytest step sets ``MEHO_REQUIRE_NODE: "1"``.
So a missing node fails the tests instead of skipping them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_JOBS = ("python-unit-shard", "python-outside-backend")
_PINNED = re.compile(r"^actions/setup-node@[0-9a-f]{40}$")


def _steps(text: str, job_name: str) -> list[dict[str, Any]]:
    doc = yaml.safe_load(text)
    return cast(list[dict[str, Any]], cast(dict[str, Any], doc["jobs"])[job_name]["steps"])


def _node_and_pytest(text: str, job_name: str) -> tuple[int, int, dict[str, Any]]:
    steps = _steps(text, job_name)
    node = [
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    ]
    tests = [i for i, step in enumerate(steps) if str(step.get("name", "")).startswith("Pytest (")]
    assert len(node) == 1, f"{job_name} must have exactly one actions/setup-node step"
    assert len(tests) == 1, f"{job_name} must have exactly one Pytest step"
    return node[0], tests[0], steps[tests[0]]


def _check(text: str, job_name: str) -> None:
    node_index, pytest_index, pytest_step = _node_and_pytest(text, job_name)
    node_step = _steps(text, job_name)[node_index]
    assert node_index < pytest_index, f"{job_name}: Install Node must run before pytest"
    assert _PINNED.match(str(node_step["uses"])), "pin actions/setup-node by full commit id"
    assert str(node_step["with"]["node-version"]) == "22"
    assert pytest_step.get("env", {}).get("MEHO_REQUIRE_NODE") == "1", (
        f'{job_name}: the pytest step must set MEHO_REQUIRE_NODE: "1"'
    )


@pytest.mark.parametrize("job_name", _JOBS)
def test_job_installs_node_and_requires_it(job_name: str) -> None:
    _check(_WORKFLOW.read_text(), job_name)


def test_node_step_has_the_same_pin_as_plugin_test() -> None:
    """One node pin for the repo, so a version bump changes all of them."""
    plugin = (_REPO_ROOT / ".github" / "workflows" / "plugin-test.yml").read_text()
    pin = re.search(r"actions/setup-node@[0-9a-f]{40}", plugin)
    assert pin is not None
    for job_name in _JOBS:
        index, _, _ = _node_and_pytest(_WORKFLOW.read_text(), job_name)
        assert _steps(_WORKFLOW.read_text(), job_name)[index]["uses"] == pin.group(0)


def _drop_node_step(text: str) -> str:
    return re.sub(
        r"\n      - name: Install Node\n(?:        .*\n|\n(?=        ))*?(?=\n      - name:)",
        "\n",
        text,
        count=1,
    )


def test_check_fails_without_the_node_step() -> None:
    text = _WORKFLOW.read_text()
    changed = _drop_node_step(text)
    assert changed != text
    with pytest.raises(AssertionError, match="setup-node"):
        _check(changed, "python-unit-shard")


def test_check_fails_without_the_switch() -> None:
    text = _WORKFLOW.read_text()
    changed = text.replace('          MEHO_REQUIRE_NODE: "1"\n', "", 1)
    assert changed != text
    with pytest.raises(AssertionError, match="MEHO_REQUIRE_NODE"):
        _check(changed, "python-unit-shard")


@pytest.mark.skip(reason="a test with a skip mark, to show the skip limit check in CI")
def test_skip_mark_for_the_skip_limit_check() -> None:
    raise AssertionError
