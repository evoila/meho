# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``static/src/app/operation-run-lock.js`` (#5111).

The script keeps the Run button of the operations Run dialog disabled while
its run goes on, stops a second Run while the first goes on, and lets a
form error (HTTP 400) replace the result view, also while the view asks
again. htmx 2.0.9 does not swap an error answer on its own.

No JS test runner ships in this repo. When ``node`` is on the PATH, these
tests run the real script against a small fake DOM, fed with the real HTML
that the routes answer, and check the Run button after each step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

import pytest

from meho_backplane.ui.paths import static_src_dir
from tests.test_ui_operations import _bff_env  # noqa: F401 -- autouse fixture
from tests.test_ui_operations_background_run import (
    _OK,
    _run,
    _run_id_of,
    _seed,
    _SlowDispatcher,
    _use,
)

_SCRIPT = static_src_dir() / "app" / "operation-run-lock.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

# A fake DOM: a dialog with the Run form, its Run button and the result
# region. The region holds one view (the HTML a route answered); the view
# "has" a ``[data-run-lock]`` element when its HTML has that attribute.
# Each step plays what htmx 2.0.9 does: ``htmx:beforeRequest`` on the
# element that asks, ``htmx:beforeSwap`` on the swap target, the swap only
# when ``shouldSwap`` is true, the Run form's ``hx-disabled-elt`` enabling
# the button again right after the swap, then ``htmx:load`` on the new view.
_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const html = JSON.parse(process.argv[2]);

const listeners = {};
const timers = [];
const body = { addEventListener: (name, fn) => { listeners[name] = fn; } };
const window = { setTimeout: (fn) => { timers.push(fn); } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document: { body }, window });

function node(name, parent) {
  const el = { name, parent };
  el.closest = (selector) => {
    for (let n = el; n; n = n.parent) {
      if (n.name === selector) return n;
    }
    return null;
  };
  return el;
}
const dialog = node("dialog", null);
const form = node("form", dialog);
const button = node("button", form);
button.disabled = false;
const region = node("#operations-run-result-region", dialog);
region.html = "";
region.querySelector = (selector) =>
  selector === "[data-run-lock]" && region.html.includes("data-run-lock") ? {} : null;
dialog.querySelector = (selector) =>
  selector === "button[data-action='run']" ? button : null;
let view = null;

function request(elt, target) {
  let stopped = false;
  listeners["htmx:beforeRequest"]({ detail: { elt, target },
    preventDefault: () => { stopped = true; } });
  return stopped;
}

function xhr(status, contentType) {
  return { status, getResponseHeader: (name) =>
    name.toLowerCase() === "content-type" ? contentType : null };
}

// One answer from the server, swapped the way htmx would.
function answer(target, status, text, contentType) {
  const detail = { elt: target, target, xhr: xhr(status, contentType || "text/html; charset=utf-8"),
    shouldSwap: status < 400, isError: status >= 400 };
  listeners["htmx:beforeSwap"]({ detail });
  if (detail.shouldSwap) {
    region.html = text;
    view = node("view", region);
    if (target === region) button.disabled = false;  // hx-disabled-elt ends
    listeners["htmx:load"]({ detail: { elt: view } });
    while (timers.length) timers.shift()();
  }
  return { swapped: detail.shouldSwap, isError: detail.isError,
           regionHtml: region.html, disabled: button.disabled };
}

// The operator clicks Run: returns null when the click was stopped.
function clickRun(status, text) {
  if (button.disabled || request(form, region)) return null;
  button.disabled = true;  // hx-disabled-elt
  return answer(region, status, text);
}

function ask(status, text, contentType) {
  const stopped = request(view, view);
  return { stopped, ...answer(view, status, text, contentType) };
}

const steps = {};
steps.firstClick = clickRun(200, html.running);
steps.secondClickOnDisabled = clickRun(200, html.running);
button.disabled = false;  // even when the button is enabled by hand
steps.secondClickOnEnabled = clickRun(200, html.running);
steps.askStillRunning = ask(200, html.running);
steps.askJson400 = ask(400, '{"detail": "x"}', "application/json");
steps.askServerError = ask(500, "<p>boom</p>");
steps.askFormError = ask(400, html.formError);
steps.clickAfterError = clickRun(200, html.running);
steps.askFinished = ask(200, html.finished);
steps.clickFormError = clickRun(400, html.formError);
const outside = node("#elsewhere", null);
const detail = { elt: outside, target: outside, xhr: xhr(400, "text/html"),
  shouldSwap: false, isError: true };
