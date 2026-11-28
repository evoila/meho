# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The new-requests line of the Approvals page, run in the real script (#6932).

The Pending and For you lists carry the bell's count as ``data-baseline``.
``static/src/app/approvals-bell.js`` shows "N new requests." when a later
bell count differs from it.

No JS test runner ships in this repo. When ``node`` is on the PATH, these
tests run the real script with a fake ``document`` and a fake Alpine. They
feed it the real answers of the list route and the badge route: the list
answer gives the line, and each bell poll swaps in a badge answer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.settings import get_settings
from meho_backplane.ui.paths import static_src_dir
from tests.test_ui_approvals import (
    _HX_HEADERS,
    _RESOLVE_OPERATOR,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _operator,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

_SCRIPT = static_src_dir() / "app" / "approvals-bell.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

# Loads the script, starts the bell like Alpine does, then runs the steps.
# A step is {visible, event: "tick" | "visibilitychange", badge}. When the
# bell polls, the step's badge answer is swapped into the badge. The answer
# per step is [polled, line hidden, line text].
_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(process.argv[2]);
const tag = /<div[^>]*data-approvals-new-notice[^>]*>/.exec(input.list);
let notice = null;
if (tag) {
  const baseline = /data-baseline="([^"]*)"/.exec(tag[0]);
  const text = { textContent: "" };
  notice = {
    hidden: / hidden[ >]/.test(tag[0]),
    dataset: baseline ? { baseline: baseline[1] } : {},
    querySelector: (sel) => (sel === "[data-approvals-new-notice-text]" ? text : null),
    text,
  };
}
const listeners = {};
const dispatched = [];
let timer = null;
let bell = null;
const document = {
  visibilityState: "visible",
  addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
  removeEventListener: () => {},
  querySelector: (sel) => (sel === "[data-approvals-new-notice]" ? notice : null),
  getElementById: () => null,
  body: { dispatchEvent: (event) => dispatched.push(event.type) },
};
const fire = (name, event) => (listeners[name] || []).forEach((fn) => fn(event));
class CustomEvent { constructor(type) { this.type = type; } }
const window = { setInterval: (fn) => { timer = fn; return 1; }, clearInterval: () => {} };
const Alpine = { data: (name, factory) => { bell = factory(); } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  document, window, Alpine, CustomEvent,
});
fire("alpine:init", {});
bell.init();
const out = input.steps.map((step) => {
  document.visibilityState = step.visible ? "visible" : "hidden";
  dispatched.length = 0;
  if (step.event === "tick") timer(); else fire("visibilitychange", {});
  const polled = dispatched.includes("meho:approval-badge-poll");
  if (polled) {
    const count = /data-pending-count="([^"]*)"/.exec(step.badge)[1];
    const counter = { dataset: { pendingCount: count } };
    const target = {
      id: "topbar-approvals-badge",
      querySelector: (sel) => (sel === "[data-pending-count]" ? counter : null),
    };
    fire("htmx:afterSwap", { detail: { target } });
  }
  return [polled, notice ? notice.hidden : null, notice ? notice.text.textContent : null];
});
process.stdout.write(JSON.stringify(out));
"""

_SHOWN_ONE = [True, False, "1 new request."]


def _run(list_html: str, steps: list[dict[str, object]]) -> list[list[object]]:
    result = subprocess.run(
        ["node", "-e", _DRIVER, str(_SCRIPT), json.dumps({"list": list_html, "steps": steps})],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    answer: list[list[object]] = json.loads(result.stdout)
    return answer


def _poll(badge: str) -> dict[str, object]:
    """A visible tick of the bell's timer, answered with *badge*."""
    return {"visible": True, "event": "tick", "badge": badge}


def _badge(client: TestClient) -> str:
    response = client.get("/ui/approvals/badge", headers=_HX_HEADERS)
    assert response.status_code == 200, response.text
    return response.text


def _get(client: TestClient, url: str) -> str:
    headers = _HX_HEADERS if url.startswith("/ui/approvals/list") else {}
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return response.text


