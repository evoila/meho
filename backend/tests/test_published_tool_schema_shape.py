# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-surface guard on the shape of every tool schema MEHO *publishes* (#2644).

The Anthropic Messages API requires each tool's ``input_schema`` to be a
plain object schema at the root: a top-level ``oneOf`` / ``allOf`` / ``anyOf``
is rejected outright, and because the API validates the whole ``tools`` array
a single offender 400s every request in that session — not just calls to the
offending tool. MEHO reaches that API through two publishers:

* the **MCP surface**, where an MCP client (Claude Code, Inspector) forwards
  each ``tools/list`` entry verbatim as a custom tool, and
* the **hosted-agent surface**, where
  :func:`~meho_backplane.agent.toolset.resolve_agent_tools` hands each
  :class:`~meho_backplane.agent.meta_tool.MetaToolSpec`'s
  ``parameter_schema`` to ``pydantic_ai.Tool.from_schema``, which forwards it
  to the configured provider.

Both publishers are swept here. The MCP half passes on arrival and is a
regression guard: :meth:`~meho_backplane.mcp.registry.ToolDefinition.to_wire`
has stripped top-level combinators since #905, so the only way to reintroduce
one is to bypass ``to_wire`` — which is exactly what the agent bridge did
until #2644 (it deep-copied the *registered* ``inputSchema``, republishing
``meho.broadcast.watch``'s ``cursor``/``since_cursor`` XOR and killing every
hosted Anthropic run at model-init with ``turns: 0``).

**Registered schemas are deliberately out of scope.** Eight registered
``inputSchema`` dicts carry a top-level combinator on purpose — it is the
server-side validation layer ``mcp/handlers.py`` runs ``jsonschema.validate``
against. Those stay; only the published copies are constrained. The design
pins for that split live in ``tests/test_mcp_registry.py`` (``to_wire``
strips, ``inputSchema`` keeps) and
``tests/test_mcp_tools_list_shape_conventions.py`` (the watch XOR's exact
registered shape).

Related narrower guard: ``tests/test_mcp_tools_topology.py`` asserts the same
combinator property over the RBAC-filtered ``tools/list`` view, from the
topology tools' own #905 regression. This file is the cross-surface superset
— it adds the agent catalog, the ``type: object`` root requirement, and the
capability-gated tools an unprovisioned operator never sees.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.agent.meta_tool import MetaToolSpec
from meho_backplane.agent.toolset import _META_TOOL_CATALOG, resolve_agent_tools
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import ToolDefinition
from tests.mcp_test_fixtures import (
    build_operator,
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

#: The JSON-Schema combinator keywords the Messages API rejects at the root of
#: a tool's ``input_schema``. Nested occurrences (inside ``properties`` /
#: ``items`` / ``$defs``) are legal and are not swept.
_FORBIDDEN_ROOT_KEYS: tuple[str, ...] = ("oneOf", "allOf", "anyOf")


def _root_violations(schema: dict[str, Any]) -> list[str]:
    """Return the reasons *schema* is not a publishable ``input_schema``.

    Empty list means the root is a plain object schema with no combinator —
    the only shape the Messages API accepts.
    """
    reasons = [f"top-level {key}" for key in _FORBIDDEN_ROOT_KEYS if key in schema]
    if schema.get("type") != "object":
        reasons.append(f"root type is {schema.get('type')!r}, expected 'object'")
    return reasons


def _registered_tools() -> list[ToolDefinition]:
    """Every tool in the MCP registry, unfiltered by role or capability.

    Read from the registry mapping rather than
    :func:`~meho_backplane.mcp.registry.all_tools_for` on purpose: that helper
    is the RBAC + capability *filter*, so even a ``tenant_admin`` sweep would
    silently skip the capability-gated ``meho-docs`` tools (and any future
    gated tool) — the exact blind spot a completeness guard must not have.
    There is no public unfiltered enumeration, and inventing one for a test is
    not worth widening the registry's API surface.
    """
    return [defn for defn, _handler in mcp_registry._TOOLS.values()]


def _agent_catalog() -> tuple[MetaToolSpec, ...]:
    """The full agent meta-tool catalog, before any toolset/role intersection."""
    return _META_TOOL_CATALOG


def test_mcp_wire_schemas_are_publishable() -> None:
    """No registered tool's ``to_wire()`` schema can reach the Messages API malformed."""
    offenders = {
        defn.name: reasons
        for defn in _registered_tools()
        if (reasons := _root_violations(defn.to_wire()["inputSchema"]))
    }
    assert offenders == {}, (
        "MCP tools publish an input_schema the Anthropic Messages API rejects "
        f"(a single offender 400s the whole tools array): {offenders}"
    )


def test_mcp_registry_sweep_is_not_vacuous() -> None:
    """Guard the guard: an empty registry would make the sweep above pass trivially."""
    assert len(_registered_tools()) > 50


def test_agent_meta_tool_schemas_are_publishable() -> None:
    """No agent meta-tool's ``parameter_schema`` can reach the Messages API malformed.

    This is the half that regressed in #2644: the broadcast bridge published
    the registered ``inputSchema`` instead of the wire copy, so
    ``broadcast_watch`` shipped a top-level ``anyOf`` to every hosted run.
    """
    offenders = {
        spec.name: reasons
        for spec in _agent_catalog()
        if (reasons := _root_violations(spec.parameter_schema))
    }
    assert offenders == {}, (
        "agent meta-tools publish a parameter_schema the Anthropic Messages "
        f"API rejects (a single offender 400s the whole run at model-init): {offenders}"
    )


def test_agent_catalog_sweep_is_not_vacuous() -> None:
    """Guard the guard: the catalog must actually carry the tools being swept."""
    names = {spec.name for spec in _agent_catalog()}
    assert {"call_operation", "broadcast_watch"} <= names


def test_default_toolset_assembles_only_publishable_schemas() -> None:
    """The default (empty-toolset) agent loop registers Messages-API-legal tools.

    The end-to-end shape of the #2644 bug: an agent run with an empty toolset
    spec — no meta-tool allow-list, so the whole catalog — died at model-init
    on ``tools.5.custom.input_schema``, which is ``broadcast_watch`` in catalog
    order. This asserts against the objects the loop actually registers
    (``pydantic_ai.Tool``), one step past the catalog sweep above, so a future
    adapter that re-derived the schema on the way to ``Tool.from_schema``
    would still be caught.
    """
    operator: Operator = build_operator(TenantRole.OPERATOR)
    tools = resolve_agent_tools({}, operator)
    assert tools, "the default toolset must register at least one tool"

    offenders = {
        tool.name: reasons
        for tool in tools
        if (reasons := _root_violations(tool.function_schema.json_schema))
    }
    assert offenders == {}, (
        f"the default agent tool list carries schemas the Messages API rejects: {offenders}"
    )
