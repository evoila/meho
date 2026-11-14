# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Targetless typed ``targets.update`` op: change fields of one target.

``targets.update`` changes the stored fields of one target of the
caller's tenant, for example a wrong ``secret_ref`` or a new host. It
runs :func:`~meho_backplane.api.v1.targets.apply_target_update`, the same
code as REST ``PATCH /api/v1/targets/{name}``. So every check of that route
applies here too: the product check, the ``secret_ref`` tenant check,
the host check, the TLS checks, the merge of ``secret_field_map``, the
platform-admin check on the Vault login keys in ``extras``, the audit of
TLS and capture changes, and the drop of kept logins after a connection
change. This module checks none of them again.

The target is named by its id, as ``list_targets`` shows it, never by
name. A name or alias could point at another target by the time an
approved call runs. Only targets of the caller's tenant are found. The
row found by id is locked and changed; MEHO does not look it up again
by name.

Like ``PATCH``, only the fields in the call change. An explicit ``null``
clears a field that can be empty (``fqdn``, ``secret_ref``, ``notes`` and
the other nullable fields). ``name`` is not a field here: a rename is a
remove and a new register.

Approval and role:

* ``safety_level="caution"`` + ``requires_approval=True``: every call
  waits until a person with ``tenant_admin`` approves it. ``host`` and
  ``fqdn`` are addresses MEHO connects to (``x-meho-caller-address``), and
  the call can also change ``secret_ref``, so a changed target is checked
  by a person before MEHO uses it. People who change a target at once use
  REST ``PATCH``, ``meho targets update`` or the web console.
* The role floor is ``tenant_admin``, the same as REST ``PATCH``. It is
  registered below, so the policy gate refuses a person with a lower role
  on every path (``call_operation``, the REST dispatch route, the CLI,
  the MCP tool), and the approval queue refuses an approver with a lower
  role.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog
from fastapi import HTTPException
from pydantic import ValidationError

from meho_backplane.api.v1.targets import apply_target_update
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.targets.ops import (
    _GROUP_KEY,
    _GROUP_WHEN_TO_USE,
    _http_exception_message,
    _parse_target_id,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.role_floors import register_role_floor
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.targets.keycloak_realm_warnings import keycloak_realm_warnings
from meho_backplane.targets.resolver import resolve_target_by_id
from meho_backplane.targets.schemas import TargetUpdate

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations._preview import PreviewContext
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "TARGETS_UPDATE_OP_ID",
    "TARGETS_UPDATE_PARAMETER_SCHEMA",
    "TARGETS_UPDATE_RESPONSE_SCHEMA",
    "build_targets_update_preview",
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
                "`id`. Never the name: a name or alias could point at another "
                "target. Only targets of your own tenant are found."
            ),
        },
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "New list of alternate names. `[]` removes all aliases.",
        },
        "product": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "New product token; must be a registered connector product.",
        },
        "version": {
            "type": _NULLABLE_STRING,
            "maxLength": 100,
            "description": "New product version. `null` clears it.",
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": "New host or IP. Checked like on register.",
            "x-meho-caller-address": True,
        },
        "port": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 65535,
            "description": "New TCP port. `null` uses the connector default.",
        },
        "fqdn": {
            "type": _NULLABLE_STRING,
            "description": "New fully-qualified domain name. `null` clears it.",
            "x-meho-caller-address": True,
        },
        "secret_ref": {
            "type": _NULLABLE_STRING,
            "description": (
                "New Vault path to the target's credential. Must lie within "
                "your tenant's secret path. `null` clears it."
            ),
        },
        "auth_model": {
            "type": "string",
            "enum": [m.value for m in AuthModel],
            "description": "New per-target identity model.",
        },
        "verify_tls": {
            "type": "boolean",
            "description": (
                "Whether MEHO checks the target's TLS certificate. `false` is "
                "audited. Cannot be `false` together with a `tls_ca_pin`."
            ),
        },
        "tls_ca_pin": {
            "type": _NULLABLE_STRING,
            "description": "New PEM CA bundle to trust for this target. `null` clears it.",
        },
        "tls_server_name": {
            "type": _NULLABLE_STRING,
            "maxLength": 512,
            "description": "New TLS server name, when it differs from `host`. `null` clears it.",
        },
        "vpn_required": {
            "type": "boolean",
            "description": "Whether a VPN must be up to reach the target.",
        },
        "secret_field_map": {
            "type": ["object", "null"],
            "additionalProperties": {"type": _NULLABLE_STRING},
            "description": (
                "Entries are merged into the stored map: a string value adds or "
                "replaces an entry, a `null` value removes it. `null` for the "
                "whole map clears it."
            ),
        },
        "extras": {
            "type": "object",
            "description": "New connector-specific metadata. Replaces the stored map.",
        },
        "notes": {
            "type": _NULLABLE_STRING,
            "description": "New free-text notes. `null` clears them.",
        },
        "preferred_impl_id": {
            "type": _NULLABLE_STRING,
            "maxLength": 200,
            "description": (
                "New connector-implementation choice, checked against the "
                "implementations of the product. `null` clears it."
            ),
        },
        "flight_recorder_capture": {
            "type": ["boolean", "null"],
            "description": (
                "`true` or `false` sets the flight recorder capture for this "
                "target; `null` returns it to the tenant default."
            ),
        },
    },
    "required": ["target_id"],
    "additionalProperties": False,
    "examples": [{"target_id": "<target-uuid>", "secret_ref": "<vault-path>"}],
}

