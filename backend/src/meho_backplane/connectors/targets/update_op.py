# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targetless typed ``targets.update`` op: change one target of the tenant.

The change of ``targets.register`` and ``targets.remove``: it takes the id
of the target and the fields to change, and calls
:func:`~meho_backplane.api.v1.targets.update_target`, the function behind
``PATCH /api/v1/targets/{name}`` and the console. So every check of that
route runs here too: product and ``preferred_impl_id`` checks, the
``secret_ref`` tenant scope, the Vault login keys (platform admin), the
TLS rules, the audit of TLS and capture changes, and the drop of the
logins connectors keep for the target.

Who waits (the policy gate, ``operations/_validate.py``):

* ``safety_level="caution"`` with ``requires_approval``: every call of the
  operation waits for a person's approval (also ``PATCH
  /api/v1/targets`` with a person's token). The console changes a target
  with :func:`~meho_backplane.api.v1.targets.update_target` directly.
* ``host`` and ``fqdn`` carry ``x-meho-caller-address``: later calls send
  the target's stored secret to that address, so no standing grant covers
  this operation and a person approves each call.
* Role floor ``tenant_admin``, the same as the REST route. After approval
  the call runs under the approver, so the approver's role counts.

The target is looked up by id in the caller's tenant, never by name, so
the approved change goes to the target the approver saw.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException
from pydantic import ValidationError

from meho_backplane.api.v1.targets import update_target
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.targets.ops import (
    _GROUP_KEY,
    _GROUP_WHEN_TO_USE,
    _http_exception_message,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.role_floors import register_role_floor
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.targets.resolver import resolve_target_by_id
from meho_backplane.targets.schemas import SECRET_FIELD_MAP_MAX_ENTRIES, TargetUpdate

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "TARGETS_UPDATE_OP_ID",
    "TARGETS_UPDATE_PARAMETER_SCHEMA",
    "TARGETS_UPDATE_RESPONSE_SCHEMA",
    "register_targets_update_operation",
    "targets_update",
]

TARGETS_UPDATE_OP_ID: Final[str] = "targets.update"

_NULLABLE_STRING: Final[list[str]] = ["string", "null"]

TARGETS_UPDATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "UUID of the target to change, as `list_targets` returns it in "
                "`id`. Never the name. Only targets of your own tenant are found."
            ),
        },
        "aliases": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "New list of alternate names. Replaces the stored list.",
        },
        "product": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "New product token, checked against the registered products.",
        },
        "version": {
            "type": _NULLABLE_STRING,
            "maxLength": 100,
            "description": "New product version; `null` clears it.",
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": "New host or IP, checked like the host of a new target.",
            "x-meho-caller-address": True,
        },
        "port": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 65535,
            "description": "New TCP port; `null` uses the connector default.",
        },
        "fqdn": {
            "type": _NULLABLE_STRING,
            "description": "New fully-qualified domain name; `null` clears it.",
            "x-meho-caller-address": True,
        },
        "secret_ref": {
            "type": _NULLABLE_STRING,
            "description": (
                "New Vault path of the credential, inside the tenant's secret "
                "subtree; `null` means the target uses no credential. Sending "
                "the same value makes MEHO read the secret again."
            ),
        },
        "auth_model": {
            "type": _NULLABLE_STRING,
            "enum": [*(m.value for m in AuthModel), None],
            "description": "New per-target identity model.",
        },
        "vpn_required": {
            "type": ["boolean", "null"],
            "description": "Whether a VPN must be up to reach the target.",
        },
        "verify_tls": {
            "type": ["boolean", "null"],
            "description": (
                "Whether MEHO checks the TLS certificate of the target. `false` "
                "is audited and cannot be combined with a `tls_ca_pin`."
            ),
        },
        "tls_ca_pin": {
            "type": _NULLABLE_STRING,
            "description": "New PEM CA bundle to trust for this target; `null` clears it.",
        },
        "tls_server_name": {
            "type": _NULLABLE_STRING,
            "maxLength": 512,
            "description": "New TLS server name; `null` derives it from `host`.",
        },
        "secret_field_map": {
            "type": ["object", "null"],
            "maxProperties": SECRET_FIELD_MAP_MAX_ENTRIES,
            "additionalProperties": {"type": _NULLABLE_STRING, "maxLength": 200},
            "description": (
                "Entries merged into the stored `{connector_field: secret_field}` "
                "map: a string adds or replaces an entry, `null` removes it. "
                "`null` for the whole map clears it."
            ),
        },
        "extras": {
            "type": ["object", "null"],
            "description": (
                "New connector-specific metadata. The Vault login keys "
                "(`vault_role`, `vault_mount`) need a platform admin."
            ),
        },
        "notes": {
            "type": _NULLABLE_STRING,
            "description": "New free-text notes; `null` clears them.",
        },
        "preferred_impl_id": {
            "type": _NULLABLE_STRING,
            "maxLength": 200,
            "description": "New connector implementation override; `null` clears it.",
        },
        "flight_recorder_capture": {
            "type": ["boolean", "null"],
            "description": "`true` on, `false` off, `null` follows the instance setting.",
        },
    },
    "required": ["target_id"],
    "additionalProperties": False,
    "examples": [{"target_id": "<target-uuid>", "host": "<new-host>"}],
}

