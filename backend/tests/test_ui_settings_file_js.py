# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``static/src/app/settings-file.js``.

The settings file page answers a refused file with an HTML fragment and
status 403, and a failed audit write with an HTML fragment and status 500.
htmx 2.0.9 does not swap an error answer, so the script turns the swap on
for an error answer of the page's two forms. An answer that is not HTML
becomes a fixed error text.

No JS test runner ships in this repo. When ``node`` is on the PATH, these
tests run the real script with a fake ``document.body`` and call its
``htmx:beforeSwap`` listener with synthetic events.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import pytest

from meho_backplane.ui.paths import static_src_dir, templates_dir

_SCRIPT = static_src_dir() / "app" / "settings-file.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

# Loads the script, then calls the listener once per case. A case is
# [status, content type, from a settings form]; the answer is
# [returned, shouldSwap, isError, serverResponse].
_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const cases = JSON.parse(process.argv[2]);
let listener = null;
const window = {};
const body = { addEventListener: (name, fn) => { if (name === "htmx:beforeSwap") listener = fn; } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document: { body }, window });
const out = cases.map(([status, contentType, fromForm]) => {
  const xhr = {
    status,
    getResponseHeader: (name) => (name.toLowerCase() === "content-type" ? contentType : null),
  };
  const marked = (selector) => fromForm && selector === "[data-settings-form]";
  const elt = { closest: (selector) => (marked(selector) ? {} : null) };
  const detail = {
    xhr, requestConfig: { elt }, shouldSwap: false, isError: true, serverResponse: "ORIGINAL",
  };
  const returned = listener({ detail });
  return [returned, detail.shouldSwap, detail.isError, detail.serverResponse];
});
process.stdout.write(JSON.stringify(out));
"""

_HTML = "text/html; charset=utf-8"
_JSON = "application/json"


def _run(cases: list[tuple[int, str, bool]]) -> list[list[Any]]:
    result = subprocess.run(
        ["node", "-e", _DRIVER, str(_SCRIPT), json.dumps(cases)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    answer: list[list[Any]] = json.loads(result.stdout)
    return answer


@requires_node
@pytest.mark.parametrize("status", [403, 500])
def test_an_html_error_answer_is_shown_as_it_is(status: int) -> None:
    (answer,) = _run([(status, _HTML, True)])

    assert answer == [True, True, False, "ORIGINAL"]


@requires_node
@pytest.mark.parametrize("status", [403, 500, 502])
def test_an_error_answer_that_is_not_html_shows_the_error_text(status: int) -> None:
    ((returned, swap, is_error, shown),) = _run([(status, _JSON, True)])

    assert (returned, swap, is_error) == (True, True, False)
    assert "data-settings-error" in shown
    assert f"MEHO could not finish this step (HTTP {status})." in shown
    assert "read the settings history before you apply it again" in shown


@requires_node
@pytest.mark.parametrize(
    "case",
    [(200, _HTML, True), (401, _JSON, True), (500, _HTML, False)],
    ids=["success", "session-expired", "other-element"],
)
def test_every_other_answer_is_left_alone(case: tuple[int, str, bool]) -> None:
    (answer,) = _run([case])

    assert answer == [False, False, True, "ORIGINAL"]


def test_the_page_loads_the_script_and_marks_both_forms() -> None:
    folder = templates_dir() / "settings"
    page = (folder / "index.html").read_text(encoding="utf-8")
    preview = (folder / "_preview.html").read_text(encoding="utf-8")

    assert '<script src="/ui/static/src/app/settings-file.js" defer></script>' in page
    assert 'id="settings-upload-form"\n    data-settings-form' in page
    assert 'id="settings-apply-form"\n          data-settings-form' in preview
