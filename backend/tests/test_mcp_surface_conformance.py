# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end ``tools/list`` listing-path conformance for the surface split (#3157).

Sibling :mod:`tests.test_mcp_surface_filter` (#3154) pins the surface
partition at *registry state*: it drives
:func:`~meho_backplane.mcp.registry.all_tools_for` and asserts the
working / operator membership as **frozensets**. This file complements
that by pinning the surface split at the *listing wire path* the way a
real MCP client observes it — the exact **sorted** name lists a
``tools/list`` response carries per claim shape — driven through
:func:`~meho_backplane.mcp.handlers.handle_tools_list` (the function the
``/mcp`` JSON-RPC route dispatches to) and, for the transport proof,
through ``POST /mcp`` itself.

It is the #2745/#2774 conformance lineage applied to the surface split:
a single authoritative snapshot of what the wire emits, so a tool added
without a surface classification (caught at construction by #3154's
required-no-default field) or *reclassified* between the two surfaces
fails CI here loudly and visibly rather than surfacing as the next
dogfood finding.

Why this is not a duplicate of #3154:

* #3154 asserts sets off ``all_tools_for`` (pre-wire registry state).
  A regression in ``handle_tools_list`` (it stopped calling
  ``all_tools_for``) or in :meth:`ToolDefinition.to_wire` (it added or
  dropped a name) would pass #3154 and fail here.
* The listing is pinned as an exact **sorted sequence**, not a set —
  the acceptance criterion is "the exact sorted working-surface listing
  and the elevated listing".
* Coverage is **per claim shape** (default working / ``mcp:admin``
  elevated / elevated-without-``meho-docs``) and includes an
  end-to-end transport assertion through the real ``/mcp`` route.

The pinned snapshots are tied to the live registry by
:func:`test_pinned_surfaces_partition_the_live_registry`, so they can
never drift from reality (a reclassification breaks the per-claim-shape
pins; an unclassified addition breaks the partition guard). The
authoritative human-readable enumeration of the same split — name +
one-liner + surface + gating claim for all 79 tools — lives in
``docs/codebase/mcp.md`` (the dual-surface inventory).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.handlers import handle_tools_list
from meho_backplane.mcp.human_only import HUMAN_ONLY_MCP_TOOLS
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_DOCS_CAPABILITY = "meho-docs"

#: The three working-surface docs tools — visible only to a session that
#: ALSO holds the ``meho-docs`` capability (mirrors #3154's set).
_DOCS_WORKING_TOOLS: frozenset[str] = frozenset({"search_docs", "ask_docs", "list_doc_collections"})

#: Every ``meho-docs``-capability-gated tool across BOTH surfaces: the
#: three working docs tools plus the two operator-surface doc-collection
#: lifecycle tools. All drop out when the capability is absent, so an
#: elevated session without ``meho-docs`` loses all five.
_DOCS_CAP_GATED: frozenset[str] = _DOCS_WORKING_TOOLS | {
    "create_doc_collections",
    "delete_doc_collections",
}

#: The **pairing-gated** automation family (Task #3029). Unlike the docs
#: capability (a static tenant JWT claim), these tools are gated on live add-on
#: pairing state — absent from every session's listing until a paired,
#: contract-healthy add-on advertises the ``automation`` meta-tool family, so
#: they sit on neither the pinned working nor operator surface (both of which
#: represent a provisioned-but-unpaired session). Their appear-on-pair /
#: disappear-on-unpair behaviour is pinned by ``test_mcp_automation_activation``;
#: here they only complete the registry partition. Kept a separate set so the
#: unpaired working/operator listings stay byte-identical to a build that never
#: carried the family.
_AUTOMATION_GATED_TOOLS: frozenset[str] = frozenset({"meho_automation_list"})

#: The default working surface, as the exact sorted listing a ``tools/list``
#: response carries for a session that clears role + capability but is NOT
#: ``mcp:admin``-elevated. This literal is the conformance snapshot; it is
#: tied to the live registry by ``test_pinned_surfaces_partition_the_live_registry``.
WORKING_SURFACE_SORTED: tuple[str, ...] = (
    "add_to_knowledge",
    "add_to_memory",
    "ask_docs",
    "call_operation",
    "list_doc_collections",
    "list_operation_groups",
    "list_targets",
    "meho_broadcast_announce",
    "meho_broadcast_recent",
    "meho_broadcast_watch",
    "meho_connector_list",
    "meho_runbook_abort",
    "meho_runbook_list_runs",
    "meho_runbook_list_templates",
    "meho_runbook_next",
    "meho_runbook_show_template",
    "meho_runbook_start",
    "meho_status",
    "preview_operation",
    "query_topology",
    "result_query",
    "search_docs",
    "search_knowledge",
    "search_memory",
    "search_operations",
)

#: The operator planes an ``mcp:admin``-elevated session additionally
#: lists, as the exact sorted listing. Same snapshot discipline.
OPERATOR_SURFACE_SORTED: tuple[str, ...] = (
    "create_doc_collections",
    "delete_doc_collections",
    "meho_agent_principals_list",
    "meho_agent_principals_register",
    "meho_agent_principals_revoke",
    "meho_agents_create",
    "meho_agents_delete",
    "meho_agents_edit",
    "meho_agents_grant_create",
    "meho_agents_grant_list",
    "meho_agents_grant_revoke",
    "meho_agents_grant_show",
    "meho_agents_list",
    "meho_agents_list_runs",
    "meho_agents_run",
    "meho_agents_run_status",
    "meho_agents_show",
    "meho_approvals_get",
    "meho_approvals_list",
    "meho_audit_replay",
    "meho_broadcast_overrides_list",
    "meho_broadcast_overrides_remove",
    "meho_broadcast_overrides_set",
    "meho_connector_delete",
    "meho_connector_disable",
    "meho_connector_edit_group",
    "meho_connector_edit_op",
    "meho_connector_enable",
    "meho_connector_enable_reads",
    "meho_connector_ingest",
    "meho_connector_ingest_status",
    "meho_connector_review",
    "meho_memory_promote",
    "meho_runbook_deprecate_template",
    "meho_runbook_discard_template",
    "meho_runbook_draft_template",
    "meho_runbook_edit_template",
    "meho_runbook_publish_template",
    "meho_runbook_reassign",
    "meho_scheduler_cancel",
    "meho_scheduler_create",
    "meho_scheduler_list",
    "meho_sensor_create",
    "meho_sensor_delete",
    "meho_sensor_list",
    "meho_sensor_results",
    "meho_targets_register",
    "meho_topology_annotate",
    "meho_topology_bulk_import",
    "meho_topology_create_node",
    "meho_topology_delete_node",
    "meho_topology_unannotate",
    "query_audit",
)

#: The full elevated listing (working + operator), sorted — what an
#: ``mcp:admin`` session holding ``meho-docs`` lists.
FULL_SURFACE_SORTED: tuple[str, ...] = tuple(
    sorted(WORKING_SURFACE_SORTED + OPERATOR_SURFACE_SORTED),
)


def _operator(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    capabilities: frozenset[str] = frozenset(),
    scopes: frozenset[str] = frozenset(),
) -> Operator:
    """Build a fixture operator with the requested role / capability / scope.

    Role defaults to ``tenant_admin`` so the role gate never masks the
    surface / capability gate under test; each test dials
    ``capabilities`` and ``scopes`` explicitly to isolate the axis it
    pins.
    """
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
        scopes=scopes,
    )


