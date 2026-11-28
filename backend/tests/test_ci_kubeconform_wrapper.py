# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for scripts/ci/kubeconform.sh, with a stand-in ``kubeconform`` on the PATH."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "ci" / "kubeconform.sh"

# The stand-in prints the arguments it got, then plays the next answer of
# the file "answers": "ok", "download" (a failed schema download) or
# "invalid" (a finding in the manifests).
_STAND_IN = """#!/usr/bin/env bash
echo "args: $*" >> "$WORK/calls"
answer="$(head -n 1 "$WORK/answers")"
sed -i 1d "$WORK/answers"
case "$answer" in
  ok) echo "Summary: Valid: 1"; exit 0 ;;
  download) echo "x - failed downloading schema at https://example.invalid/s.json"; exit 1 ;;
  *) echo "x - ConfigMap x is invalid"; exit 1 ;;
esac
"""


def _run(tmp_path: Path, answers: list[str]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stand_in = bin_dir / "kubeconform"
    stand_in.write_text(_STAND_IN)
    stand_in.chmod(0o755)
    (tmp_path / "answers").write_text("".join(f"{a}\n" for a in answers))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "WORK": str(tmp_path),
        "KUBECONFORM_CACHE": str(tmp_path / "schemas"),
        "KUBECONFORM_PAUSES": "0 0",
    }
    done = subprocess.run(
        ["bash", str(_SCRIPT), "-strict", "rendered.yaml"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = (tmp_path / "calls").read_text().splitlines()
    return done, calls


def test_it_passes_the_schema_folder_and_the_arguments(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, ["ok"])

    assert done.returncode == 0, done.stdout
    assert calls == [f"args: -cache {tmp_path / 'schemas'} -strict rendered.yaml"]
    assert (tmp_path / "schemas").is_dir()


def test_a_failed_download_is_tried_again_and_the_log_says_so(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, ["download", "ok"])

    assert done.returncode == 0, done.stdout
    assert len(calls) == 2
    assert "could not download a schema (try 1 of 3). Trying again" in done.stdout


def test_three_failed_downloads_fail_the_check(tmp_path: Path) -> None:
    done, calls = _run(tmp_path, ["download", "download", "download", "ok"])

    assert done.returncode == 1
    assert len(calls) == 3
    assert "::error::kubeconform could not download a schema after 3 tries." in done.stdout


@pytest.mark.parametrize("answers", [["invalid", "ok"], ["download", "invalid", "ok"]])
def test_a_finding_in_the_manifests_fails_at_once(tmp_path: Path, answers: list[str]) -> None:
    done, calls = _run(tmp_path, answers)

    assert done.returncode == 1
    assert len(calls) == answers.index("invalid") + 1
    assert "is invalid" in done.stdout
