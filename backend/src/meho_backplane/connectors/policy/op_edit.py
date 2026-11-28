# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``policy.op_edit`` and ``policy.op_remove_override``: change how one operation waits.

With the ``CUSTOM_POLICY`` switch on, an edit of an operation's risk level
or approval (``safety_level`` / ``requires_approval``), and the removal of
such an edit (an override), is a change of the policy. The REST routes,
``meho connector edit-op`` / ``remove-override`` and the admin MCP tool
``meho_connector_edit_op`` ask for it with one of these operations:

* only a person asks: an agent login, a service login and a hosted agent
  run are refused before anything waits (:func:`op_edit_requester_refusal`);
* every check of the edit runs before the request waits, as for a person
  with a fresh sign-in (:func:`check_op_edit`); a refused edit makes no
  request;
* the request waits until an admin approves it with a click in the web
  console. Only a platform admin approves an edit that ends approval
  (``ends_approval`` on the card) or that changes the whole install
  (:func:`platform_admin_approves`);
* an ask that names an override of another tenant is refused before
  anything waits (:data:`OTHER_TENANT_REFUSAL`);
* the approved run checks the card's record of who asked again, then
  builds the card again with the values of that moment, with the same
  function and the card's scope. When the two differ, it writes nothing
  (:data:`POLICY_CHANGED_REFUSAL`). Else it runs the edit with the person
  who clicked as the operator (:func:`policy_op_edit`,
  :func:`policy_op_remove_override`) and stores the override where the
  card says.

The operations exist only while ``CUSTOM_POLICY`` is on. With the switch
off the registrar deletes their rows, and a run refuses
(:data:`SWITCH_OFF_REFUSAL`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import PrincipalKind, is_human_principal
from meho_backplane.connectors.policy.classify import (
    GROUP_KEY,
    GROUP_WHEN_TO_USE,
    POLICY_REGISTRY_CONNECTOR_ID,
)
from meho_backplane.operations._preview import approved_preview
from meho_backplane.operations.secondary_target_policy import (
    approved_requester_sub,
    approved_requester_subject,
)
from meho_backplane.operations.typed_register import (
    register_typed_operation,
    remove_built_in_operation_rows,
)
from meho_backplane.policy.switches import custom_policy_on, custom_policy_on_in_environment
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations._preview import PreviewContext
    from meho_backplane.operations.op_policy import PolicyEditResult
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "EDIT_NEEDS_PERSON",
    "OTHER_TENANT_REFUSAL",
    "POLICY_CHANGED_REFUSAL",
    "POLICY_FIELDS",
    "POLICY_OP_EDIT_OP_ID",
    "POLICY_OP_REMOVE_OVERRIDE_OP_ID",
    "SWITCH_OFF_REFUSAL",
    "PolicyEditAskRefused",
    "approver_sentence",
    "build_op_edit_preview",
    "build_remove_override_preview",
    "op_edit_park_check",
    "op_edit_requester_refusal",
    "platform_admin_approves",
    "policy_op_edit",
    "policy_op_remove_override",
    "register_policy_op_edit_operations",
    "remove_override_park_check",
    "request_policy_edit",
]

POLICY_OP_EDIT_OP_ID: Final[str] = "policy.op_edit"
POLICY_OP_REMOVE_OVERRIDE_OP_ID: Final[str] = "policy.op_remove_override"

#: The fields of an edit that change the policy; the request waits when one is set.
POLICY_FIELDS: Final[tuple[str, ...]] = ("safety_level", "requires_approval")

#: The other fields an edit may carry; they wait with the policy fields.
_ROW_FIELDS: Final[tuple[str, ...]] = ("custom_description", "is_enabled")

EDIT_NEEDS_PERSON: Final[str] = (
    "Only a person can ask to change the risk level or approval of an operation. "
    "Use `meho connector edit-op`, `meho connector remove-override` or the REST API "
    "with your own login."
)

EDIT_NEEDS_APPROVAL: Final[str] = (
    "This edit runs only after an admin approves it in the web console. Send the edit again."
)

SWITCH_OFF_REFUSAL: Final[str] = (
    "The switch CUSTOM_POLICY is off now, so MEHO does not run this edit. Nothing was "
    "changed. Send the edit again."
)

