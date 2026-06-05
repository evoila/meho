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
    "capability_satisfied",
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


def capability_satisfied(
    operator: Operator,
    required_capability: str | None,
) -> bool:
    """Return True when *operator* is admitted past the capability gate.

    A tool / resource with ``required_capability=None`` has no capability
    gate and is always admitted (only its role gate applies). When a
    capability *is* required, the operator passes iff the key is in
    :attr:`~meho_backplane.auth.operator.Operator.capabilities`. The
    operator's set is fail-closed — empty when the JWT carries no
    capability claim — so an unprovisioned tenant never satisfies a
    capability gate.

    Centralised so the gate shape stays single-source for both
    :func:`all_tools_for` (list-time true-absence filter) and the
    call-time re-check in :mod:`~meho_backplane.mcp.handlers`. Public so
    the handlers module imports it at module level rather than dipping
    into a private sibling symbol, exactly like :func:`role_at_least`.
    """
    if required_capability is None:
        return True
    return required_capability in operator.capabilities


#: JSON-Schema combinator keywords the Anthropic Messages API rejects at
#: the *top level* of a tool's ``input_schema``. A request carrying one
#: 400s with ``input_schema does not support oneOf, allOf, or anyOf at
#: the top level`` — and because the API validates the whole ``tools``
#: array, a single offender poisons *every* call in that session, not
#: just its own. MEHO uses top-level ``allOf`` (``query_topology``'s
#: per-``kind`` conditional required-args) and ``oneOf``
#: (``meho.topology.unannotate``'s XOR selector) for *server-side*
#: jsonschema validation in :mod:`~meho_backplane.mcp.handlers`; those
#: stay on ``inputSchema`` and keep validating. Only the wire copy
#: published via :meth:`ToolDefinition.to_wire` drops them. Nested
#: occurrences (inside ``properties`` / ``items`` / ``$defs`` / …) are
#: legal and preserved — the restriction is top-level only.
_WIRE_FORBIDDEN_TOPLEVEL_KEYS: tuple[str, ...] = ("oneOf", "allOf", "anyOf")


def _wire_safe_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return *schema* with any top-level combinator keyword removed.

    See :data:`_WIRE_FORBIDDEN_TOPLEVEL_KEYS` for why. *schema* is not
    mutated: when it carries no forbidden top-level key the same object is
    returned (the common ~15-tool case, no needless copy); otherwise a
    shallow copy minus those keys is returned. Nested combinators are
    untouched — the Anthropic restriction is top-level only.
    """
    if not any(key in schema for key in _WIRE_FORBIDDEN_TOPLEVEL_KEYS):
        return schema
    return {key: value for key, value in schema.items() if key not in _WIRE_FORBIDDEN_TOPLEVEL_KEYS}


class ToolDefinition(BaseModel):
    """MCP tool definition + MEHO-internal RBAC metadata.

    The wire shape exposed via ``tools/list`` is derived through
    :meth:`to_wire`, which drops the MEHO-internal fields (``required_role``,
    ``op_class``, ``required_capability``) — clients don't need them and
    they'd leak server-side policy detail.

    ``inputSchema`` is a JSON Schema 2020-12 object the handler validates
    incoming ``tools/call.arguments`` against. ``outputSchema`` is
    optional; when present the handler's return value is also validated
    against it (T4 reference tool wires this).

    ``required_capability`` (G4.5-T1) is an optional second gating axis
    *orthogonal* to ``required_role``. When set, the tool is filtered out
    of ``tools/list`` and rejected at ``tools/call`` for any operator
    whose :attr:`~meho_backplane.auth.operator.Operator.capabilities`
    set lacks the key — true absence for unprovisioned tenants, mirroring
    the connector enable model rather than a packaging/entitlement
    system. ``None`` (the default) means "no capability gate", so every
    existing tool keeps its role-only behaviour.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    inputSchema: dict[str, Any]  # noqa: N815 — wire field is camelCase per MCP spec
    title: str | None = None
    outputSchema: dict[str, Any] | None = None  # noqa: N815
    required_role: TenantRole = TenantRole.OPERATOR
    op_class: str = "read"
    required_capability: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Serialise to the MCP wire shape, dropping MEHO-internal fields.

        Spec fields kept: ``name``, ``description``, ``inputSchema``,
        optional ``title`` / ``outputSchema``. MEHO fields dropped:
        ``required_role``, ``op_class``, ``required_capability``. The
        drop is deliberate — clients don't need server-side RBAC /
        capability details and shouldn't see them.

        ``inputSchema`` is additionally passed through
        :func:`_wire_safe_input_schema`, which strips top-level
        ``oneOf`` / ``allOf`` / ``anyOf`` — the Anthropic Messages API
        rejects those at the top level of a tool's ``input_schema`` and
        the rejection poisons the whole session. The full schema (with
        the combinators) is retained on ``self.inputSchema`` so
        server-side jsonschema validation in
        :mod:`~meho_backplane.mcp.handlers` is unaffected.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": _wire_safe_input_schema(self.inputSchema),
        }
        if self.title is not None:
            out["title"] = self.title
        if self.outputSchema is not None:
            out["outputSchema"] = self.outputSchema
        return out


class ResourceTemplateDefinition(BaseModel):
    """MCP resource-template definition + MEHO-internal RBAC metadata.

    The wire shape exposed via ``resources/templates/list`` is derived
    through :meth:`to_wire`; ``required_role`` and ``required_capability``
    are dropped for the same reason they are on :class:`ToolDefinition`.

    ``uriTemplate`` follows the RFC 6570 syntax for ``{var}`` substitution.
    v0.2 supports only simple variable substitution (no expression
    operators like ``+`` / ``#`` / ``?``); :func:`_match_uri_template`
    documents what it actually parses.

    ``required_capability`` (G4.5-T1) gates the template the same way it
    gates a tool: a capability-gated template is absent from
    ``resources/templates/list`` and 403s on ``resources/read`` for an
    operator lacking the key. ``None`` (the default) means no capability
    gate. T4's companion ``meho://docs/{...}`` resource is the first
    consumer.
    """

    model_config = ConfigDict(frozen=True)

    uriTemplate: str = Field(min_length=1)  # noqa: N815 — wire field per MCP spec
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    mimeType: str = "application/json"  # noqa: N815
    title: str | None = None
    required_role: TenantRole = TenantRole.OPERATOR
    required_capability: str | None = None

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
    """Return tools the operator may see, in registration order.

    Two orthogonal gates, both AND-ed: the operator's
    :class:`TenantRole` must meet ``required_role`` *and* — when the tool
    declares a ``required_capability`` — that key must be in the
    operator's provisioned :attr:`Operator.capabilities`. A
    capability-gated tool the tenant hasn't provisioned is *absent* from
    the listing (true absence, G4.5-T1), not just un-callable.
    """
    return [
        defn
        for defn, _ in _TOOLS.values()
        if role_at_least(operator.tenant_role, defn.required_role)
        and capability_satisfied(operator, defn.required_capability)
    ]


def all_resource_templates_for(
    operator: Operator,
) -> list[ResourceTemplateDefinition]:
    """Return resource templates the operator may see, in registration order.

    Same two-gate (role AND capability) filter as :func:`all_tools_for`.
    """
    return [
        defn
        for defn, _ in _RESOURCES.values()
        if role_at_least(operator.tenant_role, defn.required_role)
        and capability_satisfied(operator, defn.required_capability)
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
