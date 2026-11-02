# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targetless typed ``targets.probe`` op: detect a target's product and version.

``targets.probe`` lets an agent run detection on a target with one
operation. It runs :func:`~meho_backplane.api.v1.targets.probe_and_store_fingerprint`,
the same code as REST ``POST /api/v1/targets/{name}/probe``: find the
target in the caller's tenant (name, alias or id), pick its connector, ask
the system for its product and version, and store the answer in
``targets.fingerprint``. Connectors pick their implementation from that
stored answer, so a target an agent registered with ``targets.register``
is ready to use after one ``targets.probe``.

Risk level ``safe``: the op reads the system and writes only the detected
fields of MEHO's own target row. Role: like the REST route, it needs the
``operator`` or ``tenant_admin`` role; a ``read_only`` login is refused
before the target is looked up. The result holds the detected fields only,
not the connector's ``extras``.

Policy for the named target: the op is targetless, so the dispatcher's
policy gate runs with no target and a grant that names one target does not
match there. The handler finds the target and then checks MEHO's policy for
``targets.probe`` on that target
(:func:`~meho_backplane.operations.secondary_target_policy.secondary_target_refusal`),
before the system is contacted and before anything is stored. A deny for
that target refuses the call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException

from meho_backplane.api.v1.targets import probe_and_store_fingerprint, resolve_route_target
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.targets.ops import (
    _GROUP_KEY,
    _GROUP_WHEN_TO_USE,
    TARGETS_REGISTRY_CONNECTOR_ID,
    _http_exception_message,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.secondary_target_policy import (
    SecondaryTargetWrite,
    secondary_target_refusal,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "TARGETS_PROBE_OP_ID",
    "TARGETS_PROBE_PARAMETER_SCHEMA",
    "TARGETS_PROBE_RESPONSE_SCHEMA",
    "register_targets_probe_operation",
    "targets_probe",
]

TARGETS_PROBE_OP_ID: Final[str] = "targets.probe"

#: The policy check for the target named in the params. The dispatcher
#: checks ``targets.probe`` with no target; this checks it on the found
#: target, so a grant that names the target counts.
_PROBE_ON_TARGET: Final[SecondaryTargetWrite] = SecondaryTargetWrite(
    connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
    op_ids=(TARGETS_PROBE_OP_ID,),
    target_label="target",
)

_NULLABLE_STRING: Final[list[str]] = ["string", "null"]

TARGETS_PROBE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name, alias or id of the target, as `list_targets` shows it. "
                "Only targets of your own tenant are found."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
    "examples": [{"name": "<target-name>"}],
}

TARGETS_PROBE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_id": {"type": "string", "description": "UUID of the probed target."},
        "name": {"type": "string", "description": "Name of the probed target."},
        "vendor": {"type": "string", "description": "Detected vendor."},
        "product": {"type": "string", "description": "Detected product."},
        "version": {"type": _NULLABLE_STRING, "description": "Detected version, if known."},
        "build": {"type": _NULLABLE_STRING, "description": "Detected build, if known."},
        "edition": {"type": _NULLABLE_STRING, "description": "Detected edition, if known."},
        "reachable": {
            "type": "boolean",
            "description": "False when the connector could not reach the system.",
        },
        "probed_at": {
            "type": "string",
            "format": "date-time",
            "description": "When the detection ran (UTC).",
        },
        "probe_method": {
            "type": "string",
            "description": "How the connector detected it, for example an API endpoint.",
        },
    },
    "required": [
        "target_id",
        "name",
        "vendor",
        "product",
        "version",
        "build",
        "edition",
        "reachable",
        "probed_at",
        "probe_method",
    ],
}

_DESCRIPTION = (
    "Detect the product and version of one target of your tenant and store "
    "them on the target. Runs the same code as REST "
    "`POST /api/v1/targets/{name}/probe`. Needs the `operator` or "
    "`tenant_admin` role."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "After you register a target with targets.register, or when a "
        "target's system was upgraded. MEHO picks the connector "
        "implementation from the detected version."
    ),
    "preconditions": (
        "Needs the `operator` or `tenant_admin` role. The target's product "
        "must have a connector. A connector that needs a login reads it "
        "from the target's secret."
    ),
    "parameter_hints": {
        "name": "Required. Name, alias or id of the target, from list_targets.",
    },
    "output_shape": (
        "{'target_id', 'name', 'vendor', 'product', 'version', 'build', "
        "'edition', 'reachable', 'probed_at', 'probe_method'}"
    ),
    "next_step": (
        "If `reachable` is false, check the target's host and network, then "
        "call again. Otherwise call operations against the target: MEHO now "
        "uses the detected version. An error means nothing was stored."
    ),
}


async def targets_probe(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Detect one target's product and version and store it on the row.

    Op-id: ``targets.probe``. Targetless typed op; the target is named in
    *params*. Refuses a ``read_only`` login, as the REST route does. The
    target is looked up in the operator's tenant only, so a target of
    another tenant is not found. MEHO's policy for ``targets.probe`` on
    the found target is checked next; a refusal is a :class:`ValueError`
    and the system is not contacted. Service exceptions (unknown target,
    no connector, a connector that fails) are re-raised as
    :class:`ValueError`, like the other ``targets.*`` ops; nothing is
    stored then.
    """
    if operator.tenant_role is TenantRole.READ_ONLY:
        raise ValueError(
            "targets.probe needs the operator or tenant_admin role; "
            f"your role is {operator.tenant_role}"
        )
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session, session.begin():
            row = await resolve_route_target(session, operator.tenant_id, str(params["name"]))
            refusal = await secondary_target_refusal(operator, _PROBE_ON_TARGET, row)
            if refusal is not None:
                raise ValueError(f"{refusal}. Nothing was probed or stored.")
            fp = await probe_and_store_fingerprint(session, row, operator=operator)
            target_id, name = str(row.id), row.name
    except HTTPException as exc:
        raise ValueError(_http_exception_message(exc)) from exc
    return {
        "target_id": target_id,
        "name": name,
        **fp.model_dump(
            mode="json",
            include={
                "vendor",
                "product",
                "version",
                "build",
                "edition",
                "reachable",
                "probed_at",
                "probe_method",
            },
        ),
    }


async def register_targets_probe_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``targets.probe`` typed op into ``endpoint_descriptor``.

    Queued by the package ``__init__``. Uses the same ``registry`` group and
    group text as ``targets.register`` and ``targets.remove``. Safe to run
    twice.
    """
    await register_typed_operation(
        product="targets",
        version="1.x",
        impl_id="targets-registry",
        op_id=TARGETS_PROBE_OP_ID,
        handler=targets_probe,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="Detect a target's product and version and store them on the target.",
        description=_DESCRIPTION,
        parameter_schema=TARGETS_PROBE_PARAMETER_SCHEMA,
        response_schema=TARGETS_PROBE_RESPONSE_SCHEMA,
        tags=["targets", "registry", "fingerprint"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