TARGETS_UPDATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["updated"],
            "description": "`updated` when the change is saved.",
        },
        "target_id": {"type": "string", "description": "The `target_id` from the call."},
        "name": {"type": "string", "description": "Name of the changed target."},
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The fields the call sent, sorted.",
        },
    },
    "required": ["status", "target_id", "name", "fields"],
}

_DESCRIPTION = (
    "Change one target of your tenant by its UUID, with the same checks as "
    "REST `PATCH /api/v1/targets/{name}`. Only the sent fields change. Needs "
    "`tenant_admin`. A person approves each call."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "A registered target needs other connection details: a new host or "
        "port, another Vault path, a TLS setting, aliases or notes."
    ),
    "preconditions": (
        "Needs the `tenant_admin` role. Get the target's `id` from "
        "list_targets; do not pass a name. Send only the fields to change; "
        "`name` cannot change (remove the target and register it again)."
    ),
    "parameter_hints": {
        "target_id": "Required. The target's UUID from list_targets.",
        "secret_ref": "Must sit in the tenant's Vault subtree.",
        "verify_tls": "`false` is audited; prefer a `tls_ca_pin`.",
    },
    "output_shape": "{'status': 'updated', 'target_id', 'name', 'fields'}",
    "next_step": (
        "The call waits until a person approves it. It then runs under the approver's login."
    ),
}

register_role_floor(TARGETS_UPDATE_OP_ID, TenantRole.TENANT_ADMIN)


def _parse_target_id(params: dict[str, Any]) -> uuid.UUID:
    raw = params.get("target_id")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise ValueError(f"target_id must be a UUID, got {raw!r}") from exc


async def targets_update(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Change one target of the operator's tenant, named by its id.

    Op-id: ``targets.update``. The fields other than ``target_id`` build a
    :class:`~meho_backplane.targets.schemas.TargetUpdate` (only the sent
    fields count). The row is looked up by id in the operator's tenant; a
    missing target is an error, not a silent no-op. The update itself is
    :func:`~meho_backplane.api.v1.targets.update_target`, which looks the
    row up again by its unique name in the same transaction. Service
    errors are raised again as :class:`ValueError`, so the dispatcher's
    ``connector_error`` result carries a clean message.
    """
    target_id = _parse_target_id(params)
    fields = {key: value for key, value in params.items() if key != "target_id"}
    try:
        body = TargetUpdate(**fields)
    except ValidationError as exc:
        raise ValueError(f"invalid target fields: {exc}") from exc
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session, session.begin():
            row = await resolve_target_by_id(session, operator.tenant_id, target_id)
            if row is None:
                raise ValueError(f"no live target with id {target_id} in your tenant")
            updated = await update_target(
                name=row.name, body=body, operator=operator, session=session
            )
    except HTTPException as exc:
        raise ValueError(_http_exception_message(exc)) from exc
    return {
        "status": "updated",
        "target_id": str(updated.id),
        "name": updated.name,
        "fields": sorted(fields),
    }


async def register_targets_update_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``targets.update`` typed op into ``endpoint_descriptor``."""
    await register_typed_operation(
        product="targets",
        version="1.x",
        impl_id="targets-registry",
        op_id=TARGETS_UPDATE_OP_ID,
        handler=targets_update,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="Change one target of your tenant, by its id.",
        description=_DESCRIPTION,
        parameter_schema=TARGETS_UPDATE_PARAMETER_SCHEMA,
        response_schema=TARGETS_UPDATE_RESPONSE_SCHEMA,
        tags=["targets", "write", "registry"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
