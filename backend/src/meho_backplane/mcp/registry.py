# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tool + resource registries — the substrate G3-G9 verbs register against.

Two parallel registries, mirroring the MCP 2025-06-18 separation of tools
(active — invoked by ``tools/call``) and resources (passive — fetched by
``resources/read``):

* **Tool registry** — name → ``(ToolDefinition, ToolHandler)``. The
  definition carries the JSON-Schema ``inputSchema`` MCP clients pass to
  the LLM as the tool's contract, plus two MEHO-internal fields used for
  server-side filtering (``required_role``, ``op_class``). The handler
  receives the validated :class:`Operator` and the (jsonschema-validated)
  arguments dict, and returns a structured result.
* **Resource-template registry** — uriTemplate → ``(ResourceTemplateDefinition,
  ResourceHandler)``. v0.2 ships only templated resources (every URI carries
  at least one ``{var}`` placeholder); concrete-URI resources can be added
  later by registering a template with zero variables. The handler receives
  the bound template variables as a dict and returns the resource contents.

Spec correctness note (vs. issue #248 body)
===========================================

The issue body's AC says ``resources/list`` returns the registered
resources. The MCP 2025-06-18 spec is stricter: **concrete** resources go
through ``resources/list``; **templated** resources go through
``resources/templates/list``. This module separates the two faithfully
— every resource registered in v0.2 carries a template, so v0.2's
``resources/list`` response is empty and ``resources/templates/list``
carries the registry. The cost of this divergence is one extra
JSON-RPC method (`/resources/templates/list`) vs. the issue body's
conflation; the benefit is that spec-conforming clients (MCP Inspector,
Claude Desktop) get the right shape and don't have to guess which
resources are templated vs. concrete.

RBAC filtering
==============

Every registered item carries a ``required_role`` (one of the three
:class:`~meho_backplane.auth.operator.TenantRole` values). The list
methods filter the registry against the calling operator's role; tools
or resources above the operator's role rank don't appear in the
response. The role ranking is the order the closed enum was declared
(``read_only`` < ``operator`` < ``tenant_admin``) — see
:func:`role_at_least`.

Eager import
============

The :func:`eager_import_mcp_modules` helper walks every module under
``meho_backplane.mcp.tools`` and ``meho_backplane.mcp.resources`` so the
side-effect-only registration calls at the top of each module file run
at app boot. Called from the FastAPI ``lifespan`` so the first incoming
``tools/list`` request finds the registry already populated.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, TenantRole

__all__ = [
    "ResourceHandler",
    "ResourceTemplateDefinition",
    "ToolDefinition",
    "ToolHandler",
    "all_resource_templates_for",
    "all_tools_for",
    "clear_registries",
    "eager_import_mcp_modules",
    "get_resource_for_uri",
    "get_tool",
    "register_mcp_resource",
    "register_mcp_tool",
    "role_at_least",
]

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Handler type aliases
# ---------------------------------------------------------------------------


#: A tool handler receives the validated operator and the
#: jsonschema-validated arguments dict, and returns a JSON-serialisable dict
#: that the :func:`~meho_backplane.mcp.handlers.handle_tools_call` wrapper
#: packs into the MCP ``content`` array. Returning ``isError`` semantics
#: (tool-execution errors per MCP spec §Error Handling) is currently
#: out of scope for the registry; T4 reference tools wire it.
ToolHandler = Callable[[Operator, dict[str, Any]], Awaitable[dict[str, Any]]]

#: A resource handler receives the operator and the URI-template variables
#: bound from the concrete URI, and returns the resource contents.
ResourceHandler = Callable[[Operator, dict[str, str]], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


#: Ordering of :class:`TenantRole` from least-privileged to most-privileged.
#: A pinned tuple — not a runtime sort of ``TenantRole`` members — because
#: the closed enum's declaration order is the policy contract; sorting at
#: runtime would couple this module to the ``StrEnum`` member iteration
#: order, which is fragile across Python versions.
_ROLE_RANK: tuple[TenantRole, ...] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)


def role_at_least(actual: TenantRole, required: TenantRole) -> bool:
    """Return True when *actual* meets or exceeds *required* in :data:`_ROLE_RANK`.

    Centralised so the RBAC filter shape stays single-source for both
    :func:`all_tools_for` / :func:`all_resource_templates_for` (list-time
    filter) and the call-time re-check in
    :mod:`~meho_backplane.mcp.handlers`. Public so the handlers module
    can import it at module level rather than dipping into a private
    sibling symbol.
    """
    return _ROLE_RANK.index(actual) >= _ROLE_RANK.index(required)


class ToolDefinition(BaseModel):
    """MCP tool definition + MEHO-internal RBAC metadata.

    The wire shape exposed via ``tools/list`` is derived through
    :meth:`to_wire`, which drops the MEHO-internal fields (``required_role``,
    ``op_class``) — clients don't need them and they'd leak server-side
    policy detail.

    ``inputSchema`` is a JSON Schema 2020-12 object the handler validates
    incoming ``tools/call.arguments`` against. ``outputSchema`` is
    optional; when present the handler's return value is also validated
    against it (T4 reference tool wires this).
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    inputSchema: dict[str, Any]  # noqa: N815 — wire field is camelCase per MCP spec
    title: str | None = None
    outputSchema: dict[str, Any] | None = None  # noqa: N815
    required_role: TenantRole = TenantRole.OPERATOR
    op_class: str = "read"

    def to_wire(self) -> dict[str, Any]:
        """Serialise to the MCP wire shape, dropping MEHO-internal fields.

        Spec fields kept: ``name``, ``description``, ``inputSchema``,
        optional ``title`` / ``outputSchema``. MEHO fields dropped:
        ``required_role``, ``op_class``. The drop is deliberate — clients
        don't need server-side RBAC details and shouldn't see them.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema,
        }
        if self.title is not None:
            out["title"] = self.title
        if self.outputSchema is not None:
            out["outputSchema"] = self.outputSchema
        return out


