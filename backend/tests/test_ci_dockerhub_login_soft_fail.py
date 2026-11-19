# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A failed Docker Hub login must not fail a CI job.

The test images come through the image proxy, so the login only buys more
pull headroom. A short network problem on the way to Docker Hub once failed a
job in a merge-queue run and threw a ready pull request out of the queue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _login_steps() -> list[tuple[str, dict[str, Any]]]:
    workflow = cast("dict[Any, Any]", yaml.safe_load(_CI_WORKFLOW.read_text()))
    found: list[tuple[str, dict[str, Any]]] = []
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("docker/login-action@"):
                found.append((job_name, step))
    return found


def test_there_are_docker_hub_login_steps() -> None:
    assert _login_steps(), "the guard below would pass on nothing"


def test_every_docker_hub_login_step_continues_on_error() -> None:
    for job_name, step in _login_steps():
        assert step.get("continue-on-error") is True, (
            f"job {job_name!r}: the Docker Hub login step must set continue-on-error: true"
        )
