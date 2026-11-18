# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approver reads the script of a waiting ``linux.script.run``, page by page.

The approval row keeps no params. The full script is in the encrypted hand-off
store (:mod:`~meho_backplane.operations.approval_handoff`) until the approved
call runs. :func:`read_script_page` decrypts it without deleting it, hides
values that look like secrets over the whole script, and returns one page
(:func:`~meho_backplane.connectors.linux.script_preview.shown_script_page_fields`).

The redaction pass over a large script takes time, so it runs once per
request, in a worker thread, not in the event loop. The result holds no value
that looks like a secret; the last few are kept in memory
(:data:`_SHOWN_CACHE_SIZE`), so reading the other pages does not run it again.

The console and the REST route both use it, so ``meho approvals show`` and the
console show the same text. It works only while the request waits for approval.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.linux.script_preview import (
    ShownScript,
    shown_script,
    shown_script_page_fields,
)
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_handoff import (
    ApprovalHandoffError,
    ApprovalHandoffMissingError,
)
from meho_backplane.operations.approval_handoff import read as read_handoff

__all__ = [
    "SCRIPT_GONE_MESSAGE",
    "SCRIPT_OP_ID",
    "SCRIPT_UNREADABLE_MESSAGE",
    "ApprovalScriptGoneError",
    "ApprovalScriptMissingError",
    "ApprovalScriptUnreadableError",
    "read_script_page",
]

SCRIPT_OP_ID = "linux.script.run"

SCRIPT_GONE_MESSAGE = (
    "MEHO no longer keeps this script. It keeps the script only while the request "
    "waits for approval."
)

SCRIPT_UNREADABLE_MESSAGE = (
    "MEHO cannot read the stored script of this request, so it cannot show it. "
    "Approving it fails for the same reason. Reject the request and ask for a new one."
)

#: How many shown scripts stay in memory for later page reads.
_SHOWN_CACHE_SIZE: Final[int] = 8

_shown_cache: OrderedDict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], ShownScript] = OrderedDict()


class ApprovalScriptMissingError(Exception):
    """The request has no script to read."""


class ApprovalScriptGoneError(Exception):
    """The request is decided or expired, so MEHO no longer keeps its script."""


class ApprovalScriptUnreadableError(Exception):
    """The stored script cannot be decrypted or has a wrong shape."""


async def read_script_page(
    session: AsyncSession, request: ApprovalRequest, page: int
) -> dict[str, Any]:
    """The script fields of page *page* of a waiting *request*.

    Raises:
        ApprovalScriptMissingError: The request is not a script run.
        ApprovalScriptGoneError: The request is no longer waiting.
        ApprovalScriptUnreadableError: The stored script cannot be read back.
        ScriptPageOutOfRangeError: *page* is not a page of the script.
    """
    if request.op_id != SCRIPT_OP_ID:
        raise ApprovalScriptMissingError("This request has no script.")
    if request.status != ApprovalRequestStatus.PENDING.value:
        raise ApprovalScriptGoneError(SCRIPT_GONE_MESSAGE)
    if request.execution_handle is None:
        raise ApprovalScriptGoneError(SCRIPT_GONE_MESSAGE)
    key = (request.tenant_id, request.id, request.execution_handle)
    shown = _shown_cache.get(key)
    if shown is None:
        shown = await _read_shown(session, request, request.execution_handle)
        _shown_cache[key] = shown
        while len(_shown_cache) > _SHOWN_CACHE_SIZE:
            _shown_cache.popitem(last=False)
    else:
        _shown_cache.move_to_end(key)
    return shown_script_page_fields(shown, page)


async def _read_shown(
    session: AsyncSession, request: ApprovalRequest, handle: uuid.UUID
) -> ShownScript:
    """Decrypt the stored script of *request* and run the redaction pass in a thread."""
    try:
        handoff = await read_handoff(session, handle, request.id, request.tenant_id)
    except ApprovalHandoffMissingError as exc:
        raise ApprovalScriptGoneError(SCRIPT_GONE_MESSAGE) from exc
    except ApprovalHandoffError as exc:
        raise ApprovalScriptUnreadableError(SCRIPT_UNREADABLE_MESSAGE) from exc
    params = handoff.get("params")
    script = params.get("script") if isinstance(params, dict) else None
    if not isinstance(script, str):
        raise ApprovalScriptMissingError("This request has no script.")
    return await asyncio.to_thread(shown_script, script)
