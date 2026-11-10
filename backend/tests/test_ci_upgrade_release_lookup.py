# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for the release lookup in ``.github/workflows/chart.yml``.

The ``upgrade-from-release`` job starts with the step "Find the last release".
It takes the newest release that has a published image. A release whose image
manifest is missing is skipped. Any other error of the image lookup must stop
the job, also a login error whose text says "not found" (for example a Docker
credential helper that is not installed).

These tests run the real step script, taken from the workflow file, with
``gh`` and ``docker`` replaced by shell functions. The shell is started the
way the runner starts a ``run:`` step (``bash --noprofile --norc -eo
pipefail``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "chart.yml"
_JOB = "upgrade-from-release"
_STEP = "Find the last release"

_MISSING = "no such manifest: ghcr.io/evoila/meho:{tag}"
_CREDENTIAL_ERROR = (
    'error getting credentials - err: exec: "docker-credential-desktop": '
    "executable file not found in $PATH, out: ``"
)


def _step_script() -> str:
    workflow = cast("dict[str, Any]", yaml.safe_load(_CHART_WORKFLOW.read_text()))
    steps = cast("list[dict[str, Any]]", workflow["jobs"][_JOB]["steps"])
    (step,) = [s for s in steps if s.get("name") == _STEP]
    return str(step["run"]).replace("${{ github.repository }}", "evoila/meho")


def _run(
    tmp_path: Path, tags: list[str], answers: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the step. ``answers`` maps a tag to a docker error; other tags have an image."""
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    cases = "".join(
        f"    *:{tag}) echo '{err.format(tag=tag)}' >&2; return 1 ;;\n"
        for tag, err in answers.items()
    )
    stubs = (
        f"gh() {{ printf '%s\\n' {' '.join(tags)}; }}\n"
        'docker() {\n  case "$3" in\n'
        f"{cases}"
        "    *) echo '{}' ;;\n  esac\n}\n"
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", stubs + _step_script()],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "GITHUB_ENV": str(github_env)},
        check=False,
    )
    return result, github_env.read_text()


def test_skips_a_release_without_an_image(tmp_path: Path) -> None:
    result, env = _run(tmp_path, ["v0.46.0", "v0.45.0"], {"v0.46.0": _MISSING})
    assert result.returncode == 0, result.stdout + result.stderr
    assert env == "PRIOR=v0.45.0\n"
    assert "skipping v0.46.0" in result.stdout


def test_credential_error_stops_the_lookup(tmp_path: Path) -> None:
    result, env = _run(tmp_path, ["v0.46.0", "v0.45.0"], {"v0.46.0": _CREDENTIAL_ERROR})
    assert result.returncode == 1
    assert env == ""
    assert "could not look up the image of v0.46.0" in result.stdout
    assert "skipping" not in result.stdout


def test_fails_when_no_release_has_an_image(tmp_path: Path) -> None:
    tags = ["v0.46.0", "v0.45.0", "v0.44.0", "v0.43.0", "v0.42.0"]
    result, env = _run(tmp_path, tags, dict.fromkeys(tags, _MISSING))
    assert result.returncode == 1
    assert env == ""
    assert "none of the newest five releases has a published image" in result.stdout