listeners["htmx:beforeSwap"]({ detail });
steps.outsideRegion400 = detail.shouldSwap;
process.stdout.write(JSON.stringify(steps));
"""


def _real_views(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The running view, a late target error (a poll) and a finished view."""
    client, mock, csrf = _seed()
    failing = _SlowDispatcher(
        {"status": "error", "error": "no such target", "extras": {"error_code": "no_target"}}
    )
    _use(monkeypatch, failing)
    with mock, client:
        running, _ = _run(client, csrf)
        run_id = _run_id_of(client)
        failing.release.set()
        deadline = time.monotonic() + 10
        poll = client.get(f"/ui/operations/runs/{run_id}")
        while poll.status_code == 200 and time.monotonic() < deadline:
            time.sleep(0.05)
            poll = client.get(f"/ui/operations/runs/{run_id}")
        quick = _SlowDispatcher(_OK)
        quick.release.set()
        _use(monkeypatch, quick)
        finished, _ = _run(client, csrf)
    assert running.status_code == 200
    assert "data-run-lock" in running.text
    assert poll.status_code == 400
    assert "no such target" in poll.text
    # The error view does not ask again: the polling stops.
    assert "hx-get" not in poll.text
    assert "hx-trigger" not in poll.text
    assert 'data-run-status="ok"' in finished.text
    return {"running": running.text, "formError": poll.text, "finished": finished.text}


def _drive(views: dict[str, str]) -> dict[str, Any]:
    node_bin = shutil.which("node")
    assert node_bin is not None
    done = subprocess.run(
        [node_bin, "-e", _DRIVER, str(_SCRIPT), json.dumps(views)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result: dict[str, Any] = json.loads(done.stdout)
    return result


@requires_node
def test_run_stays_disabled_while_the_run_goes_on_and_a_form_error_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    views = _real_views(monkeypatch)
    steps = _drive(views)

    # One click: the running view swaps in and Run stays disabled.
    assert steps["firstClick"]["swapped"] is True
    assert steps["firstClick"]["disabled"] is True
    # A second Run while the first goes on is stopped: by the disabled
    # button, and by the request guard when the button is enabled anyway.
    assert steps["secondClickOnDisabled"] is None
    assert steps["secondClickOnEnabled"] is None
    # Asking again does not unlock Run.
    assert steps["askStillRunning"]["stopped"] is False
    assert steps["askStillRunning"]["disabled"] is True
    # Only an HTML 400 is swapped: a JSON 400 and a 500 are left to htmx.
    assert steps["askJson400"]["swapped"] is False
    assert steps["askServerError"]["swapped"] is False
    assert steps["askServerError"]["disabled"] is True
    # A late target error replaces the view, shows the error and frees Run.
    assert steps["askFormError"]["stopped"] is False
    assert steps["askFormError"]["swapped"] is True
    assert steps["askFormError"]["isError"] is False
    assert steps["askFormError"]["regionHtml"] == views["formError"]
    assert "no such target" in steps["askFormError"]["regionHtml"]
    assert steps["askFormError"]["disabled"] is False
    # Run works again after the error, and is freed when the run ends.
    assert steps["clickAfterError"]["disabled"] is True
    assert steps["askFinished"]["disabled"] is False
    # A form error as the first answer to Run shows too, and frees Run.
    assert steps["clickFormError"]["swapped"] is True
    assert steps["clickFormError"]["disabled"] is False
    # An error answer to anything outside the result region is not touched.
    assert steps["outsideRegion400"] is False


def test_script_source_contract() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for event in ("htmx:load", "htmx:beforeRequest", "htmx:beforeSwap"):
        assert f'document.body.addEventListener("{event}"' in source
    assert "detail.xhr.status !== 400" in source
    assert "eval(" not in source
