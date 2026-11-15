# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A push to main must not start a CI run (#6387).

Every change reaches main through the merge queue, and the queue run already
ran the full matrix on the same code. A push run only repeated it: it took
heavy runners from pull requests and merge groups, and the next merge
cancelled it, which left a red gate on the commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

_SHARD_JOBS = (
    "python-lint",
    "python-unit-shard",
    "python-integration-shard",
    "python-migration-tests",
)


def _workflow() -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load(_CI_WORKFLOW.read_text()))


def test_ci_has_no_push_trigger() -> None:
    workflow = _workflow()
    # PyYAML reads the key `on` as the boolean True.
    triggers = workflow.get(True) or workflow["on"]
    assert "push" not in triggers, (
        "ci.yml must not run on push to main: the merge queue ran the same code"
    )


def test_ci_still_runs_for_pull_requests_queue_and_nightly() -> None:
    workflow = _workflow()
    triggers = workflow.get(True) or workflow["on"]
    assert {"pull_request", "merge_group", "schedule"} <= set(triggers)


def test_test_shards_exist_so_the_rule_guards_something() -> None:
    jobs = _workflow()["jobs"]
    for name in _SHARD_JOBS:
        assert name in jobs
