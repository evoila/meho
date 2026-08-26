# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The human-only MCP surface has no agent-facing path (#3155 / #3153).

Three decision verbs — ``meho_approvals_approve``,
``meho_approvals_reject``, ``meho_agents_grant_elevate`` — must be
absent from ``tools/list`` under **every** claim set (including an
elevated ``tenant_admin`` token) and must reject a direct
``tools/call`` with a remediation naming the console / CLI path. The
REST / CLI / console surfaces are untouched; this file pins only the
MCP-side removal.

Three complementary guards:

* **Registry absence** — after the full eager import, none of the
  three names resolves to a registered tool. This is claim-independent:
  a name that is not in the registry cannot surface for any role,
  capability, or future elevation claim.
* **Wire behaviour** — ``tools/list`` never lists them (parametrised
  over every role, up to and including ``tenant_admin``), and
  ``tools/call`` on each returns ``-32602`` ``INVALID_PARAMS`` whose
  message names the operator console and the ``meho`` CLI verb.
* **Static re-add guard** — no module under ``mcp/tools/`` that
  registers a tool may reference an approval-decision or
  grant-elevation endpoint symbol, so a future registration wiring one
  of those handlers back onto the agent surface fails CI at unit time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.tools as mcp_tools_pkg
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.human_only import HUMAN_ONLY_MCP_TOOLS
from meho_backplane.mcp.registry import (
    clear_registries,
    eager_import_mcp_modules,
    get_tool,
)
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

#: The three verbs that have no MCP path under any claim set.
_HUMAN_ONLY_NAMES: tuple[str, ...] = (
    "meho_approvals_approve",
    "meho_approvals_reject",
    "meho_agents_grant_elevate",
)

#: Substrings the remediation for each verb MUST carry — the operator
#: console and the concrete ``meho`` CLI verb (AC: "a remediation naming
#: the CLI/console path"). Pins the message so a future edit that drops
#: the actionable path fails here.
_REMEDIATION_TOKENS: dict[str, tuple[str, ...]] = {
    "meho_approvals_approve": ("console", "meho approvals approve"),
    "meho_approvals_reject": ("console", "meho approvals reject"),
    "meho_agents_grant_elevate": ("console", "meho agent grant elevate"),
}

#: Endpoint symbols whose presence in a tool module means a handler is
#: wired to an approval-decision or grant-elevation surface. A file that
#: both registers a tool and references one of these is a re-add of a
#: human-only verb onto the agent surface.
_FORBIDDEN_ENDPOINT_SYMBOLS: tuple[str, ...] = (
    "approve_request",  # approval-decision (status flip + audit + broadcast)
    "reject_request",  # approval-decision
    "resume_dispatch_after_approval",  # executes the approved op
    "AgentElevationCreate",  # grant-elevation schema
)


def _tools_call(name: str, arguments: dict[str, Any], call_id: int = 1) -> dict[str, Any]:
    """Build a JSON-RPC ``tools/call`` envelope."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def test_human_only_map_pins_exactly_the_three_verbs() -> None:
    """:data:`HUMAN_ONLY_MCP_TOOLS` names exactly the three de-registered verbs."""
    assert set(HUMAN_ONLY_MCP_TOOLS) == set(_HUMAN_ONLY_NAMES)


def test_human_only_tools_are_not_registered() -> None:
    """After a full eager import, none of the three verbs is a registered tool.

    Registry absence is the load-bearing "no MCP path under any claim
    set" guarantee — a tool that is not in the registry can never be
    listed or dispatched for any role, capability, or future elevation
    claim. Runs the production eager import directly (not the fixture
    reload) so a real boot is exercised.
    """
    clear_registries()
    try:
        eager_import_mcp_modules()
        for name in _HUMAN_ONLY_NAMES:
            assert get_tool(name) is None, (
                f"{name!r} must have no MCP registration (human-only, #3155)"
            )
    finally:
        clear_registries()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY, TenantRole.OPERATOR, TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_human_only_tools_absent_from_tools_list_for_every_role(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """No role — including the highest, ``tenant_admin`` — sees the three verbs.

    Parametrised across the whole role ladder to prove the removal is
    not a role-gate (which an elevated token could clear) but a true
    de-registration.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200, resp.text
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    leaked = names & set(_HUMAN_ONLY_NAMES)
    assert leaked == set(), f"human-only verbs must never appear on tools/list; saw {leaked!r}"


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.parametrize("tool_name", _HUMAN_ONLY_NAMES)
def test_human_only_tools_call_is_denied_with_remediation(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    tool_name: str,
) -> None:
    """A direct ``tools/call`` (even as ``tenant_admin``) is denied with remediation.

    The dispatcher denies before the registry lookup, so knowing the
    name out-of-band cannot dispatch the verb. The message must name the
    operator console and the concrete ``meho`` CLI verb so an agent that
    reached for the removed tool is redirected to the human path.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call(tool_name, {}))
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] == INVALID_PARAMS, body
    message = body["error"]["message"]
    assert "no MCP path" in message, message
    for token in _REMEDIATION_TOKENS[tool_name]:
        assert token in message, f"{tool_name}: remediation missing {token!r} — {message!r}"


def test_no_tool_module_wires_a_decision_or_elevation_endpoint() -> None:
    """Static guard: no tool module registers a tool AND references a forbidden endpoint.

    Grep/static enforcement of the AC. A future PR that re-adds an MCP
    tool whose handler resolves to an approval-decision
    (``approve_request`` / ``reject_request`` /
    ``resume_dispatch_after_approval``) or grant-elevation
    (``AgentElevationCreate``) endpoint would reference one of those
    symbols in the tool module, tripping this test — regardless of the
    tool's wire name.
    """
    tools_dir = Path(mcp_tools_pkg.__path__[0])
    patterns = {sym: re.compile(rf"\b{re.escape(sym)}\b") for sym in _FORBIDDEN_ENDPOINT_SYMBOLS}
    offenders: list[str] = []
    for path in sorted(tools_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "register_mcp_tool(" not in source:
            continue
        hits = [sym for sym, pat in patterns.items() if pat.search(source)]
        if hits:
            offenders.append(f"{path.name}: {', '.join(hits)}")
    assert offenders == [], (
        "tool module(s) register a tool and reference an approval-decision / "
        f"grant-elevation endpoint (human-only, #3155): {offenders}"
    )
