# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The bring-your-own render must not change by accident (#4039).

An install that brings its own PostgreSQL, Keycloak and Vault turns the
built-in core parts off. For such an install, a new chart version must
render the same objects as the previous one. This test renders
``deploy/values-examples/values-bring-your-own-fixture.yaml`` and compares
it with the saved copy in ``fixtures/chart/bring-your-own-render.golden``.

Two things are evened out before the compare, and nothing else:

* blank lines and trailing spaces (Helm 3 and Helm 4 differ only there);
* the broadcast password, which the chart makes at random on every render
  when no Secret is given.

When a change to the bring-your-own render is wanted, write the new copy
and commit it with the change, so the review shows the difference::

    MEHO_UPDATE_CHART_RENDER=1 uv run pytest tests/test_chart_bring_your_own_render.py

Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_FIXTURE = _REPO_ROOT / "deploy" / "values-examples" / "values-bring-your-own-fixture.yaml"
_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "chart" / "bring-your-own-render.golden"

_RANDOM_PASSWORD = re.compile(r'^(\s+requirepass: )"[^"]*"$')

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _normalise(rendered: str) -> str:
    lines = []
    for raw in rendered.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        lines.append(_RANDOM_PASSWORD.sub(r'\1"<random>"', line))
    return "\n".join(lines) + "\n"


def _render_fixture() -> str:
    result = subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), "-f", str(_FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return _normalise(result.stdout)


def test_bring_your_own_render_matches_the_saved_copy() -> None:
    rendered = _render_fixture()
    if os.environ.get("MEHO_UPDATE_CHART_RENDER") == "1":
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(rendered)
    expected = _GOLDEN.read_text()
    if rendered != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=str(_GOLDEN.relative_to(_REPO_ROOT)),
                tofile="helm template (this change)",
                n=2,
            )
        )
        pytest.fail(
            "The bring-your-own render changed. If the change is wanted, run "
            "`MEHO_UPDATE_CHART_RENDER=1 uv run pytest "
            "tests/test_chart_bring_your_own_render.py` and commit the new copy.\n" + diff[:6000]
        )


def test_fixture_renders_no_built_in_part() -> None:
    rendered = _render_fixture()
    assert "app.kubernetes.io/name: postgres" not in rendered
    assert "-core-postgres-" not in rendered


def test_normalise_masks_only_the_random_password() -> None:
    text = 'a: 1\n\n  requirepass: "c2VjcmV0"   \nb:   \n  requirepassword: "x"\n'
    assert _normalise(text) == 'a: 1\n  requirepass: "<random>"\nb:\n  requirepassword: "x"\n'
