# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targetless typed ``targets.register`` op + its registrar (#2861).

The third **synthetic** typed-op product (after ``secret`` and
``topology``): no vendor connector backs it. The op is registered under
the natural key ``(product="targets", version="1.x",
impl_id="targets-registry")``, so the wire ``connector_id`` is
``targets-registry-1.x`` — which round-trips through
:func:`~meho_backplane.operations._lookup.parse_connector_id` back to
``("targets", "1.x", "targets-registry")`` (digit-led version segment,
product = the head's first hyphen segment; the ``secret-broker-1.x`` /
``topology-graph-1.x`` mold, #1577).

Why it exists (Goal #221)
=========================

The targets registry was the only CRUD-shaped registry in MEHO with no
agent-surface write path: ``list_targets`` / ``query_topology`` are
read-only, and REST ``POST /api/v1/targets`` (and the UI) both call
:func:`~meho_backplane.api.v1.targets.create_target` — a human-only
surface. An agent driving the onboarding path had no in-tool equivalent
and fell back to a raw ``POST`` with a hand-minted JWT. Routing the MCP
write front (``meho_targets_register``) through
:func:`~meho_backplane.operations.dispatcher.dispatch` puts it behind
:func:`~meho_backplane.operations._validate.policy_gate` — the single
seam where an AGENT principal's ``caution``-level write parks as a
durable :class:`~meho_backplane.db.models.ApprovalRequest`
(propose-then-approve) while a human ``tenant_admin`` keeps the
default-allow immediate path. REST and UI are human-only surfaces and
keep calling the service primitive directly.

Approval posture — the load-bearing dial (mirrors ``topology.*`` #2537):

* ``safety_level="caution"`` + ``requires_approval=False`` parks **agent
  principals only** (the AGENT ``caution`` verdict floor →
  ``needs-approval`` in :mod:`meho_backplane.auth.permissions`), while a
  human ``tenant_admin`` rides the default-allow branch and executes
  immediately.
* NOT ``requires_approval=True`` — that would park humans too.
* NOT ``safety_level="dangerous"`` — that would DENY agents by default
  rather than park them. Target registration is reversible
  (``DELETE /api/v1/targets`` + the soft-delete tombstone), so caution
  fits; dangerous does not.

The handler is a **module-level** function (no connector instance to
bind), so the dispatcher resolves it with ``connector_instance=None`` /
``target=None``. It builds a
:class:`~meho_backplane.targets.schemas.TargetCreate` from the validated
params and calls :func:`~meho_backplane.api.v1.targets.create_target`
unchanged — product-token validation, ``secret_ref`` tenant-scope guard,
SSRF guard, ``preferred_impl_id`` validation, and JWT-derived
``tenant_id`` all come for free; this shim re-implements none of them.
``tenant_id`` is taken from *operator* inside the service, never from
*params* (``additionalProperties: false`` already rejects a smuggled
``tenant_id`` at the schema layer). ``create_target`` only ``flush``-es
and relies on the caller's transaction, so the handler wraps it in
``session.begin()`` (unlike the topology services, which own their own
transaction).

Validation failures are translated to :class:`ValueError` at this
boundary — :class:`fastapi.HTTPException` (unknown product, out-of-scope
``secret_ref``, unknown ``preferred_impl_id``, duplicate name) from the
service and :class:`pydantic.ValidationError` (SSRF-blocked host, malformed
CA-pin, ``verify_tls``/``tls_ca_pin`` contradiction, forbidden extra
field) from the model — so the dispatcher's ``connector_error`` envelope
carries a clean message the MCP front maps to ``-32602``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import ValidationError

from meho_backplane.api.v1.targets import create_target
from meho_backplane.connectors.targets.schemas import (
    TARGETS_REGISTER_PARAMETER_SCHEMA,
    TARGETS_REGISTER_RESPONSE_SCHEMA,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.targets.schemas import TargetCreate

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "TARGETS_REGISTER_OP_ID",
    "TARGETS_REGISTRY_CONNECTOR_ID",
    "register_targets_registry_operations",
    "targets_register",
]

#: Wire connector_id for the synthetic targets-registry product. Must
#: stay parser-compatible: digit-led version suffix, product recoverable
#: as the head's first hyphen segment (see module docstring).
TARGETS_REGISTRY_CONNECTOR_ID = "targets-registry-1.x"

#: Op id — the ``audit_log.path`` string this write correlates on.
TARGETS_REGISTER_OP_ID = "targets.register"

_GROUP_KEY = "registry"
_GROUP_WHEN_TO_USE = (
    "Write to the tenant's target registry: register a new target "
    "(name + product + host, plus optional connection detail) so it can "
    "be dispatched against. Use when onboarding a system the agent must "
    "later drive — an rke2 cluster, a vCenter, a DNS server. "
    "Agent-principal calls park as approval requests for a human "
    "operator to approve; human tenant_admin calls execute immediately. "
    "Target reads live on list_targets / query_topology, not here."
)

_DESCRIPTION = (
    "Register a new target in the operator's tenant, reusing the same "
    "service path as REST `POST /api/v1/targets`: product-token "
    "validation, the `secret_ref` tenant-scope guard, the SSRF guard on "
    "`host`/`fqdn`, and the JWT-derived `tenant_id` all apply. "
    "Tenant-scoped automatically — no `tenant_id` argument (cross-tenant "
    "registration is structurally impossible). `fingerprint` is "
    "server-managed and rejected. Agent-principal calls park as approval "
    "requests; human tenant_admin calls execute immediately."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Onboard a new target from the agent surface instead of falling "
        "back to a raw REST POST. Supply name + product + host (plus any "
        "optional connection detail); the tenant is taken from your "
        "identity."
    ),
    "parameter_hints": {
        "name": "Required. Unique within the tenant; the dispatch handle.",
        "product": "Required. A registered connector product token (e.g. 'rke2').",
        "host": "Required. Dialable host/IP; SSRF-screened.",
        "secret_ref": "Optional. Must sit in the tenant's Vault subtree; omit to derive it.",
    },
    "output_shape": "{'target_id', 'name', 'product', 'host', 'tenant_id'}",
}


async def targets_register(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Register a new target in the operator's tenant.

    Op-id: ``targets.register``. Targetless typed op. The dispatcher has
    already validated *params* against
    :data:`~meho_backplane.connectors.targets.schemas.TARGETS_REGISTER_PARAMETER_SCHEMA`.
    Builds a :class:`~meho_backplane.targets.schemas.TargetCreate` and
    delegates to :func:`~meho_backplane.api.v1.targets.create_target`,
    which owns every validation and the audit trail; this shim
    re-implements none of it. ``tenant_id`` comes from *operator* inside
    the service — never from *params*.

    ``create_target`` relies on the caller's transaction (it ``flush``-es
    but does not commit), so the call is wrapped in ``session.begin()``.
    Model / service validation failures are re-raised as
    :class:`ValueError` so the dispatcher's ``connector_error`` envelope
    maps them to ``-32602`` with a clean message.
    """
    try:
        body = TargetCreate(**params)
    except ValidationError as exc:
        raise ValueError(f"invalid target parameters: {exc}") from exc

    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session, session.begin():
            created = await create_target(body=body, operator=operator, session=session)
    except HTTPException as exc:
        raise ValueError(_http_exception_message(exc)) from exc

    return {
        "target_id": str(created.id),
        "name": created.name,
        "product": created.product,
        "host": created.host,
        "tenant_id": str(created.tenant_id),
    }


def _http_exception_message(exc: HTTPException) -> str:
    """Flatten a service ``HTTPException`` to a single operator-facing line.

    ``create_target`` raises structured 422s (``detail`` is a dict for the
    unknown-product and out-of-scope-``secret_ref`` cases) and plain-string
    409/422s. Return the string detail verbatim; render a structured detail
    prefixed with its status so the cause is still legible in the ``-32602``
    message.
    """
    detail = exc.detail
    if isinstance(detail, str):
        return detail
    return f"{exc.status_code}: {detail}"


async def register_targets_registry_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``targets.register`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (via ``register_typed_op_registrar``) and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent: a re-run against
    unchanged text is a no-op for the embedding pipeline. The
    ``embedding_service`` kwarg is the test seam every connector registrar
    carries.

    ``safety_level="caution"`` + ``requires_approval=False`` is the
    agents-park / humans-immediate combination — see the module docstring
    for why neither ``requires_approval=True`` nor
    ``safety_level="dangerous"`` fits.
    """
    await register_typed_operation(
        product="targets",
        version="1.x",
        impl_id="targets-registry",
        op_id=TARGETS_REGISTER_OP_ID,
        handler=targets_register,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="Register a new target in the operator's tenant.",
        description=_DESCRIPTION,
        parameter_schema=TARGETS_REGISTER_PARAMETER_SCHEMA,
        response_schema=TARGETS_REGISTER_RESPONSE_SCHEMA,
        tags=["targets", "write", "registry"],
        safety_level="caution",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
