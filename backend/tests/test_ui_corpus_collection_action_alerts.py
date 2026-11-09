# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``static/src/app/corpus-collection-actions.js``.

The Docs Corpus collection detail page answers a refused probe, enable or
disable with an HTML alert fragment and an error status (403 for a global
row without platform admin, 409, 503). htmx 2.0.9 does not swap an error
response on its own, so the page loads this handler: on ``htmx:beforeSwap``
it turns the swap on for an error answer to one of the action controls
(``data-collection-action``) when the body is HTML, and closes the Disable
confirm modal so the alert in the readiness card is not hidden behind it.

No JS test runner ships in this repo. These tests pin the handler's source
contract, and -- when ``node`` is on the PATH -- run the real handler against
a small fake DOM with synthetic ``htmx:beforeSwap`` events to check each
decision: the alert swap is turned on for the three controls, the modal is
closed, and every other response is left alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from meho_backplane.ui.paths import static_src_dir

_HANDLER = static_src_dir() / "app" / "corpus-collection-actions.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

# A small fake DOM: a body that records listeners, a readiness card, the
# three action controls (one inside the Disable modal) and one plain
# element. Each case builds a synthetic ``htmx:beforeSwap`` event with the
# shape htmx 2.0.9 uses -- fired on the swap target, so ``detail.elt`` is the
# readiness card, and the control that sent the request is only in
# ``detail.requestConfig.elt`` -- and reports what the handler did with it.
_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

function element(attrs, parent) {
  const el = { attrs: attrs || {}, parent: parent || null, open: false, closed: false };
  el.hasAttribute = (name) => Object.prototype.hasOwnProperty.call(el.attrs, name);
  el.closest = (selector) => {
    const name = selector.slice(1, -1);
    for (let node = el; node; node = node.parent) {
      if (node.hasAttribute(name)) return node;
    }
    return null;
  };
  el.close = () => { el.open = false; el.closed = true; };
  return el;
}

const listeners = {};
const byId = {};
const body = {
  addEventListener: (name, fn) => { listeners[name] = fn; },
};
const document = { body, getElementById: (id) => byId[id] || null };
const window = {};
const source = fs.readFileSync(process.argv[2], "utf8");
vm.runInNewContext(source, { document, window });

const api = window.mehoCorpusCollectionActions;
const card = element({ id: "collection-readiness-card" });
byId[api.CARD_ID] = card;
const probeButton = element({ "data-collection-action": "probe" });
const enableButton = element({ "data-collection-action": "enable" });
const modal = element({ id: "corpus-disable-modal" });
const disableForm = element({ "data-collection-action": "disable" }, modal);
const submitButton = element({ type: "submit" }, disableForm);
const otherButton = element({ "hx-post": "/ui/elsewhere" });

function xhr(status, contentType) {
  return { status, getResponseHeader: (name) =>
    name.toLowerCase() === "content-type" ? contentType : null };
}

function run(control, status, contentType, options) {
  const detail = {
    elt: card, target: card, xhr: xhr(status, contentType),
    requestConfig: (options && options.noRequestConfig) ? undefined
      : { elt: control, target: card, verb: "post" },
    shouldSwap: (options && options.shouldSwap) || false, isError: status >= 400,
  };
  byId[api.DISABLE_MODAL_ID] = (options && options.modalOpen) ? modal : null;
  modal.open = !!(options && options.modalOpen);
  modal.closed = false;
  const handled = listeners["htmx:beforeSwap"]({ detail });
  return { handled, shouldSwap: detail.shouldSwap, isError: detail.isError,
           modalClosed: modal.closed };
}

const html = "text/html; charset=utf-8";
const result = {
  listener: typeof listeners["htmx:beforeSwap"] === "function",
  probe403: run(probeButton, 403, html),
  enable409: run(enableButton, 409, html),
  probe503: run(probeButton, 503, html),
  disable403FromModal: run(submitButton, 403, html, { modalOpen: true }),
  probe200: run(probeButton, 200, html),
  probe403Json: run(probeButton, 403, "application/json"),
  other403: run(otherButton, 403, html),
  alreadySwapping: run(probeButton, 403, html, { shouldSwap: true }),
  noRequestConfig: run(probeButton, 403, html, { noRequestConfig: true }),
};
process.stdout.write(JSON.stringify(result));
"""


def test_handler_source_contract() -> None:
    """The served handler listens on <body>, scopes to the controls, and swaps."""
    assert _HANDLER.is_file(), f"handler missing: {_HANDLER}"
    source = _HANDLER.read_text(encoding="utf-8")
    assert 'document.body.addEventListener("htmx:beforeSwap"' in source
    assert 'var ACTION_ATTR = "data-collection-action"' in source
    assert 'var CARD_ID = "collection-readiness-card"' in source
    assert 'var DISABLE_MODAL_ID = "corpus-disable-modal"' in source
    # The control that sent the request comes from the request config: htmx
    # fires the event on the swap target, so ``detail.elt`` is the card.
    assert "detail.requestConfig" in source
    assert "actionControl(detail.elt)" not in source
    assert "detail.xhr.status < 400" in source
    assert '"text/html"' in source
    assert "detail.shouldSwap = true" in source
    assert "detail.isError = false" in source
    assert "modal.close()" in source
    # No inline script and no eval: the console's CSP posture holds.
    assert "eval(" not in source
    assert "innerHTML" not in source


@requires_node
def test_handler_turns_the_alert_swap_on_for_the_action_controls(tmp_path: Path) -> None:
    """Run the real handler against a fake DOM with synthetic beforeSwap events."""
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(driver), str(_HANDLER)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert result["listener"] is True

    # An error answer with an HTML body, from any of the three controls:
    # the swap is turned on, and htmx treats the answer as a normal swap.
    for case in ("probe403", "enable409", "probe503"):
        assert result[case] == {
            "handled": True,
            "shouldSwap": True,
            "isError": False,
            "modalClosed": False,
        }, case

    # The same from the submit button inside the Disable confirm modal: the
    # modal is closed too, so the alert in the readiness card is in view.
    assert result["disable403FromModal"] == {
        "handled": True,
        "shouldSwap": True,
        "isError": False,
        "modalClosed": True,
    }

    # Left alone: a success, a JSON error (the app-shell handlers own those),
    # an error from any other element, and a response htmx already swaps.
    assert result["probe200"]["handled"] is False
    assert result["probe200"]["shouldSwap"] is False
    assert result["probe403Json"]["handled"] is False
    assert result["probe403Json"]["shouldSwap"] is False
    assert result["probe403Json"]["isError"] is True
    assert result["other403"]["handled"] is False
    assert result["other403"]["shouldSwap"] is False
    assert result["alreadySwapping"]["handled"] is False
    assert result["alreadySwapping"]["shouldSwap"] is True
    # An event without a request config names no control: left alone.
    assert result["noRequestConfig"]["handled"] is False
    assert result["noRequestConfig"]["shouldSwap"] is False
