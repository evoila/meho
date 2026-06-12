# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret-broker typed op ``secret.move`` + its registrar.

This is the first **synthetic** typed-op product in the codebase: no
vendor connector backs it. The op is registered under the natural key
``(product="secret", version="1.x", impl_id="secret-broker")``, so the
wire ``connector_id`` is ``secret-broker-1.x`` — which round-trips
through :func:`~meho_backplane.operations._lookup.parse_connector_id`
back to ``("secret", "1.x", "secret-broker")`` (the version segment is
digit-led and the product is the head's first hyphen segment, both
required by the parser). A colon form or a non-digit-led version would
make the descriptor unreachable.

``secret.move`` copies a single credential field from a source store to
a sink store **server-side**. The agent submits only declarative
``<kind>:<ref>`` references; the value is read inside the backplane,
held in an in-memory :class:`~.endpoints.SecretMaterial`, written to the
sink, and never returned. The response carries only the move status, the
value's SHA-256, and its byte length. ``requires_approval=True`` +
``safety_level="dangerous"`` route the move through the existing
approval gate (the gate parks an unapproved move at ``awaiting_approval``
and the handler never runs); the scoped/time-boxed grant wiring is the
policy task (#1579), not this mechanism.

The handler is a **module-level** function (not a connector-bound
method), so the dispatcher resolves it with ``connector_instance=None``
and ``target=None`` — the synthetic product has no connector instance to
bind to. Adapters are resolved per move from
:data:`~.endpoints.SECRET_ENDPOINT_REGISTRY`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.connectors.secret.endpoints import (
    SECRET_ENDPOINT_REGISTRY,
    UnknownSecretKindError,
    parse_secret_ref,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.secret.endpoints import (
        SecretEndpoint,
        SecretRef,
    )
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "SECRET_MOVE_PARAMETER_SCHEMA",
    "register_secret_broker_operations",
    "secret_move",
]

_log = structlog.get_logger(__name__)

#: A ``<kind>:<ref>`` move endpoint reference. A non-empty string; the
#: kind/ref split (and the per-kind ref grammar) is validated in the
#: handler — JSON Schema cannot express "a registered kind" — so the
#: schema only guards against an empty/whitespace value here. The
#: ``pattern`` requires at least one non-whitespace char on each side of
#: a colon so an obviously malformed intent (``":x"``, ``"x:"``, no
#: colon) fails at param validation rather than inside the handler.
_SECRET_REF_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 3,
    "pattern": r"^\S+:\S+$",
    "description": (
        "A '<kind>:<ref>' secret reference. 'kind' selects the store "
        "adapter (e.g. 'vault'); 'ref' is the store-specific address "
        "(for vault: a KV-v2 path with a '#<field>' fragment, e.g. "
        "'vault:secret/db/prod#password'). References only — never the "
        "secret value."
    ),
}

#: ``secret.move`` parameter schema (JSON Schema 2020-12). Carries the
#: source/sink references and an operator-supplied reason for the audit
#: trail — never the value. ``additionalProperties: false`` keeps a
#: caller from smuggling a value field past the schema into the handler.
SECRET_MOVE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "from": _SECRET_REF_PROPERTY,
        "to": _SECRET_REF_PROPERTY,
        "reason": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Why the move is being performed. Recorded for the "
                "operator/approver; must not contain a secret value."
            ),
        },
    },
    "required": ["from", "to"],
    "additionalProperties": False,
}

_SECRET_MOVE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "const": "moved"},
        "value_sha256": {
            "type": "string",
            "description": "Hex SHA-256 of the moved value — provenance, never the value.",
        },
        "length": {
            "type": "integer",
            "description": "Byte length of the moved value.",
        },
    },
    "required": ["status", "value_sha256", "length"],
    "additionalProperties": False,
}

_SECRET_MOVE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Move a single credential field from one store to another "
        "server-side WITHOUT ever reading the value into your context. "
        "Use when an operational flow needs a secret copied from A to B "
        "(e.g. provisioning a downstream system with a credential that "
        "lives in Vault) and observing the value would force a rotation. "
        "Submit references, never values: --from/--to are '<kind>:<ref>' "
        "strings. The move is change-class: it requires approval before "
        "it executes, and the response returns only the move status, the "
        "value's SHA-256, and its length — never the value."
    ),
    "parameter_hints": {
        "from": "Required. Source '<kind>:<ref>' (e.g. 'vault:secret/db/prod#password').",
        "to": "Required. Sink '<kind>:<ref>'.",
        "reason": "Optional. Human-readable justification for the approver/audit trail.",
    },
    "output_shape": (
        "On success: {'status': 'moved', 'value_sha256': <hex>, "
        "'length': <int>}. The value never appears."
    ),
}

