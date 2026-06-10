# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schema-pairwise pinning test for the MCP ``tools/list`` surface.

G0.18-T5 (#1358) of Initiative #1353 closed the SEV-4 shape
inconsistencies surfaced by ``claude-rdc-hetzner-dc#789`` Finding N4:
the 51-tool ``tools/list`` had sibling-tool drift around the
forward-cursor parameter name, the ``<noun>_id`` UUID convention,
the cross-tenant ``tenant_id`` name, the ``status`` enum surface, the
``limit`` / ``offset`` defaults, the ``op_class`` audit / broadcast
enum, and the ``register.name`` constraint enforcement.

This file pins the reconciled conventions structurally so a future
drift fails CI at unit-test time rather than surfacing as the next
RDC dogfood-cycle finding. Each convention has one ``test_*`` per
named drift; the test reads the ``inputSchema`` off each tool's
:class:`~meho_backplane.mcp.registry.ToolDefinition` and asserts the
fields that govern the wire shape.

The convention citations live in
``docs/codebase/api-shape-conventions.md`` §14 ("Intra-MCP tools/list
shape parity").
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from meho_backplane.broadcast import OP_CLASS_ENUM
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import ToolDefinition

# Importing the tool modules registers the side-effect tool definitions.
# pylint: disable=unused-import
from meho_backplane.mcp.tools import (  # noqa: F401
    agent_principals,
    agents,
    approvals,
    audit,
    broadcast,
    connector_admin,
    operations,
    runbook_runs,
    runbooks,
    scheduler,
    topology,
)


def _tool_def(name: str) -> ToolDefinition:
    """Resolve a registered tool by wire-name, asserting it exists."""
    entry = mcp_registry.get_tool(name)
    assert entry is not None, f"missing MCP tool registration: {name!r}"
    defn, _handler = entry
    return defn


def _input_schema(name: str) -> dict[str, Any]:
    return _tool_def(name).inputSchema


def _properties(name: str) -> dict[str, Any]:
    return _input_schema(name).get("properties", {})


# ---------------------------------------------------------------------------
# §14.1 — `op_class` enum parity between audit and broadcast
# ---------------------------------------------------------------------------


def test_query_audit_op_class_enum_matches_broadcast_enum() -> None:
    """`query_audit.op_class` carries the same enum as broadcast filters.

    RDC #789 N4 caught the v0.8.0 wire drift: `query_audit.op_class`
    advertised five values in prose (`read` / `write` /
    `credential_read` / `audit_query` / `other`) while
    `meho.broadcast.recent.filter.op_class` and
    `meho.broadcast.watch.filter.op_class` enumerated six (the five
    plus `credential_mint`). An operator filtering audit for a
    freshly-minted credential operation (`harbor.robot.create`) found
    the filter syntactically rejected on one surface and accepted on
    the other. Convention §14.1: the audit op_class enum IS the
    broadcast op_class enum; one source.
    """
    audit_enum = _properties("query_audit")["op_class"]["enum"]
    # `null` is included so the `["string", "null"]` type isn't
    # narrowed by the enum (omitted = no filter).
    assert None in audit_enum
    audit_values = [v for v in audit_enum if v is not None]
    assert tuple(audit_values) == tuple(OP_CLASS_ENUM), (
        f"query_audit.op_class drifted from OP_CLASS_ENUM: "
        f"got {audit_values}, want {list(OP_CLASS_ENUM)}"
    )
    # The broadcast filters use the same enum.
    for tool_name in ("meho.broadcast.recent", "meho.broadcast.watch"):
        broadcast_enum = _properties(tool_name)["filter"]["properties"]["op_class"]["enum"]
        assert tuple(broadcast_enum) == tuple(OP_CLASS_ENUM), (
            f"{tool_name}.filter.op_class drifted from OP_CLASS_ENUM: "
            f"got {broadcast_enum}, want {list(OP_CLASS_ENUM)}"
        )
    # credential_mint specifically — the missing value that triggered
    # the finding. Pin its presence on every surface that filters
    # op_class so a future re-removal trips this assertion.
    assert "credential_mint" in audit_values
    for tool_name in ("meho.broadcast.recent", "meho.broadcast.watch"):
        filter_enum = _properties(tool_name)["filter"]["properties"]["op_class"]["enum"]
        assert "credential_mint" in filter_enum


# ---------------------------------------------------------------------------
# §14.2 — Forward-cursor parameter named `cursor` everywhere
# ---------------------------------------------------------------------------

# Tools whose forward-pagination parameter MUST be `cursor` (the
# canonical name). For broadcast.recent / .watch the canonical name
# coexists with a deprecated alias, exercised separately below.
_TOOLS_WITH_CURSOR_PARAM: tuple[str, ...] = (
    "query_audit",
    "query_topology",
    "list_targets",
    "list_operation_groups",
    "meho.broadcast.recent",
    "meho.broadcast.watch",
)


@pytest.mark.parametrize("tool_name", _TOOLS_WITH_CURSOR_PARAM)
def test_paginating_read_tools_expose_cursor_parameter(tool_name: str) -> None:
    """Every paginating MCP read tool exposes `cursor` (canonical name).

    Convention §14.2: forward-pagination is named `cursor`. The
    v0.8.0 wire shape varied (`since` on broadcast.recent,
    `since_cursor` on broadcast.watch, `cursor` everywhere else); the
    convention is one name across the surface, with the alias names
    kept as deprecated for one cycle.
    """
    properties = _properties(tool_name)
    assert "cursor" in properties, (
        f"{tool_name}: missing canonical `cursor` parameter (properties={sorted(properties)})"
    )


def test_broadcast_recent_carries_since_alias_marked_deprecated() -> None:
    """`meho.broadcast.recent.since` survives as deprecated alias for `cursor`.

    Migration shape: `since` was the v0.8.0 name; we keep it accepted
    for backward compat but flag it `deprecated` so a schema-driven
    client (Inspector, etc.) renders the migration nudge.
    """
    properties = _properties("meho.broadcast.recent")
    assert properties["since"].get("deprecated") is True
    assert "deprecated" not in properties["cursor"]


def test_broadcast_watch_carries_since_cursor_alias_marked_deprecated() -> None:
    """`meho.broadcast.watch.since_cursor` survives as deprecated alias for `cursor`."""
    properties = _properties("meho.broadcast.watch")
    assert properties["since_cursor"].get("deprecated") is True
    assert "deprecated" not in properties["cursor"]


def test_broadcast_watch_required_anyof_accepts_either_alias() -> None:
    """The `cursor` ↔ `since_cursor` XOR is structurally pinned on broadcast.watch.

    Without the `anyOf` form, declaring the canonical `cursor` as
    `required` would break v0.8.0 callers passing `since_cursor`; we
    accept either at the schema layer and let the handler enforce the
    mutual exclusion.
    """
    schema = _input_schema("meho.broadcast.watch")
    assert schema["anyOf"] == [
        {"required": ["cursor"]},
        {"required": ["since_cursor"]},
    ]
    # The legacy single-key `required` array is dropped — the anyOf
    # is the only required-shape declaration.
    assert "required" not in schema


# ---------------------------------------------------------------------------
# §14.3 — `<noun>_id` UUID parameter convention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ("meho.approvals.get", "meho.approvals.approve", "meho.approvals.reject"),
)
def test_approvals_tools_expose_approval_request_id_canonical_name(
    tool_name: str,
) -> None:
    """The approval-request UUID parameter is named `approval_request_id`.

    Convention §14.3: every MCP tool that names a resource UUID uses
    the `<noun>_id` form (`trigger_id`, `agent_session_id`,
    `audit_id`). The v0.8.0 wire shape used a bare `id` on the
    approvals tools — the only sibling-drift remaining at that point.
    The bare `id` is retained as a deprecated alias for one cycle.
    """
    properties = _properties(tool_name)
    assert "approval_request_id" in properties, (
        f"{tool_name}: missing canonical `approval_request_id` (properties={sorted(properties)})"
    )
    assert properties["approval_request_id"]["format"] == "uuid"
    # The deprecated `id` alias survives for backward compat.
    assert "id" in properties
    assert properties["id"].get("deprecated") is True
    # Either alias name satisfies the schema-level required check.
    schema = _input_schema(tool_name)
    assert schema["anyOf"] == [
        {"required": ["approval_request_id"]},
        {"required": ["id"]},
    ]
    assert "required" not in schema


# ---------------------------------------------------------------------------
# §14.4 — Cross-tenant scope parameter named `tenant_id`
# ---------------------------------------------------------------------------


def test_list_targets_tenant_argument_is_tenant_id_with_legacy_alias() -> None:
    """`list_targets` exposes the cross-tenant scope as `tenant_id`.

    Convention §14.4: the cross-tenant scope parameter is named
    `tenant_id` (the form used by `meho.connector.*` and
    `meho.scheduler.create`). The v0.8.0 `list_targets.tenant` is
    retained as a deprecated alias.

    Accepted shape asymmetry: `list_targets.tenant_id` accepts slug
    OR UUID (preserved for backward compat); the connector / scheduler
    tools accept UUID-only. Documented in
    `docs/codebase/api-shape-conventions.md` §14.
    """
    properties = _properties("list_targets")
    assert "tenant_id" in properties
    assert "tenant" in properties
    assert properties["tenant"].get("deprecated") is True
    assert "deprecated" not in properties["tenant_id"]


@pytest.mark.parametrize(
    "tool_name",
    (
        "meho.connector.ingest",
        "meho.connector.review",
        "meho.connector.edit_group",
        "meho.connector.edit_op",
        "meho.connector.enable",
        "meho.connector.disable",
        "meho.scheduler.create",
    ),
)
def test_admin_tools_use_tenant_id_uuid_only(tool_name: str) -> None:
    """Admin tools name the cross-tenant scope `tenant_id` (UUID-only)."""
    properties = _properties(tool_name)
    assert "tenant_id" in properties, (
        f"{tool_name}: missing `tenant_id` (properties={sorted(properties)})"
    )
    assert properties["tenant_id"]["format"] == "uuid"


# ---------------------------------------------------------------------------
# §14.5 — `limit` / `offset` defaults declared in-schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, expected_limit_default, expected_offset_default",
    (
        ("meho.approvals.list", 50, 0),
        ("meho.scheduler.list", 100, 0),
    ),
)
def test_list_tools_declare_limit_offset_defaults_in_schema(
    tool_name: str,
    expected_limit_default: int,
    expected_offset_default: int,
) -> None:
    """`limit` / `offset` defaults are declared in-schema, not prose-only.

    Convention §14.5: every paginating list tool declares
    `default: <N>` on `limit` and `offset` (and on `cursor` when
    applicable). A schema-driven MCP client renders the documented
    default; prose-only defaults force the client to guess.
    """
    properties = _properties(tool_name)
    assert properties["limit"]["default"] == expected_limit_default
    assert properties["offset"]["default"] == expected_offset_default