TARGETS_UPDATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_id": {"type": "string", "description": "UUID of the changed target."},
        "name": {"type": "string", "description": "Name of the changed target."},
        "product": {"type": "string", "description": "Product of the target after the change."},
        "version": {
            "type": _NULLABLE_STRING,
            "description": "Version of the target after the change, if known.",
        },
        "host": {"type": "string", "description": "Host of the target after the change."},
        "updated_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of the fields the call changed.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Only present when a setting is allowed but risky, for example a "
                "Keycloak target that manages the realm MEHO itself signs in with. "
                "Tell the person about each line."
            ),
        },
    },
    "required": ["target_id", "name", "product", "version", "host", "updated_fields"],
}

_DESCRIPTION = (
    "Change fields of one target of your tenant, named by its UUID, with the "
    "code of REST `PATCH /api/v1/targets/{name}`. Only the sent fields "
    "change; `null` clears a field that can be empty. Needs `tenant_admin`; "
    "each call waits for a person's approval."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "A target has a wrong or old field, for example a wrong `secret_ref`, "
        "a new host or a new port. Use it instead of removing and registering "
        "the target again."
    ),
    "preconditions": (
        "A person needs the `tenant_admin` role; any lower role is refused. "
        "Each call waits until a person with that role approves it. "
        "Get the target's `id` from list_targets; do not pass a name. "
        "Send at least one field to change."
    ),
    "parameter_hints": {
        "target_id": "Required. The target's UUID from list_targets.",
        "secret_ref": "Optional. A Vault path inside your tenant's secret path.",
        "host": "Optional. The new host or IP.",
    },
    "output_shape": "{'target_id', 'name', 'product', 'version', 'host', 'updated_fields'}",
    "next_step": (
        "The call waits for approval and then runs with the same params. "
        "When you changed the host, port, product or version, call "
        "targets.probe so MEHO detects the product and version again."
    ),
}

# The policy gate checks the floor for a person on every path, and the
# approval queue checks it for the approver.
register_role_floor(TARGETS_UPDATE_OP_ID, TenantRole.TENANT_ADMIN)


def _update_fields(params: dict[str, Any]) -> dict[str, Any]:
    fields = {k: v for k, v in params.items() if k != "target_id"}
    if not fields:
        raise ValueError(
            "targets.update needs at least one field to change besides target_id; "
            "nothing was changed"
        )
    return fields


async def targets_update(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Change the sent fields of one target of the operator's tenant.

    Op-id: ``targets.update``. Targetless typed op; the target is named by
    ``target_id`` in *params*. The row is looked up by id in the operator's
    tenant only and locked until the change commits; that row goes to
    :func:`~meho_backplane.api.v1.targets.apply_target_update`. A missing
    or deleted target, a call with no field and every service or model
    check failure is a :class:`ValueError`; nothing is changed then.
    """
    target_id = _parse_target_id(params)
    try:
        body = TargetUpdate(**_update_fields(params))
    except ValidationError as exc:
        raise ValueError(f"invalid target parameters: {exc}") from exc

    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session, session.begin():
            row = await resolve_target_by_id(
                session, operator.tenant_id, target_id, for_update=True
            )
            if row is None:
                raise ValueError(
                    f"no live target with id {target_id} in your tenant; nothing was changed"
                )
            structlog.contextvars.bind_contextvars(target_id=str(row.id), target_name=row.name)
            updated = await apply_target_update(row, body, operator, session)
    except HTTPException as exc:
        raise ValueError(_http_exception_message(exc)) from exc

    warnings = keycloak_realm_warnings(updated.product, updated.extras)
    return {
        "target_id": str(updated.id),
        "name": updated.name,
        "product": updated.product,
        "version": updated.version,
        "host": updated.host,
        "updated_fields": sorted(body.model_fields_set),
        **({"warnings": warnings} if warnings else {}),
    }


async def build_targets_update_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show which target ``targets.update`` would change, and which fields.

    Names the target (id, name, host) and its connector link, and lists
    the names of the fields the call sends. When no live target with the
    id exists in the caller's tenant, it says so; the call would fail.
    Reads only.
    """
    target_id = _parse_target_id(ctx.params)
    fields = sorted(k for k in ctx.params if k != "target_id")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await resolve_target_by_id(session, ctx.operator.tenant_id, target_id)
    if row is None:
        return {
            "action": TARGETS_UPDATE_OP_ID,
            "target": None,
            "fields": fields,
            "result_if_run": "fails",
            "note": "no live target with this id in your tenant",
        }
    return {
        "action": TARGETS_UPDATE_OP_ID,
        "target": {"id": str(row.id), "name": row.name, "host": row.host},
        "connector": {
            "product": row.product,
            "version": row.version,
            "preferred_impl_id": row.preferred_impl_id,
        },
        "fields": fields,
        "result_if_run": "updated" if fields else "fails",
    }


async def register_targets_update_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``targets.update`` typed op into ``endpoint_descriptor``.

    Queued by the package ``__init__``. Uses the same ``registry`` group and
    group text as the other ``targets.*`` ops. Safe to run twice.
    ``requires_approval=True`` is also what the address marker on ``host``
    and ``fqdn`` sets.
    """
    await register_typed_operation(
        product="targets",
        version="1.x",
        impl_id="targets-registry",
        op_id=TARGETS_UPDATE_OP_ID,
        handler=targets_update,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary="Change fields of one target of your tenant, by its id.",
        description=_DESCRIPTION,
        parameter_schema=TARGETS_UPDATE_PARAMETER_SCHEMA,
        response_schema=TARGETS_UPDATE_RESPONSE_SCHEMA,
        tags=["targets", "write", "registry"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
