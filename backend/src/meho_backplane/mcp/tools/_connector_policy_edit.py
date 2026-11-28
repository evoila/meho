# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_connector_edit_op`` with ``CUSTOM_POLICY`` on: a risk level or approval edit waits.

The tool asks for the edit with ``policy.op_edit``
(:mod:`meho_backplane.connectors.policy.op_edit`), with ``tighten_only``:
an edit that turns approval off or lowers a level is refused before
anything waits. A raise waits until an admin approves it in the web
console. With the switch off, nothing here runs.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.policy.op_edit import (
    POLICY_FIELDS,
    POLICY_OP_EDIT_OP_ID,
    PolicyEditAskRefused,
    approver_sentence,
    request_policy_edit,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools._connector_shared import (
    raise_invalid_params_for_builtin_write_forbidden,
    raise_invalid_params_for_connector_not_found,
    raise_invalid_params_for_edit_value_error,
    raise_invalid_params_for_legacy_tenant_copy,
)
from meho_backplane.operations.ingest import (
    BuiltinConnectorWriteForbiddenError,
    ConnectorNotFoundError,
    LegacyTenantConnectorCopyError,
)
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

__all__ = ["request_policy_edit_from_tool"]


async def request_policy_edit_from_tool(
    operator: Operator, arguments: dict[str, Any], fields: tuple[str, ...]
) -> dict[str, Any] | None:
    """Ask for the risk level / approval edit in *arguments*; ``None`` when it runs as today.

    ``None`` while the switch is off or when *arguments* set no policy
    field. *fields* are the edit fields the tool accepts; the set ones go
    into the request and wait with it.
    """
    if not custom_policy_on(get_settings()):
        return None
    if all(arguments.get(name) is None for name in POLICY_FIELDS):
        return None
    if "custom_description" in arguments and arguments["custom_description"] is None:
        raise McpInvalidParamsError(
            "meho_connector_edit_op: policy_edit_mixed_clear: safety_level and "
            "requires_approval wait for an admin's approval. Clear custom_description "
            "in a call of its own.",
            data={"error_code": "policy_edit_mixed_clear"},
        )
    params: dict[str, Any] = {
        "connector_id": arguments["connector_id"],
        "op_id": arguments["op_id"],
        "tenant_id": arguments.get("tenant_id"),
        "tighten_only": True,
    }
    params.update({name: arguments[name] for name in fields if arguments.get(name) is not None})
    try:
        result, card = await request_policy_edit(operator, POLICY_OP_EDIT_OP_ID, params)
    except PolicyEditAskRefused as exc:
        raise McpInvalidParamsError(
            f"meho_connector_edit_op: policy_edit_needs_person: {exc}",
            data={"error_code": "policy_edit_needs_person"},
        ) from exc
    except BuiltinConnectorWriteForbiddenError as exc:
        raise_invalid_params_for_builtin_write_forbidden(exc)
    except LegacyTenantConnectorCopyError as exc:
        raise_invalid_params_for_legacy_tenant_copy(exc)
    except ConnectorNotFoundError as exc:
        raise_invalid_params_for_connector_not_found(exc)
    except ValueError as exc:
        raise_invalid_params_for_edit_value_error(exc)
    status = str(result.get("status"))
    extras = result.get("extras") or {}
    return {
        "connector_id": arguments["connector_id"],
        "op_id": arguments["op_id"],
        "status": status,
        "approval_request_id": extras.get("approval_request_id"),
        "message": (
            approver_sentence(card)
            if status == "awaiting_approval"
            else str(result.get("error") or "The change is stored.")
        ),
    }
