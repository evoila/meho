# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approver reads the script of a waiting ``linux.script.run``, page by page.

The approval row keeps no params. The full script is in the encrypted hand-off
store (:mod:`~meho_backplane.operations.approval_handoff`) until the approved
call runs. :func:`read_script_page` decrypts it without deleting it, hides
values that look like secrets over the whole script, and returns one page
(:func:`~meho_backplane.connectors.linux.script_preview.script_page_fields`).

The console and the REST route both use it, so ``meho approvals show`` and the
console show the same text. It works only while the request waits for approval.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.linux.script_preview import script_page_fields
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_handoff import (
    ApprovalHandoffMissingError,
)
from meho_backplane.operations.approval_handoff import read as read_handoff

__all__ = [
    "SCRIPT_GONE_MESSAGE",
    "SCRIPT_OP_ID",
    "ApprovalScriptGoneError",
    "ApprovalScriptMissingError",
    "read_script_page",
]

SCRIPT_OP_ID = "linux.script.run"

SCRIPT_GONE_MESSAGE = (
    "MEHO no longer keeps this script. It keeps the script only while the request "
    "waits for approval."
)


class ApprovalScriptMissingError(Exception):
    """The request has no script to read."""


class ApprovalScriptGoneError(Exception):
    """The request is decided or expired, so MEHO no longer keeps its script."""


async def read_script_page(
    session: AsyncSession, request: ApprovalRequest, page: int
) -> dict[str, Any]:
    """The script fields of page *page* of a waiting *request*.

    Raises:
        ApprovalScriptMissingError: The request is not a script run.
        ApprovalScriptGoneError: The request is no longer waiting.
        ScriptPageOutOfRangeError: *page* is not a page of the script.
    """
    if request.op_id != SCRIPT_OP_ID:
        raise ApprovalScriptMissingError("This request has no script.")
    if request.status != ApprovalRequestStatus.PENDING.value:
        raise ApprovalScriptGoneError(SCRIPT_GONE_MESSAGE)
    if request.execution_handle is None:
        raise ApprovalScriptGoneError(SCRIPT_GONE_MESSAGE)
    try:
        handoff = await read_handoff(
            session, request.execution_handle, request.id, request.tenant_id
        )
    except ApprovalHandoffMissingError as exc:
        raise ApprovalScriptGoneError(SCRIPT_GONE_MESSAGE) from exc
    params = handoff.get("params")
    script = params.get("script") if isinstance(params, dict) else None
    if not isinstance(script, str):
        raise ApprovalScriptMissingError("This request has no script.")
    return script_page_fields(script, page)