def test_query_audit_limit_default_declared() -> None:
    """`query_audit.limit` declares its default in-schema."""
    assert _properties("query_audit")["limit"]["default"] == 100


def test_list_targets_limit_default_declared() -> None:
    """`list_targets.limit` declares its default in-schema."""
    assert _properties("list_targets")["limit"]["default"] == 100


def test_list_operation_groups_limit_default_declared() -> None:
    """`list_operation_groups.limit` declares its default in-schema."""
    assert _properties("list_operation_groups")["limit"]["default"] == 100


# ---------------------------------------------------------------------------
# §14.6 — `*.list.status` is a JSON enum (not prose-only)
# ---------------------------------------------------------------------------


def test_approvals_list_status_is_json_enum() -> None:
    """`meho.approvals.list.status` surfaces as a JSON-Schema enum.

    Convention §14.6: list-tool `status` filters declare their allowed
    values via `enum`, not in prose. Pairs with
    `meho.scheduler.list.status` (which already used the JSON enum
    shape).
    """
    status_schema = _properties("meho.approvals.list")["status"]
    assert "enum" in status_schema, (
        f"meho.approvals.list.status: expected `enum` declaration (got {sorted(status_schema)})"
    )
    assert status_schema["enum"] == [
        "pending",
        "approved",
        "rejected",
        "expired",
        "all",
    ]
    assert status_schema["default"] == "pending"