POLICY_CHANGED_REFUSAL: Final[str] = (
    "The policy changed since this request was made. Send the edit again."
)

OTHER_TENANT_REFUSAL: Final[str] = (
    "This names an override of another tenant. Act in that tenant to change it."
)

_NO_SCOPE_ON_CARD: Final[str] = (
    "The approval card does not say where the edit goes, so MEHO does not run it. "
    "Send the edit again."
)

_PLATFORM_ADMIN_APPROVES: Final[str] = (
    "This edit turns approval off or changes the whole install. Only a platform admin "
    "may approve it."
)

_ADMIN_SENTENCE: Final[str] = (
    "The edit waits for approval. An Admin approves it in the web console."
)
_PLATFORM_ADMIN_SENTENCE: Final[str] = (
    "The edit waits for approval. A platform admin approves it in the web console."
)

_SAFETY_LEVELS: Final[list[str]] = ["safe", "caution", "dangerous", "destructive"]

_CONNECTOR_FIELDS: Final[dict[str, Any]] = {
    "connector_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "description": "The connector of the operation, for example vault-1.x.",
    },
    "op_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1000,
        "description": "The operation to change.",
    },
}

_REASON_FIELD: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 1024,
    "description": "Why you make the change. Needed when the change ends approval.",
}

EDIT_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["connector_id", "op_id"],
    "anyOf": [{"required": ["safety_level"]}, {"required": ["requires_approval"]}],
    "properties": {
        **_CONNECTOR_FIELDS,
        "tenant_id": {
            "type": ["string", "null"],
            "format": "uuid",
            "description": "The tenant of the connector. Leave it out for your own tenant.",
        },
        "safety_level": {
            "type": "string",
            "enum": _SAFETY_LEVELS,
            "description": "The new risk level. It can only go up.",
        },
        "requires_approval": {
            "type": "boolean",
            "description": "true: every call waits for approval. false: approval off.",
        },
        "custom_description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "description": "A new description of the operation. Changes with the edit.",
        },
        "is_enabled": {
            "type": "boolean",
            "description": "Turn the operation on or off. Changes with the edit.",
        },
        "reason": _REASON_FIELD,
        "tighten_only": {
            "type": "boolean",
            "description": "true: refuse an edit that turns approval off or lowers a level.",
        },
    },
}

REMOVE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["connector_id", "op_id"],
    "properties": {
        **_CONNECTOR_FIELDS,
        "override_tenant_id": {
            "type": ["string", "null"],
            "format": "uuid",
            "description": "The tenant whose override to remove. Leave it out for the install.",
        },
        "reason": _REASON_FIELD,
    },
}

_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "connector_id": {"type": "string", "description": "The connector of the operation."},
        "op_id": {"type": "string", "description": "The operation that changed."},
    },
    "required": ["connector_id", "op_id"],
}

_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "A tenant admin wants an operation to have a higher risk level, to always wait "
        "for approval, or to remove such a change."
    ),
    "preconditions": (
        "Needs the tenant_admin role and a person's own login. The request waits until "
        "an admin approves it in the web console."
    ),
    "output_shape": "{'connector_id', 'op_id'}",
    "next_step": "Ask an admin to approve the request in the web console.",
}


# ---------------------------------------------------------------------------
# Who may ask, who may approve
# ---------------------------------------------------------------------------


def op_edit_requester_refusal(operator: Operator, params: dict[str, Any]) -> str | None:
    """Why *operator* may not ask for the edit, or ``None`` when it may.

    Only a person asks: no agent login, no service login, no hosted agent
    run. The policy gate runs it on every path, before the call waits.
    """
    del params  # every edit has the same rule
    if not is_human_principal(operator) or resolve_actor_sub() is not None:
        return EDIT_NEEDS_PERSON
    return None


def _requester_refusal_at_run() -> str | None:
    """Check the card's record of who asked again; no record is a refusal."""
    requester = approved_requester_subject()
    if (
        requester is None
        or requester.get("principal_kind") != PrincipalKind.USER.value
        or requester.get("act") is not None
    ):
        return EDIT_NEEDS_PERSON
    return None