def _seed() -> uuid.UUID:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_request(tenant_id=_TENANT_A, op_id="pending.first", work_ref="gh:evoila/meho#1")
    return _seed_session_sync(tenant_id=_TENANT_A)


@requires_node
def test_a_request_after_the_list_fetch_shows_one_new_request() -> None:
    session_id = _seed()
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        list_html = _get(client, "/ui/approvals/list?tab=pending")
        _seed_request(tenant_id=_TENANT_A, op_id="pending.new")
        badge = _badge(client)

    assert _run(list_html, [_poll(badge)]) == [_SHOWN_ONE]


@requires_node
def test_a_request_before_the_first_poll_after_a_page_load_shows() -> None:
    session_id = _seed()
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        page = _get(client, "/ui/approvals?tab=pending")
        _seed_request(tenant_id=_TENANT_A, op_id="pending.new")
        badge = _badge(client)

    assert _run(page, [_poll(badge)]) == [_SHOWN_ONE]


@requires_node
def test_requests_while_the_tab_is_hidden_show_when_it_is_shown() -> None:
    session_id = _seed()
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        list_html = _get(client, "/ui/approvals/list?tab=pending")
        for i in range(3):
            _seed_request(tenant_id=_TENANT_A, op_id=f"pending.new.{i}")
        badge = _badge(client)

    steps: list[dict[str, object]] = [
        {"visible": False, "event": "tick", "badge": None},
        {"visible": True, "event": "visibilitychange", "badge": badge},
    ]
    assert _run(list_html, steps) == [[False, True, ""], [True, False, "3 new requests."]]


def _show_url(client: TestClient) -> str:
    """The URL the "Show" button of a drawn Pending list fetches."""
    body = _get(client, "/ui/approvals/list?tab=pending")
    match = re.search(r'data-approvals-new-notice.*?hx-get="([^"]+)"', body, re.DOTALL)
    assert match is not None
    return match.group(1)


@requires_node
@pytest.mark.parametrize(
    "url",
    [
        "show",
        # A tab click and a decision in the page both fetch this URL.
        "/ui/approvals/list?tab=pending",
        "/ui/approvals/list?tab=pending&order=newest",
        "/ui/approvals/list?tab=pending&order=oldest",
        "/ui/approvals/list?tab=pending&work_ref=gh:evoila/meho%231",
    ],
    ids=["show", "tab-or-decision", "order-newest", "order-oldest", "work-ref"],
)
def test_each_list_fetch_shows_the_line_for_a_later_request(url: str) -> None:
    session_id = _seed()
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        if url == "show":
            url = _show_url(client)
        list_html = _get(client, url)
        _seed_request(tenant_id=_TENANT_A, op_id="pending.new")
        badge = _badge(client)

    assert _run(list_html, [_poll(badge)]) == [_SHOWN_ONE]


@requires_node
def test_the_for_you_list_shows_the_line_with_routing_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()
    session_id = _seed()
    operator = _operator(tenant_id=_TENANT_A)
    with (
        respx.mock(assert_all_called=False),
        patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
    ):
        client = _authenticated_client(session_id)
        list_html = _get(client, "/ui/approvals/list?tab=for_you")
        _seed_request(tenant_id=_TENANT_A, op_id="pending.new")
        badge = _badge(client)

    assert _run(list_html, [_poll(badge)]) == [_SHOWN_ONE]


@requires_node
def test_the_same_count_keeps_the_line_hidden() -> None:
    session_id = _seed()
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        list_html = _get(client, "/ui/approvals/list?tab=pending")
        badge = _badge(client)

    assert _run(list_html, [_poll(badge), _poll(badge)]) == [[True, True, ""]] * 2


@requires_node
def test_a_line_without_a_count_stays_hidden() -> None:
    session_id = _seed()
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        list_html = _get(client, "/ui/approvals/list?tab=pending")
        _seed_request(tenant_id=_TENANT_A, op_id="pending.new")
        badge = _badge(client)
    without_count = re.sub(r' data-baseline="\d+"', "", list_html)

    assert _run(without_count, [_poll(badge)]) == [[True, True, ""]]