class ResourceTemplateDefinition(BaseModel):
    """MCP resource-template definition + MEHO-internal RBAC metadata.

    The wire shape exposed via ``resources/templates/list`` is derived
    through :meth:`to_wire`; ``required_role`` is dropped for the same
    reason it is on :class:`ToolDefinition`.

    ``uriTemplate`` follows the RFC 6570 syntax for ``{var}`` substitution.
    v0.2 supports only simple variable substitution (no expression
    operators like ``+`` / ``#`` / ``?``); :func:`_match_uri_template`
    documents what it actually parses.
    """

    model_config = ConfigDict(frozen=True)

    uriTemplate: str = Field(min_length=1)  # noqa: N815 — wire field per MCP spec
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    mimeType: str = "application/json"  # noqa: N815
    title: str | None = None
    required_role: TenantRole = TenantRole.OPERATOR

    def to_wire(self) -> dict[str, Any]:
        """Serialise to the MCP wire shape, dropping MEHO-internal fields."""
        out: dict[str, Any] = {
            "uriTemplate": self.uriTemplate,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mimeType,
        }
        if self.title is not None:
            out["title"] = self.title
        return out


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------


_TOOLS: dict[str, tuple[ToolDefinition, ToolHandler]] = {}
_RESOURCES: dict[str, tuple[ResourceTemplateDefinition, ResourceHandler]] = {}


# ---------------------------------------------------------------------------
# Registration API
# ---------------------------------------------------------------------------


def register_mcp_tool(definition: ToolDefinition, handler: ToolHandler) -> None:
    """Register a tool keyed by :attr:`ToolDefinition.name`.

    Idempotent in the sense that double-registration of the same name is a
    programmer error and surfaces immediately. The error is a
    :class:`RuntimeError` (not a wire-error) because the registry is
    populated at module-import time; a duplicate is a configuration bug
    the operator must fix before the process accepts traffic.
    """
    if definition.name in _TOOLS:
        raise RuntimeError(f"MCP tool already registered: {definition.name!r}")
    _TOOLS[definition.name] = (definition, handler)
    _log.info("mcp_tool_registered", name=definition.name)