def platform_admin_approves(card: dict[str, Any]) -> str | None:
    """Why only a platform admin approves the request of *card*, or ``None``.

    That is an edit that ends approval in any scope, or one for the whole
    install. A card that does not say counts as one.
    """
    if card.get("ends_approval") is not False or card.get("scope") != "tenant":
        return _PLATFORM_ADMIN_APPROVES
    return None


def approver_sentence(card: dict[str, Any]) -> str:
    """The sentence that says who approves the request of *card*."""
    return _PLATFORM_ADMIN_SENTENCE if platform_admin_approves(card) else _ADMIN_SENTENCE


# ---------------------------------------------------------------------------
# The checks before the request waits, and the approval card
# ---------------------------------------------------------------------------


def _uuid_or_none(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


def _ends_approval(result: PolicyEditResult) -> bool:
    approval = result.changes.get("requires_approval")
    ended_here = approval is not None and approval["old"] and not approval["new"]
    return bool(ended_here or result.approval_ended_for_tenants)


def _refuse_other_tenant(operator: Operator, tenant_id: UUID | None) -> None:
    """Refuse an ask that names an override of another tenant than the caller's."""
    if tenant_id is not None and tenant_id != operator.tenant_id:
        raise PolicyEditAskRefused(OTHER_TENANT_REFUSAL)


def _card(
    params: dict[str, Any],
    result: PolicyEditResult,
    scope_tenant_id: UUID | None,
    row_tenant_id: UUID | None,
) -> dict[str, Any]:
    """The approval card: the full effect of an edit or removal, JSON-shaped."""
    return {
        "connector_id": params["connector_id"],
        "op_id": params["op_id"],
        "scope": "install" if scope_tenant_id is None else "tenant",
        "scope_tenant_id": None if scope_tenant_id is None else str(scope_tenant_id),
        "row_tenant_id": None if row_tenant_id is None else str(row_tenant_id),
        "changes": result.changes,
        "reason": params.get("reason"),
        "ends_approval": _ends_approval(result),
    }


async def check_op_edit(
    operator: Operator, params: dict[str, Any], *, card_scope: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run every check of the edit in *params* and write nothing; return its approval card.

    The override goes to the scope of *card_scope* (a stored card) when
    given, else to the scope of *operator* (the person who asks). Raises
    :class:`PolicyEditAskRefused` and the errors of
    :meth:`~meho_backplane.operations.ingest.service.ReviewService.edit_op`.
    """
    from meho_backplane.operations.ingest.service import ReviewService

    tenant_id = _uuid_or_none(params.get("tenant_id"))
    _refuse_other_tenant(operator, tenant_id)
    scope: dict[str, Any] = {}
    if card_scope is not None:
        scope["policy_scope_tenant_id"] = _uuid_or_none(card_scope.get("scope_tenant_id"))
    result, scope_tenant_id, row_tenant_id = await ReviewService(operator).check_op_edit(
        params["connector_id"],
        params["op_id"],
        tenant_id=tenant_id,
        safety_level=params.get("safety_level"),
        requires_approval=params.get("requires_approval"),
        reason=params.get("reason"),
        row_edit=any(params.get(name) is not None for name in _ROW_FIELDS),
        tighten_only=bool(params.get("tighten_only")),
        **scope,
    )
    card = _card(params, result, scope_tenant_id, row_tenant_id)
    card.update({name: params[name] for name in _ROW_FIELDS if params.get(name) is not None})
    return card


async def check_override_removal(operator: Operator, params: dict[str, Any]) -> dict[str, Any]:
    """Run every check of the removal in *params* and write nothing; return its approval card.

    Raises :class:`PolicyEditAskRefused` and the errors of
    :meth:`~meho_backplane.operations.ingest.service.ReviewService.remove_op_override`.
    """
    from meho_backplane.operations.ingest.service import ReviewService

    override_tenant_id = _uuid_or_none(params.get("override_tenant_id"))
    _refuse_other_tenant(operator, override_tenant_id)
    result, row_tenant_id = await ReviewService(operator).check_override_removal(
        params["connector_id"],
        params["op_id"],
        override_tenant_id=override_tenant_id,
        reason=params.get("reason"),
    )
    return {**_card(params, result, override_tenant_id, row_tenant_id), "override_removed": True}


async def build_op_edit_preview(ctx: PreviewContext) -> dict[str, Any]:
    """The approval card of an edit, stored on the request when it is made."""
    refusal = op_edit_requester_refusal(ctx.operator, ctx.params)
    if refusal is not None:
        raise ValueError(refusal)
    return await check_op_edit(ctx.operator, ctx.params)


async def build_remove_override_preview(ctx: PreviewContext) -> dict[str, Any]:
    """The approval card of a removal, stored on the request when it is made."""
    refusal = op_edit_requester_refusal(ctx.operator, ctx.params)
    if refusal is not None:
        raise ValueError(refusal)
    return await check_override_removal(ctx.operator, ctx.params)


async def _refusal_of(check: Any, ctx: PreviewContext) -> str | None:
    from meho_backplane.operations.ingest.exceptions import (
        BuiltinConnectorWriteForbiddenError,
        ConnectorNotFoundError,
        LegacyTenantConnectorCopyError,
    )
    from meho_backplane.operations.op_policy import OverrideNotFoundError

    try:
        await check(ctx.operator, ctx.params)
    except (
        PolicyEditAskRefused,
        ValueError,
        OverrideNotFoundError,
        ConnectorNotFoundError,
        BuiltinConnectorWriteForbiddenError,
        LegacyTenantConnectorCopyError,
    ) as exc:
        return str(exc)
    return None


async def op_edit_park_check(ctx: PreviewContext) -> str | None:
    """Refuse an edit that its checks refuse, before it waits, on every path."""
    return await _refusal_of(check_op_edit, ctx)


async def remove_override_park_check(ctx: PreviewContext) -> str | None:
    """Refuse a removal that its checks refuse, before it waits, on every path."""
    return await _refusal_of(check_override_removal, ctx)


# ---------------------------------------------------------------------------
# The request, for the REST routes and the MCP tool
# ---------------------------------------------------------------------------


class PolicyEditAskRefused(Exception):  # noqa: N818 - a refusal, not a fault
    """The ask is refused before it waits: the caller is not a person, or names another tenant."""


async def request_policy_edit(
    operator: Operator, op_id: str, params: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask for the edit or removal *op_id* with *params*; return the call result and the card.

    Refuses before anything waits: :class:`PolicyEditAskRefused` for a
    caller who is not a person, and the errors of the service for an edit
    its checks refuse. The request is a call of *op_id* through
    :func:`~meho_backplane.operations.meta_tools.call_operation`, so the
    role check, the approval and the audit row are those of every call.
    """
    refusal = op_edit_requester_refusal(operator, params)
    if refusal is not None:
        raise PolicyEditAskRefused(refusal)
    check = check_op_edit if op_id == POLICY_OP_EDIT_OP_ID else check_override_removal
    card = await check(operator, params)
    from meho_backplane.operations.meta_tools import call_operation

    result = await call_operation(
        operator,
        {
            "connector_id": POLICY_REGISTRY_CONNECTOR_ID,
            "op_id": op_id,
            "target": None,
            "params": params,
        },
    )
    return result, card


# ---------------------------------------------------------------------------
# The approved run
# ---------------------------------------------------------------------------


def _check_who_runs(operator: Operator, params: dict[str, Any], op_id: str) -> dict[str, Any]:
    """Refuse a run that is not allowed; return the approval card (empty in the console).

    A run without an approval writes only for an admin in the web console.
    After an approval, the card's record of who asked is checked again.
    """
    if not custom_policy_on(get_settings()):
        raise ValueError(SWITCH_OFF_REFUSAL)
    requester = approved_requester_sub()
    if requester is None or requester == operator.sub:
        if not (operator.web_session and operator.may_approve_rights_change()):
            raise ValueError(EDIT_NEEDS_APPROVAL)
        refusal = op_edit_requester_refusal(operator, params)
    else:
        refusal = _requester_refusal_at_run()
    if refusal is not None:
        raise ValueError(EDIT_NEEDS_PERSON)
    return approved_preview(op_id) or {}


def _refuse_if_changed(card: dict[str, Any], now: dict[str, Any]) -> None:
    """Refuse the approved run when the effect of now differs from the stored card."""
    if now != card:
        raise ValueError(POLICY_CHANGED_REFUSAL)


async def policy_op_edit(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Run one approved risk level / approval edit.

    Op-id: ``policy.op_edit``. Targetless. After an approval, the run first
    builds the card again with the values of now and the card's scope, and
    writes nothing when it differs from the stored card. Then it runs the
    edit as a person in the web console (``person_present``), with
    *operator* (the person who clicked) as the one who changes it, and the
    override goes where the card says.
    """
    del target
    from meho_backplane.operations.ingest.service import ReviewService

    card = _check_who_runs(operator, params, POLICY_OP_EDIT_OP_ID)
    scope: dict[str, Any] = {}
    if approved_requester_sub() is not None:
        if card.get("scope") not in {"tenant", "install"}:
            raise ValueError(_NO_SCOPE_ON_CARD)
        _refuse_if_changed(card, await check_op_edit(operator, params, card_scope=card))
        scope["policy_scope_tenant_id"] = _uuid_or_none(card.get("scope_tenant_id"))
    fields = {name: params[name] for name in (*POLICY_FIELDS, *_ROW_FIELDS) if name in params}
    warnings = await ReviewService(operator).edit_op(
        params["connector_id"],
        params["op_id"],
        tenant_id=_uuid_or_none(params.get("tenant_id")),
        reason=params.get("reason"),
        person_present=True,
        tighten_only=bool(params.get("tighten_only")),
        **fields,
        **scope,
    )
    return {
        "connector_id": params["connector_id"],
        "op_id": params["op_id"],
        "warnings": [warning.model_dump(mode="json") for warning in warnings],
    }


async def policy_op_remove_override(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Run one approved removal of an override.

    Op-id: ``policy.op_remove_override``. Targetless. Like
    :func:`policy_op_edit`; the scope is in *params*.
    """
    del target
    from meho_backplane.operations.ingest.service import ReviewService

    card = _check_who_runs(operator, params, POLICY_OP_REMOVE_OVERRIDE_OP_ID)
    if approved_requester_sub() is not None:
        _refuse_if_changed(card, await check_override_removal(operator, params))
    await ReviewService(operator).remove_op_override(
        params["connector_id"],
        params["op_id"],
        override_tenant_id=_uuid_or_none(params.get("override_tenant_id")),
        reason=params.get("reason"),
        person_present=True,
    )
    return {"connector_id": params["connector_id"], "op_id": params["op_id"]}


async def register_policy_op_edit_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert both operations; delete their rows while ``CUSTOM_POLICY`` is off."""
    op_ids = (POLICY_OP_EDIT_OP_ID, POLICY_OP_REMOVE_OVERRIDE_OP_ID)
    if not custom_policy_on_in_environment():
        await remove_built_in_operation_rows(
            product="policy", version="1.x", impl_id="policy-registry", op_ids=op_ids
        )
        return
    rows = (
        (
            POLICY_OP_EDIT_OP_ID,
            policy_op_edit,
            "Raise the risk level of an operation, or change its approval.",
            EDIT_PARAMETER_SCHEMA,
        ),
        (
            POLICY_OP_REMOVE_OVERRIDE_OP_ID,
            policy_op_remove_override,
            "Remove a change of the risk level or approval of an operation.",
            REMOVE_PARAMETER_SCHEMA,
        ),
    )
    for op_id, handler, summary, schema in rows:
        await register_typed_operation(
            product="policy",
            version="1.x",
            impl_id="policy-registry",
            op_id=op_id,
            handler=handler,
            group_key=GROUP_KEY,
            when_to_use=GROUP_WHEN_TO_USE,
            summary=summary,
            description=(
                f"{summary} A change of the policy: it waits until an admin approves it "
                "in the web console. Only a person asks for it."
            ),
            parameter_schema=schema,
            response_schema=_RESPONSE_SCHEMA,
            tags=["policy", "write", "rights"],
            safety_level="dangerous",
            requires_approval=True,
            changes_rights=True,
            llm_instructions=_LLM_INSTRUCTIONS,
            embedding_service=embedding_service,
        )