def test_scheduler_list_status_remains_json_enum() -> None:
    """`meho.scheduler.list.status` is (and remains) a JSON enum."""
    status_schema = _properties("meho.scheduler.list")["status"]
    assert status_schema["enum"] == ["active", "paused", "cancelled", "fired"]


# ---------------------------------------------------------------------------
# §14.7 — `agent_principals.register.name` enforces its documented pattern
# ---------------------------------------------------------------------------


def test_agent_principals_register_name_enforces_documented_alphabet() -> None:
    """`meho.agent_principals.register.name` carries the documented pattern.

    Convention §14.7: when a tool documents an alphabet for a name
    field, the JSON-Schema declares that alphabet via `pattern`. The
    v0.8.0 `agent_principals.register.name` documented its safe
    alphabet in prose only; the service-layer regex validated
    post-dispatch, so an MCP client schema-validating ahead of the
    call never saw the constraint. `meho.agents.create.name` already
    carried the pattern — this pins parity.
    """
    name_schema = _properties("meho.agent_principals.register")["name"]
    assert "pattern" in name_schema
    assert name_schema["pattern"] == r"^[A-Za-z0-9_\-\.]+$"
    assert name_schema["minLength"] == 1
    assert name_schema["maxLength"] == 128
    # Sanity-check the pattern matches the documented service-side regex.
    assert re.fullmatch(name_schema["pattern"], "agent.with-allowed_chars.42")
    assert re.fullmatch(name_schema["pattern"], "bad/slash") is None


