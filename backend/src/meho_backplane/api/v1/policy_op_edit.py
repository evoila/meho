# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The answer of the operation PATCH and override DELETE routes when an edit waits.

With the ``CUSTOM_POLICY`` switch on, a PATCH that sets ``safety_level``
or ``requires_approval``, and the DELETE of an override, ask for the
edit with ``policy.op_edit`` / ``policy.op_remove_override``
(:mod:`meho_backplane.connectors.policy.op_edit`). The route answers
``202`` with :class:`ClassificationRequestResponse`, the one answer shape
of the PATCH route, while the request waits
for an admin's click in the web console, and ``403`` when the call is
refused. With the switch off, nothing here runs.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from meho_backplane.api.v1.policy_classification import ClassificationRequestResponse
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.policy.op_edit import POLICY_FIELDS, approver_sentence
from meho_backplane.operations.ingest.api_schemas import EditOpBody
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

__all__ = [
    "OP_PATCH_RESPONSES",
    "POLICY_EDIT_RESPONSES",
    "edit_waits",
    "op_edit_params",
    "policy_edit_answer",
    "remove_override_params",
]


#: The extra answer of the DELETE route of an override.
POLICY_EDIT_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {
        "model": ClassificationRequestResponse,
        "description": (
            "With CUSTOM_POLICY on: the change waits until an admin approves it in the web console."
        ),
    },
}

#: The extra answer of the operation PATCH route: a category / labels change,
#: or with CUSTOM_POLICY on a risk level / approval edit. One body shape.
OP_PATCH_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {
        "model": ClassificationRequestResponse,
        "description": (
            "category / labels were sent: the change waits until a tenant admin approves "
            "it in the web console. With CUSTOM_POLICY on, safety_level / requires_approval "
            "were sent: the change waits until an admin approves it in the web console."
        ),
    },
}

_STATUS_CODES = {"awaiting_approval": 202, "ok": 200, "denied": 403}


def edit_waits(body: EditOpBody) -> bool:
    """True when the PATCH *body* waits: the switch is on and it sets a policy field."""
    return custom_policy_on(get_settings()) and any(
        getattr(body, name) is not None for name in POLICY_FIELDS
    )


def op_edit_params(
    connector_id: str, op_id: str, body: EditOpBody, operator: Operator
) -> dict[str, Any]:
    """The params of ``policy.op_edit`` for one PATCH. Only the fields that are set."""
    params: dict[str, Any] = {
        "connector_id": connector_id,
        "op_id": op_id,
        "tenant_id": str(operator.tenant_id),
    }
    for name in (*POLICY_FIELDS, "custom_description", "is_enabled", "reason"):
        value = getattr(body, name)
        if value is not None:
            params[name] = value
    return params


def remove_override_params(
    connector_id: str, op_id: str, tenant_id: Any, reason: str | None
) -> dict[str, Any]:
    """The params of ``policy.op_remove_override`` for one DELETE."""
    params: dict[str, Any] = {
        "connector_id": connector_id,
        "op_id": op_id,
        "override_tenant_id": None if tenant_id is None else str(tenant_id),
    }
    if reason is not None:
        params["reason"] = reason
    return params


def policy_edit_answer(result: dict[str, Any], card: dict[str, Any]) -> JSONResponse:
    """Map the call result of an edit request to HTTP.

    ``202`` when it waits, ``403`` when the call is refused, ``200`` when
    it ran at once (an admin in the web console), ``400`` for any other
    error.
    """
    status = str(result.get("status"))
    extras = result.get("extras") or {}
    message = (
        approver_sentence(card)
        if status == "awaiting_approval"
        else str(result.get("error") or "The change is stored.")
    )
    body = ClassificationRequestResponse(
        status=status,
        approval_request_id=extras.get("approval_request_id"),
        message=message,
        matched_operations=1,
        changed_operations=1 if status == "ok" else 0,
    )
    return JSONResponse(
        status_code=_STATUS_CODES.get(status, 400), content=body.model_dump(mode="json")
    )
