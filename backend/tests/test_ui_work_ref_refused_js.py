# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``static/src/app/work-ref-refused.js``.

The ticket id search boxes answer a refused value with HTML, status 422 and
the ``Meho-Work-Ref-Refused`` header. htmx 2.0.9 does not swap an error
answer, so the script turns the swap on for exactly that answer.

No JS test runner ships in this repo. When ``node`` is on the PATH, these
tests run the real script with a fake ``document.body`` and call its
``htmx:beforeSwap`` listener with synthetic events.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from meho_backplane.ui.paths import static_src_dir, templates_dir
from meho_backplane.ui.routes.work_ref_form import WORK_REF_REFUSED_HEADER

_SCRIPT = static_src_dir() / "app" / "work-ref-refused.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

# Loads the script, then calls the listener once per case. A case is
# [status, headers]; the answer is [returned, shouldSwap, isError].
_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const cases = JSON.parse(process.argv[2]);
let listener = null;
const window = {};
const body = { addEventListener: (name, fn) => { if (name === "htmx:beforeSwap") listener = fn; } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document: { body }, window });
const out = cases.map(([status, headers]) => {
  const lower = {};
  for (const [k, v] of Object.entries(headers)) lower[k.toLowerCase()] = v;
  const xhr = { status, getResponseHeader: (name) => lower[name.toLowerCase()] ?? null };
  const detail = { xhr, shouldSwap: false, isError: true };
  const returned = listener({ detail });
  return [returned, detail.shouldSwap, detail.isError];
});
process.stdout.write(JSON.stringify(out));
"""

_HTML = {"content-type": "text/html; charset=utf-8"}


def _run(cases: list[tuple[int, dict[str, str]]]) -> list[list[bool]]:
    result = subprocess.run(
        ["node", "-e", _DRIVER, str(_SCRIPT), json.dumps(cases)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    answer: list[list[bool]] = json.loads(result.stdout)
    return answer


@requires_node
def test_a_refused_ticket_id_answer_is_swapped_in() -> None:
    (answer,) = _run([(422, {**_HTML, WORK_REF_REFUSED_HEADER: "1"})])

    assert answer == [True, True, False]


@requires_node
@pytest.mark.parametrize(
    "case",
    [
        (422, _HTML),
        (422, {"content-type": "application/json", WORK_REF_REFUSED_HEADER: "1"}),
        (400, {**_HTML, WORK_REF_REFUSED_HEADER: "1"}),
        (500, _HTML),
    ],
    ids=["no-header", "json", "other-status", "server-error"],
)
def test_every_other_error_answer_is_left_alone(case: tuple[int, dict[str, str]]) -> None:
    (answer,) = _run([case])

    assert answer == [False, False, True]


def test_every_console_page_loads_the_script() -> None:
    head = (templates_dir() / "_head_assets.html").read_text(encoding="utf-8")

    assert '<script src="/ui/static/src/app/work-ref-refused.js" defer></script>' in head
    assert head.index("htmx.min.js") < head.index("work-ref-refused.js")