def test_agents_create_name_remains_pattern_constrained() -> None:
    """`meho.agents.create.name` is (and remains) pattern-constrained."""
    name_schema = _properties("meho.agents.create")["name"]
    assert name_schema["pattern"] == r"^[A-Za-z0-9_\-\.]+$"


# ---------------------------------------------------------------------------
# §14.8 — `list_operation_groups` paginates like `list_targets`
# ---------------------------------------------------------------------------


def test_list_operation_groups_paginates_keyset_on_group_key() -> None:
    """`list_operation_groups` exposes `limit` + `cursor` keyset pagination.

    Convention §14.8: every list-shaped MCP tool paginates. The v0.8.0
    `list_operation_groups` was unpaginated (no `limit`, no `cursor`,
    no `next_cursor`) while its sibling `list_targets` was keyset-
    paginated. This test pins the structural fix.
    """
    properties = _properties("list_operation_groups")
    assert "limit" in properties
    assert "cursor" in properties
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 500
    assert properties["limit"]["default"] == 100
    # The output schema documents `next_cursor` so a schema-driven
    # client knows the round-trip handle exists.
    output = _tool_def("list_operation_groups").outputSchema
    assert output is not None
    assert "next_cursor" in output["properties"]
    assert "next_cursor" in output["required"]


# ---------------------------------------------------------------------------
# §14.9 — runbook family: dotted canonical names + template_slug field
# ---------------------------------------------------------------------------

