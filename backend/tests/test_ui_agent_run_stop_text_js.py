# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console's Stop button shows who may stop a run when a stop is refused.

``static/src/app/agent-run-console.js`` reads the detail of a 403 answer.
When it is ``agent_run_cancel_forbidden: <rule>``, the console shows the
rule. Other 403 answers keep the general text.

No JS test runner ships in this repo. When ``node`` is on the PATH, these
tests load the real script with a fake ``document`` and ``Alpine``, build
the console, and call ``cancel()`` against a fake ``fetch``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import pytest

from meho_backplane.auth.operator import RUN_STOP_RULE
from meho_backplane.ui.paths import static_src_dir

_SCRIPT = static_src_dir() / "app" / "agent-run-console.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

# Loads the script, builds the console, answers the stop with argv[2]
# (status and JSON body, or null for a body that is not JSON), and prints
# the console's error text.
_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const [status, body] = JSON.parse(process.argv[2]);
let factory = null;
const document = {
  body: { dataset: { mehoTenant: "" } },
  addEventListener: (name, fn) => { if (name === "alpine:init") fn(); },
};
const Alpine = { data: (name, fn) => { factory = fn; } };
const fetch = async () => ({
  status,
  ok: false,
  headers: { get: () => null },
  json: async () => { if (body === null) throw new SyntaxError("not json"); return body; },
});
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, Alpine, fetch });
const console_ = factory({ cancelUrlTemplate: "/ui/agents/a1/run/__RUN_ID__/cancel" });
console_.runId = "run-1";
console_.cancel().then(() => process.stdout.write(JSON.stringify(console_.cancelError)));
"""


def _stop_text(status: int, body: Any) -> str:
    result = subprocess.run(
        ["node", "-e", _DRIVER, str(_SCRIPT), json.dumps([status, body])],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    text: str = json.loads(result.stdout)
    return text


@requires_node
def test_a_refused_stop_shows_who_may_stop_the_run() -> None:
    text = _stop_text(403, {"detail": f"agent_run_cancel_forbidden: {RUN_STOP_RULE}"})

    assert text == f"You may not stop this run: {RUN_STOP_RULE}."


@requires_node
@pytest.mark.parametrize("body", [{"detail": "agent_run_cancel_forbidden"}, None])
def test_another_refused_stop_keeps_the_general_text(body: Any) -> None:
    assert _stop_text(403, body) == "You do not have permission to stop this run."
