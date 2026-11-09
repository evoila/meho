# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/helm_golden.py`` (no ``helm`` binary needed).

The command tests put a fake ``helm`` first on ``PATH``. It prints its
arguments, a random password and the text of a file the test controls, or
fails when the test asks it to.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "helm_golden.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("helm_golden", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


golden = _load()


def test_mask_random_hides_the_generated_values_only() -> None:
    text = (
        "data:\n"
        '  requirepass: "made-up-one"\n'
        '  token: "made-up-two"\n'
        '  other: "kept"\n'
        "annotations:\n"
        "  checksum/metrics-auth: " + "ab" * 32 + "\n"
        "  checksum/config: 1234   \n"
    )
    masked = golden.mask_random(text)
    assert 'requirepass: "<random>"' in masked
    assert 'token: "<random>"' in masked
    assert "checksum/metrics-auth: <random>" in masked
    assert 'other: "kept"' in masked
    assert "checksum/config: 1234" in masked
    assert golden.mask_random(masked) == masked


def test_every_scenario_has_a_committed_golden_file() -> None:
    for name in golden.SCENARIOS:
        assert (_REPO_ROOT / golden.GOLDEN_DIR / f"{name}.yaml").is_file(), name


def test_the_chart_job_runs_when_an_input_of_the_golden_renders_changes() -> None:
    workflow = (_REPO_ROOT / ".github" / "workflows" / "chart.yml").read_text(encoding="utf-8")
    match = re.search(r"grep -qE '([^']+)' <<<\"\$files\"", workflow)
    assert match is not None, "change filter not found in chart.yml"
    chart_filter = re.compile(match.group(1))
    inputs = {"scripts/ci/helm_golden.py", f"{golden.CHART}/values.yaml"}
    inputs |= {f"{golden.GOLDEN_DIR}/{name}.yaml" for name in golden.SCENARIOS}
    for args in golden.SCENARIOS.values():
        inputs |= {args[i + 1] for i, arg in enumerate(args) if arg in ("-f", "--values")}
    assert "deploy/values-examples/values-openshift.yaml" in inputs
    for path in sorted(inputs):
        assert chart_filter.search(path), f"{path} does not trigger the chart job"
    assert not chart_filter.search("deploy/values-examples/values-other.yaml")


_FAKE_HELM = r"""#!/bin/sh
if [ -n "$FAKE_HELM_FAIL" ]; then
  echo "Error: values don't meet the schema" >&2
  exit 1
fi
echo "# args: $*"
echo "  requirepass: \"$$-$(date +%N)\""
cat "$FAKE_HELM_BODY"
"""


@pytest.fixture
def fake_helm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake ``helm`` on ``PATH`` and return the file it prints."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helm = bin_dir / "helm"
    helm.write_text(_FAKE_HELM)
    helm.chmod(0o755)
    body = tmp_path / "body.yaml"
    body.write_text("kind: Service\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HELM_BODY", str(body))
    monkeypatch.delenv("FAKE_HELM_FAIL", raising=False)
    return body


def _golden_files(root: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((root / golden.GOLDEN_DIR).glob("*.yaml"))}


def test_write_then_check_passes_and_renders_again_equal(
    fake_helm: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert golden.main(["--root", str(root), "--write"]) == 0
    written = _golden_files(root)
    assert sorted(written) == sorted(f"{name}.yaml" for name in golden.SCENARIOS)
    assert all('requirepass: "<random>"' in text for text in written.values())
    capfd.readouterr()

    assert golden.main(["--root", str(root)]) == 0
    out = capfd.readouterr().out
    assert all(f"ok: {name}" in out for name in golden.SCENARIOS)
    assert _golden_files(root) == written


def test_check_fails_when_the_render_changed(
    fake_helm: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert golden.main(["--root", str(root), "--write"]) == 0
    before = _golden_files(root)
    fake_helm.write_text("kind: Deployment\n")
    capfd.readouterr()

    assert golden.main(["--root", str(root)]) == 1
    out = capfd.readouterr().out
    assert "-kind: Service" in out
    assert "+kind: Deployment" in out
    assert "Helm golden files out of date" in out
    assert _golden_files(root) == before


def test_check_fails_when_a_golden_file_is_missing(
    fake_helm: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert golden.main(["--root", str(root), "--write"]) == 0
    (root / golden.GOLDEN_DIR / "openshift.yaml").unlink()
    capfd.readouterr()

    assert golden.main(["--root", str(root)]) == 1
    out = capfd.readouterr().out
    assert "ok: built-in-core" in out
    assert "+kind: Service" in out
    assert "Helm golden files out of date" in out


@pytest.mark.parametrize("write", [False, True])
def test_a_helm_failure_exits_2_and_writes_nothing(
    fake_helm: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    write: bool,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("FAKE_HELM_FAIL", "1")
    argv = ["--root", str(root), *(["--write"] if write else [])]

    assert golden.main(argv) == 2
    out = capfd.readouterr().out
    assert "Helm render failed" in out
    assert "values don't meet the schema" in out
    assert not (root / golden.GOLDEN_DIR).exists()


def test_a_missing_helm_binary_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert golden.main(["--root", str(tmp_path)]) == 2
    assert "Helm render failed" in capfd.readouterr().out