def register_mcp_resource(
    definition: ResourceTemplateDefinition,
    handler: ResourceHandler,
) -> None:
    """Register a resource template keyed by :attr:`ResourceTemplateDefinition.uriTemplate`.

    Same idempotency contract as :func:`register_mcp_tool` — duplicates
    raise immediately because the registry is populated at module-import
    time and a collision indicates two modules trying to own the same
    URI namespace.

    Three flavours of bad input are rejected:

    * **Duplicate placeholder names** — the ``uriTemplate`` reuses the
      same ``{var}`` name more than once (e.g.
      ``meho://tenant/{id}/{id}``). :func:`_match_uri_template` would
      build a regex with two identically-named capture groups, which
      Python's :mod:`re` engine raises :class:`re.error` on at compile
      time. The crash would surface at first ``resources/read`` match
      attempt as a JSON-RPC INTERNAL_ERROR; reject at registration
      instead so the misconfiguration is loud and pre-traffic.
    * **Exact duplicate** — the ``uriTemplate`` string is already
      registered.
    * **Same-shape collision** — two templates that differ only in
      variable names (e.g. ``meho://kb/{slug}`` vs ``meho://kb/{id}``)
      would produce identical match regexes in
      :func:`_match_uri_template` and silently shadow each other in
      :func:`get_resource_for_uri` based on registration order. Reject
      these at registration time so the failure surfaces immediately
      instead of as a dead-code handler the operator can't see fire.
    """
    var_names = _TEMPLATE_VAR_RE.findall(definition.uriTemplate)
    if len(var_names) != len(set(var_names)):
        raise RuntimeError(
            f"MCP resource template reuses placeholder names: {definition.uriTemplate!r}",
        )
    if definition.uriTemplate in _RESOURCES:
        raise RuntimeError(
            f"MCP resource template already registered: {definition.uriTemplate!r}",
        )
    incoming_shape = _canonical_template_shape(definition.uriTemplate)
    for existing_template in _RESOURCES:
        if _canonical_template_shape(existing_template) == incoming_shape:
            raise RuntimeError(
                "MCP resource template shape already registered: "
                f"{definition.uriTemplate!r} conflicts with "
                f"{existing_template!r} (variable names differ but the "
                "match shape is identical)",
            )
    _RESOURCES[definition.uriTemplate] = (definition, handler)
    _log.info("mcp_resource_registered", uri_template=definition.uriTemplate)


def get_tool(name: str) -> tuple[ToolDefinition, ToolHandler] | None:
    """Return ``(definition, handler)`` for *name*, or ``None`` if unregistered."""
    return _TOOLS.get(name)


def get_resource_for_uri(
    uri: str,
) -> tuple[ResourceTemplateDefinition, ResourceHandler, dict[str, str]] | None:
    """Match a concrete URI against every registered template.

    Returns ``(definition, handler, bound_params)`` on the first matching
    template, or ``None`` if no template matches. Iteration order is
    registration order, which is deterministic per process (Python 3.7+
    dict guarantee); collisions in the URI namespace would have failed
    :func:`register_mcp_resource` earlier so the first-match rule never
    has to disambiguate.
    """
    for template, (defn, handler) in _RESOURCES.items():
        bound = _match_uri_template(uri, template)
        if bound is not None:
            return defn, handler, bound
    return None


def all_tools_for(operator: Operator) -> list[ToolDefinition]:
    """Return tools the operator's :class:`TenantRole` admits, in registration order."""
    return [
        defn
        for defn, _ in _TOOLS.values()
        if role_at_least(operator.tenant_role, defn.required_role)
    ]


def all_resource_templates_for(
    operator: Operator,
) -> list[ResourceTemplateDefinition]:
    """Return resource templates the operator's role admits, in registration order."""
    return [
        defn
        for defn, _ in _RESOURCES.values()
        if role_at_least(operator.tenant_role, defn.required_role)
    ]


# ---------------------------------------------------------------------------
# URI-template matching (RFC 6570 simple-expansion subset)
# ---------------------------------------------------------------------------