@contextmanager
def _client_for(operator: Operator) -> Iterator[TestClient]:
    """Yield a ``TestClient`` whose MCP identity is exactly *operator*.

    The shared ``client_with_operator`` fixture always builds an
    ``mcp:admin``-elevated operator with no tenant capabilities, so it
    can't express the non-elevated / capability-holding claim shapes
    this file pins. This local override binds an arbitrary operator and
    runs the FastAPI lifespan (``with TestClient(app)``) so the real
    ``/mcp`` route + dispatcher are exercised end to end.
    """
    app.dependency_overrides[verify_mcp_jwt_and_bind] = lambda: operator
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


# ---------------------------------------------------------------------------
# Snapshot hygiene + tie to the live registry
# ---------------------------------------------------------------------------


def test_pinned_surfaces_are_sorted_and_unique() -> None:
    """Each pinned listing is strictly sorted with no duplicate names."""
    for pinned in (WORKING_SURFACE_SORTED, OPERATOR_SURFACE_SORTED, FULL_SURFACE_SORTED):
        assert list(pinned) == sorted(pinned)
        assert len(pinned) == len(set(pinned))


def test_pinned_surfaces_partition_the_live_registry() -> None:
    """The pinned listings partition the whole registry (exhaustive + disjoint).

    This is the tie that keeps the snapshots honest: an unclassified new
    tool (once it clears #3154's construction-time surface requirement)
    lands in the registry but no pinned listing, breaking the
    exhaustiveness check; a reclassified tool breaks the per-claim-shape
    listing pins below. Either way CI fails visibly.

    Three disjoint sets now partition the registry: the working surface, the
    operator surface, and the pairing-gated automation family (#3029) — which
    is on neither default surface because it is absent until an add-on pairs,
    so it is carried separately here rather than folded into the working
    listing (which would break the byte-identical-unpaired pins below).
    """
    working = set(WORKING_SURFACE_SORTED)
    operator = set(OPERATOR_SURFACE_SORTED)
    automation = set(_AUTOMATION_GATED_TOOLS)
    assert working.isdisjoint(operator)
    assert working.isdisjoint(automation)
    assert operator.isdisjoint(automation)
    assert working | operator | automation == set(mcp_registry._TOOLS)


def test_full_surface_is_working_plus_operator() -> None:
    """The elevated listing is exactly the union of the two surfaces."""
    assert set(FULL_SURFACE_SORTED) == set(WORKING_SURFACE_SORTED) | set(OPERATOR_SURFACE_SORTED)
    assert len(FULL_SURFACE_SORTED) == len(WORKING_SURFACE_SORTED) + len(OPERATOR_SURFACE_SORTED)


