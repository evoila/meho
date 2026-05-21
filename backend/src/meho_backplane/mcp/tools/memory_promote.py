# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho.memory.promote`` -- admin-only memory promotion meta-tool (G5.2-T4).

The MCP twin of ``POST /api/v1/memory/{scope}/{slug}/promote`` (T4 #626).
Lives in the **admin namespace** (``meho.*``) and is registered with
``required_role=TenantRole.TENANT_ADMIN`` so the MCP registry's
list-time filter hides it from non-admin sessions -- it is NOT on the
agent's daily surface. Promotion is a privileged broaden-visibility
operation; consumer-needs.md §G5 names tenant-shared memory as "real
organisational state -- others depend on it" and the v0.2-decisions
document carves the agent surface narrowly. Operators initiate
promotion via the matching CLI verb (T5 #627); the admin meta-tool
exists for orchestrators / Claude Desktop sessions running under a
``tenant_admin`` operator who needs to drive promotions from the MCP
transport.

Why a separate module
=====================

The companion :mod:`meho_backplane.mcp.tools.memory` module owns the
two daily-surface tools (``search_memory`` / ``add_to_memory``). The
admin namespace is structurally distinct -- a wider role gate, a
``meho.*`` name, and the explicit "not on the agent surface" property
that CLAUDE.md postulate 5 carves. Keeping the admin tool in its own
module keeps the per-file surface-area documentation accurate (the
daily-surface module docstring claims "Two of the ~17 agent-facing
meta-tools"; mixing the admin tool in would invalidate that prose) and
lets the registry-reload fixture distinguish "the agent surface
changed" from "the admin surface changed" without an extra
``importlib.reload`` per file.

Error mapping
=============

Errors raised by :meth:`MemoryService.promote` map to JSON-RPC error
codes via :class:`~meho_backplane.mcp.server.McpInvalidParamsError`
(``-32602``) -- JSON-RPC has no distinct HTTP-403 / 400 / 409 codes,
so the dispatcher re-uses ``INVALID_PARAMS`` for every caller-input
fault. The error message preserves the original semantic so the
operator can tell which failure mode tripped:

* :class:`InvalidPromotionStepError` -- the
  ``(source, target)`` pair isn't in the ladder.
  Message: ``meho.memory.promote: <helper-message>``.
* :class:`PermissionDeniedError` -- legal step, wrong role.
  Message: ``meho.memory.promote: insufficient_promotion_authority``.
* :class:`NotImplementedError` -- per-target ACL gap (G0.3 #224
  unshipped). Surfaces as :class:`McpInvalidParamsError` so the
  operator sees a clear error rather than an opaque INTERNAL_ERROR.
* Source not visible (service returned ``None``) -- raised as
  :class:`McpInvalidParamsError` ``memory_not_found``. Mirrors the
  HTTP 404 collapse so the tenant-boundary info-leak avoidance holds
  on the MCP surface too: tenant A's admin cannot probe for tenant
  B's slugs by trying to promote them.

Audit contract
==============

The handler binds ``audit_promotion_target_scope`` contextvar before
calling the service so the chassis MCP audit row's ``payload`` JSONB
carries the load-bearing distinguisher (G8 audit-query consumers grep
on this key to separate promote rows from ``memory.remember``).
:func:`~meho_backplane.mcp.audit.write_mcp_audit_row` walks every
``audit_*`` contextvar via :func:`~meho_backplane.audit._resolve_audit_payload`
so the binding surfaces transparently without further plumbing.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.memory.rbac import (
    InvalidPromotionStepError,
    PermissionDeniedError,
)
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService

__all__: list[str] = []


#: Canonical tool name. ``meho.*`` namespace marks it as a backplane
#: meta-tool (CLAUDE.md postulate 5) rather than a vendor verb; the
#: ``.memory.promote`` suffix mirrors the audit row's
#: ``audit_op_id="memory.promote"`` for forensic correlation.
_TOOL_NAME: Final[str] = "meho.memory.promote"

#: Op-class string -- promotion is a write (inserts a target row and
#: optionally deletes the source). Consumed by the MCP dispatcher's
#: broadcast classifier the same way :mod:`meho_backplane.mcp.tools.memory`
#: tags ``add_to_memory`` write.
_OP_CLASS_WRITE: Final[str] = "write"

#: JSON-Schema enum of the five memory-scope values, mirrored from
#: :class:`MemoryScope`. Used in both ``source_scope`` and ``to`` so
#: a scope-enum extension lands in both places automatically.
_SCOPE_ENUM: Final[list[str]] = [scope.value for scope in MemoryScope]


async def _meho_memory_promote_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Drive :meth:`MemoryService.promote` from the MCP transport.

    Mirrors the HTTP route's behaviour: same service call, same error
    mapping (translated to JSON-RPC ``INVALID_PARAMS`` for caller-input
    faults), same audit contextvar (``audit_promotion_target_scope``)
    so the MCP audit row carries the same payload key as the HTTP one.
    Returns the target row's :class:`MemoryEntry` dict so the admin can
    verify the promotion landed without a follow-up
    ``resources/read meho://memory/{scope}/{slug}`` round-trip.

    The ``required_role=TENANT_ADMIN`` gate at the registry level means
    a non-admin session never reaches this handler (the role filter in
    :func:`~meho_backplane.mcp.registry.all_tools_for` hides the tool;
    the dispatcher's call-time re-check rejects an explicit
    ``tools/call`` invocation with a ``-32601`` ``method_not_found``).
    """
    source_scope = MemoryScope(arguments["source_scope"])
    source_slug: str = arguments["slug"]
    target_scope = MemoryScope(arguments["to"])
    move: bool = bool(arguments.get("move", False))
    target_name = arguments.get("target_name")

    # Bind the audit contextvar BEFORE the service call so the row
    # carries the target-scope distinguisher even when the service
    # raises mid-promotion (partial audit is the load-bearing forensic
    # signal). Mirrors the HTTP route's binding shape.
    structlog.contextvars.bind_contextvars(
        audit_promotion_target_scope=target_scope.value,
    )

    service = MemoryService()
    try:
        entry = await service.promote(
            operator=operator,
            source_scope=source_scope,
            source_slug=source_slug,
            target_scope=target_scope,
            move=move,
            target_name=target_name,
        )
    except InvalidPromotionStepError as exc:
        raise McpInvalidParamsError(f"{_TOOL_NAME}: {exc}") from exc
    except PermissionDeniedError as exc:
        del exc
        raise McpInvalidParamsError(f"{_TOOL_NAME}: insufficient_promotion_authority") from None
    except NotImplementedError as exc:
        raise McpInvalidParamsError(f"{_TOOL_NAME}: not_implemented: {exc}") from exc
    except ValueError as exc:
        # ``promote_target_conflict`` and ``target_name required``
        # both surface here from the service; INVALID_PARAMS covers
        # both -- the message preserves the distinguishing prefix.
        raise McpInvalidParamsError(f"{_TOOL_NAME}: {exc}") from exc

    if entry is None:
        # Source not visible -- same tenant-boundary collapse the HTTP
        # 404 enforces. Surfaces as INVALID_PARAMS rather than
        # INTERNAL_ERROR because the operator's input ("promote this
        # slug") names a row they cannot see.
        raise McpInvalidParamsError(f"{_TOOL_NAME}: memory_not_found")

    return entry.model_dump(mode="json")


register_mcp_tool(
    definition=ToolDefinition(
        name=_TOOL_NAME,
        description=(
            "Promote one memory to a strictly broader scope along the "
            "ladder: user -> user-tenant -> tenant, OR user -> "
            "user-target -> target. The new target row carries "
            "metadata.promoted_from = '<source-scope>/<source-slug>' "
            "and a cleared expires_at (broader-scope memories are "
            "intentionally long-lived). "
            "Idempotent: re-running the same promotion returns the "
            "existing target row (no duplicate insert, no 409). "
            "Passing move=true deletes the source row in the same "
            "transaction (one-way; demotion is not supported in v0.2). "
            "Admin-only -- tenant_admin role required. The agent's "
            "daily memory surface is search_memory + add_to_memory; "
            "this tool is for orchestrators and admin sessions "
            "driving deliberate visibility widening."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_scope": {
                    "type": "string",
                    "enum": _SCOPE_ENUM,
                    "description": (
                        "The current scope of the memory being "
                        "promoted. One of 'user', 'user-tenant', "
                        "'user-target', 'tenant', 'target'."
                    ),
                },
                "slug": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The source memory's slug -- the operator-"
                        "facing identifier within source_scope. The "
                        "target row carries the same slug; idempotency "
                        "is keyed on (target_scope, slug) plus the "
                        "metadata.promoted_from marker."
                    ),
                },
                "to": {
                    "type": "string",
                    "enum": _SCOPE_ENUM,
                    "description": (
                        "The target scope (strictly broader than "
                        "source_scope on the same ladder). "
                        "Non-ladder pairs raise INVALID_PARAMS."
                    ),
                },
                "move": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, delete the source row in the "
                        "same transaction as the target insert. "
                        "Default false (copy-and-leave)."
                    ),
                },
                "target_name": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "description": (
                        "Required when 'to' is 'user-target' AND the "
                        "source scope is 'user' (the ladder step that "
                        "needs a fresh target binding). For "
                        "'user-target -> target' the service inherits "
                        "the source's target_name; omit to use that "
                        "default. Ignored for tenant-flavoured "
                        "promotions."
                    ),
                },
            },
            "required": ["source_scope", "slug", "to"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_meho_memory_promote_handler,
)