#: The 11 runbook pairs (#1612): flat v0.12 name → canonical dotted name.
_RUNBOOK_NAME_PAIRS = (
    ("runbook_draft_template", "meho.runbook.draft_template"),
    ("runbook_edit_template", "meho.runbook.edit_template"),
    ("runbook_publish_template", "meho.runbook.publish_template"),
    ("runbook_deprecate_template", "meho.runbook.deprecate_template"),
    ("runbook_list_templates", "meho.runbook.list_templates"),
    ("runbook_show_template", "meho.runbook.show_template"),
    ("runbook_start", "meho.runbook.start"),
    ("runbook_next", "meho.runbook.next"),
    ("runbook_abort", "meho.runbook.abort"),
    ("runbook_reassign", "meho.runbook.reassign"),
    ("runbook_list_runs", "meho.runbook.list_runs"),
)


@pytest.mark.parametrize(("flat_name", "dotted_name"), _RUNBOOK_NAME_PAIRS)
def test_runbook_tools_dotted_canonical_with_flat_deprecated_alias(
    flat_name: str,
    dotted_name: str,
) -> None:
    """Convention §14.9: runbooks follow the dotted `meho.<noun>.<verb>` grammar.

    The runbook family was the last flat multi-verb family on the
    surface (#1612). The dotted name is canonical; the flat name stays
    registered as a deprecated alias routed to the same handler for one
    release (removed in v0.14.0).
    """
    dotted_entry = mcp_registry.get_tool(dotted_name)
    flat_entry = mcp_registry.get_tool(flat_name)
    assert dotted_entry is not None, dotted_name
    assert flat_entry is not None, flat_name
    # Canonical carries no deprecation marker; the alias points home.
    assert dotted_entry[0].deprecated_alias_for is None
    assert flat_entry[0].deprecated_alias_for == dotted_name
    assert flat_entry[0].description.startswith(f"DEPRECATED alias for `{dotted_name}`")
    # Same handler object — alias, not fork.
    assert flat_entry[1] is dotted_entry[1]


@pytest.mark.parametrize(
    ("tool_name", "other_required"),
    (
        ("meho.runbook.draft_template", ["body"]),
        ("meho.runbook.edit_template", ["body"]),
        ("meho.runbook.publish_template", ["version"]),
        ("meho.runbook.deprecate_template", ["version"]),
        ("meho.runbook.show_template", None),
    ),
)
def test_runbook_template_tools_expose_template_slug_canonical_name(
    tool_name: str,
    other_required: list[str] | None,
) -> None:
    """Convention §14.9: the template id is `template_slug` on every input.

    The v0.12 wire split the same identifier across `slug` (template
    verbs) and `template_slug` (run verbs); #1612 unifies on the more
    specific `template_slug` everywhere, keeping `slug` as a deprecated
    alias for one cycle — the `cursor`/`since` and
    `approval_request_id`/`id` migration shape applied to the runbook
    family.
    """
    properties = _properties(tool_name)
    assert "template_slug" in properties, (
        f"{tool_name}: missing canonical `template_slug` (properties={sorted(properties)})"
    )
    assert "deprecated" not in properties["template_slug"]
    assert properties["slug"].get("deprecated") is True
    # Either alias name satisfies the schema-level required check; the
    # handler enforces the XOR.
    schema = _input_schema(tool_name)
    assert schema["anyOf"] == [
        {"required": ["template_slug"]},
        {"required": ["slug"]},
    ]
    if other_required is None:
        # The template id is the only required input — the anyOf is the
        # only required-shape declaration (broadcast.watch precedent).
        assert "required" not in schema
    else:
        assert schema["required"] == other_required


@pytest.mark.parametrize(
    "tool_name",
    ("meho.runbook.start", "meho.runbook.list_runs"),
)
def test_runbook_run_tools_keep_template_slug_field(tool_name: str) -> None:
    """Convention §14.9: the run verbs already used `template_slug` — pinned."""
    properties = _properties(tool_name)
    assert "template_slug" in properties
    assert "slug" not in properties