def test_automation_family_absent_from_default_surfaces() -> None:
    """The pairing-gated automation family is on neither pinned default surface (#3029).

    The pinned working / operator listings represent a provisioned-but-unpaired
    session, so the automation family — absent until an add-on pairs — must not
    appear in either. This is the static half of the byte-identical-unpaired
    guarantee; the live-DB half (the wire listing with and without a paired
    add-on) is pinned by ``test_mcp_automation_activation``.
    """
    automation = set(_AUTOMATION_GATED_TOOLS)
    assert automation.isdisjoint(set(WORKING_SURFACE_SORTED))
    assert automation.isdisjoint(set(OPERATOR_SURFACE_SORTED))
    # The family is nonetheless registered — it is a real, gated tool.
    assert automation <= set(mcp_registry._TOOLS)


def test_human_only_verbs_absent_from_both_surfaces() -> None:
    """The de-registered human-only trio (#3155) is on neither pinned surface.

    The working surface must never carry ``meho_approvals_approve`` /
    ``meho_approvals_reject`` / ``meho_agents_grant_elevate`` — they have
    no MCP registration at all, so they cannot appear in any listing.
    """
    human_only = set(HUMAN_ONLY_MCP_TOOLS)
    assert human_only.isdisjoint(set(WORKING_SURFACE_SORTED))
    assert human_only.isdisjoint(set(OPERATOR_SURFACE_SORTED))


# ---------------------------------------------------------------------------
# tools/list wire path — per claim shape (handle_tools_list)
# ---------------------------------------------------------------------------


async def _list_names(operator: Operator) -> list[str]:
    """Return the sorted tool names ``handle_tools_list`` emits for *operator*."""
    result = await handle_tools_list(operator, None)
    return sorted(tool["name"] for tool in result["tools"])


async def test_default_session_wire_listing_is_working_surface() -> None:
    """A non-elevated session's ``tools/list`` wire output is exactly the working surface.

    Role held at ``tenant_admin`` and ``meho-docs`` granted so the only
    thing gating the operator planes out is the absent ``mcp:admin``
    scope — isolating the surface filter as the sole cause.
    """
    op = _operator(capabilities=frozenset({_DOCS_CAPABILITY}))
    assert await _list_names(op) == list(WORKING_SURFACE_SORTED)


async def test_elevated_session_wire_listing_is_full_surface() -> None:
    """An ``mcp:admin`` session's wire output is exactly working + operator."""
    op = _operator(
        capabilities=frozenset({_DOCS_CAPABILITY}),
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )
    assert await _list_names(op) == list(FULL_SURFACE_SORTED)


async def test_elevated_session_without_docs_drops_five_docs_tools() -> None:
    """The surface gate AND-composes with the capability gate on the wire path.

    An elevated session that has not provisioned ``meho-docs`` lists
    everything except the five docs-capability-gated tools (three working
    + two operator), proving the two gates are independent axes end to
    end — the listing-path analogue of #3154's AC3 registry assertion.
    """
    op = _operator(scopes=frozenset({MCP_ADMIN_SCOPE}))
    expected = sorted(set(FULL_SURFACE_SORTED) - _DOCS_CAP_GATED)
    assert await _list_names(op) == expected


async def test_wire_listing_entries_are_spec_shape_no_surface_leak() -> None:
    """Every listed entry is the MCP wire shape and never leaks ``surface``.

    The listing is what a client consumes, so the per-entry contract
    (spec fields present, MEHO-internal ``surface`` absent) is pinned on
    the same path as the membership.
    """
    op = _operator(
        capabilities=frozenset({_DOCS_CAPABILITY}),
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )
    result = await handle_tools_list(op, None)
    for tool in result["tools"]:
        assert {"name", "description", "inputSchema"} <= set(tool)
        assert "surface" not in tool
        assert "required_role" not in tool


# ---------------------------------------------------------------------------
# Transport path — the real /mcp JSON-RPC route (end-to-end)
# ---------------------------------------------------------------------------


def test_tools_list_over_mcp_route_default_session() -> None:
    """``POST /mcp`` tools/list for a non-elevated session returns the working surface.

    Proves the transport → dispatch → handler → wire chain, not just the
    handler in isolation.
    """
    op = _operator(capabilities=frozenset({_DOCS_CAPABILITY}))
    with _client_for(op) as client:
        resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200, resp.text
    names = sorted(tool["name"] for tool in resp.json()["result"]["tools"])
    assert names == list(WORKING_SURFACE_SORTED)


def test_tools_list_over_mcp_route_elevated_session() -> None:
    """``POST /mcp`` tools/list for an ``mcp:admin`` session returns the full surface."""
    op = _operator(
        capabilities=frozenset({_DOCS_CAPABILITY}),
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )
    with _client_for(op) as client:
        resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200, resp.text
    names = sorted(tool["name"] for tool in resp.json()["result"]["tools"])
    assert names == list(FULL_SURFACE_SORTED)