_SECRET_MOVE_WHEN_TO_USE = (
    "Use to move a credential from one store to another server-side, "
    "with the backplane as the credential-bearing intermediary so the "
    "agent never observes the value. The right op when the question is "
    "'copy this secret from A to B' rather than 'read this secret' "
    "(which would land the value in the transcript)."
)


def _resolve_endpoint(spec: SecretRef) -> SecretEndpoint:
    """Resolve a parsed ``<kind>:<ref>`` to a constructed endpoint.

    Raises :class:`UnknownSecretKindError` when no adapter is registered
    for the kind — the dispatcher maps it to a ``connector_error`` with
    the class name in ``extras['exception_class']``. The error names the
    unknown kind and the registered kinds, never the ref's value side.
    """
    factory = SECRET_ENDPOINT_REGISTRY.get(spec.kind)
    if factory is None:
        known = ", ".join(sorted(SECRET_ENDPOINT_REGISTRY)) or "(none)"
        raise UnknownSecretKindError(
            f"no secret endpoint adapter for kind {spec.kind!r}; registered kinds: {known}"
        )
    return factory(spec.ref)


async def secret_move(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Move a credential field from a source store to a sink store.

    Op-id: ``secret.move``. Synthetic typed op (no vendor connector).
    The handler:

    1. parses ``params['from']`` / ``params['to']`` into
       ``<kind>:<ref>`` references (the dispatcher has already validated
       the param schema, so both are present and colon-shaped);
    2. resolves each kind to a :class:`~.endpoints.SecretEndpoint` via
       the registry;
    3. ``read_secret`` from the source (the value enters memory only as
       a :class:`~.endpoints.SecretMaterial`);
    4. ``write_secret`` to the sink — entirely server-side.

    Returns ONLY ``{'status': 'moved', 'value_sha256': <hex>, 'length':
    <int>}``. The value is never in the params, the return value, a log
    event, or (therefore) the audit row — only its hash + length. The
    ``reason`` param (when supplied) is operator-facing justification and
    is intentionally not read here; it is recorded by the dispatcher's
    audit row via the validated params hash and is surfaced to the
    approver by the approval-queue task (#1579).
    """
    source_ref = parse_secret_ref(str(params["from"]))
    sink_ref = parse_secret_ref(str(params["to"]))
    source = _resolve_endpoint(source_ref)
    sink = _resolve_endpoint(sink_ref)

    # Server-side read → write. The value lives only inside ``material``
    # for the duration of this call; it never crosses back to the caller.
    material = await source.read_secret(operator)
    await sink.write_secret(operator, material)

    _log.info(
        "secret_broker.move",
        from_kind=source_ref.kind,
        to_kind=sink_ref.kind,
        value_sha256=material.value_sha256,
        length=material.length,
    )
    return {
        "status": "moved",
        "value_sha256": material.value_sha256,
        "length": material.length,
    }


async def register_secret_broker_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``secret.move`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (via ``register_typed_op_registrar``) and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent: a re-run against
    unchanged text is a no-op for the embedding pipeline.

    ``requires_approval=True`` + ``safety_level="dangerous"`` make the
    move change-class: the existing policy gate parks an unapproved
    dispatch at ``awaiting_approval`` (G11.7-T1 #1401) and the handler
    never runs. The ``embedding_service`` kwarg is the test seam, the
    same shape every connector registrar carries.
    """
    await register_typed_operation(
        product="secret",
        version="1.x",
        impl_id="secret-broker",
        op_id="secret.move",
        handler=secret_move,
        group_key="broker",
        when_to_use=_SECRET_MOVE_WHEN_TO_USE,
        summary="Move a credential from one store to another, value never returned.",
        description=(
            "Copies a single credential field from a source store to a "
            "sink store server-side via the SecretEndpoint adapter "
            "registry (vault-kv is the first pair). The agent submits "
            "only '<kind>:<ref>' references; the value is read, hashed, "
            "and written entirely inside the backplane and never enters "
            "the op params, the response, a log event, or the audit row "
            "— the response carries only status + value SHA-256 + length. "
            "Change-class: requires_approval=True + safety_level=dangerous "
            "route the move through the approval gate before it executes."
        ),
        parameter_schema=SECRET_MOVE_PARAMETER_SCHEMA,
        response_schema=_SECRET_MOVE_RESPONSE_SCHEMA,
        tags=["secret-broker", "write", "change-class"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=_SECRET_MOVE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