#: Regex matching a single ``{var}`` placeholder in an RFC 6570 template.
#: v0.2 supports only this simple-expansion subset; the expression-operator
#: syntax (``{+var}`` reserved expansion, ``{#var}`` fragment, ``{?var}``
#: query, ``{var*}`` exploded) is intentionally unsupported — the spec
#: allows servers to accept any subset, and v0.2's resources never need
#: query/fragment/explosion semantics.
_TEMPLATE_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _canonical_template_shape(template: str) -> str:
    """Strip variable *names* from a URI template so structurally-equal
    templates collapse to the same key.

    The matcher in :func:`_match_uri_template` builds its regex from the
    *positions* of the ``{var}`` placeholders; the variable names show
    up only as named groups inside that template's own compiled regex.
    Two templates differing only in variable name (``meho://kb/{slug}``
    vs ``meho://kb/{id}``) therefore produce identical match behaviour
    and would silently shadow each other in :func:`get_resource_for_uri`
    based on registration order.

    Collapsing each ``{var}`` to a fixed ``{}`` placeholder yields a
    canonical shape key that :func:`register_mcp_resource` checks for
    collisions at registration time so the failure is loud (RuntimeError
    at boot) rather than silent (the second handler never fires).
    """
    return _TEMPLATE_VAR_RE.sub("{}", template)


def _match_uri_template(uri: str, template: str) -> dict[str, str] | None:
    """Match *uri* against *template* and return the bound variables.

    Compiles the template into a regex by replacing each ``{var}`` with a
    non-greedy capture and anchoring the result. Returns ``None`` when the
    URI doesn't match the template's shape, or a dict of
    ``{var_name: captured_value}`` when it does. Non-variable portions of
    the template must match exactly (literal, character-for-character).

    Examples::

        _match_uri_template("meho://tenant/abc/info", "meho://tenant/{id}/info")
            == {"id": "abc"}
        _match_uri_template("meho://kb/notes", "meho://tenant/{id}/info")
            == None
    """
    # Replace each `{var}` with a named regex capture; escape literal parts.
    var_names: list[str] = []

    def _to_capture(match: re.Match[str]) -> str:
        var = match.group(1)
        var_names.append(var)
        # Non-greedy `[^/]+` keeps a variable from eating across path
        # segments — `meho://tenant/{id}/info` against
        # `meho://tenant/a/b/info` should NOT match (would otherwise bind
        # `id="a/b"`).
        return f"(?P<{var}>[^/]+)"

    # Escape literal portions piece-wise so the `{var}` placeholders stay
    # un-escaped, then run the substitution.
    parts: list[str] = []
    cursor = 0
    for placeholder in _TEMPLATE_VAR_RE.finditer(template):
        parts.append(re.escape(template[cursor : placeholder.start()]))
        parts.append(_to_capture(placeholder))
        cursor = placeholder.end()
    parts.append(re.escape(template[cursor:]))
    pattern = "^" + "".join(parts) + "$"

    match = re.match(pattern, uri)
    if match is None:
        return None
    return {name: match.group(name) for name in var_names}


# ---------------------------------------------------------------------------
# Eager-import sweep (called from FastAPI lifespan)
# ---------------------------------------------------------------------------


def eager_import_mcp_modules() -> None:
    """Import every module under ``meho_backplane.mcp.tools`` / ``...resources``.

    Each tool / resource module calls :func:`register_mcp_tool` or
    :func:`register_mcp_resource` at its top level; importing the module
    runs that side effect. The lifespan calls this once at startup so the
    first ``tools/list`` request finds the registry populated.

    Either subpackage being empty (no ``tools/<x>.py`` files yet — the
    state through G0.5-T3) is normal and silently skipped. An
    :class:`ImportError` on the subpackage itself bubbles — that's a
    real configuration bug (the subpackage's ``__init__.py`` is broken).
    """
    for subpkg_name in ("tools", "resources"):
        subpkg = importlib.import_module(f"meho_backplane.mcp.{subpkg_name}")
        if not hasattr(subpkg, "__path__"):
            continue
        for _finder, name, _ispkg in pkgutil.iter_modules(subpkg.__path__):
            importlib.import_module(f"meho_backplane.mcp.{subpkg_name}.{name}")


# ---------------------------------------------------------------------------
# Test-only helper
# ---------------------------------------------------------------------------


def clear_registries() -> None:
    """Reset both registries. **Test-only — never call from production.**

    The registries are populated at module-import time and meant to live
    for the process lifetime; clearing them mid-flight would leave any
    in-flight ``tools/call`` with a dangling handler reference. Tests
    that exercise :func:`register_mcp_tool` / :func:`register_mcp_resource`
    use this to restore a clean slate between cases.
    """
    _TOOLS.clear()
    _RESOURCES.clear()
