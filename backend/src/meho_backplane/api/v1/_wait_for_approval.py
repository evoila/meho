# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST writes that wait for approval when they come with a token.

The REST target writes (``POST``, ``PATCH`` and ``DELETE /api/v1/targets``)
and the four REST topology graph writes follow the same rule as
``call_operation``: only a person in the web interface runs them at once.
A call made with a token (the ``meho`` CLI, a script, an AI tool that uses
a person's login) goes through the dispatcher as the matching typed
operation (``targets.*`` or ``topology.*``). The policy gate decides; for
a person's token it makes the call wait for approval, and the route
answers 202 with the id of the approval request.

The console calls the write functions in process with a web session, so
it keeps running them at once. A person approves or rejects the request in
the console, with ``meho approvals``, or on REST
(``docs/codebase/approvals.md``).
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, is_human_principal

__all__ = [
    "AWAITING_APPROVAL_RESPONSES",
    "WriteAwaitingApproval",
    "dispatch_rest_write",
    "runs_at_once",
]


class WriteAwaitingApproval(BaseModel):
    """The 202 answer of a REST write that waits for approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["awaiting_approval"] = Field(
        description="Always `awaiting_approval`: nothing is written yet."
    )
    op_id: str = Field(
        description="The operation that runs after approval, for example `targets.register`."
    )
    approval_request_id: uuid.UUID = Field(
        description=(
            "Id of the approval request. A person approves it in the console, "
            "with `meho approvals approve <id>`, or on REST."
        )
    )
    message: str = Field(description="What happens next, in plain words.")


#: The ``responses`` entry each write route adds to its OpenAPI operation.
AWAITING_APPROVAL_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_202_ACCEPTED: {
        "model": WriteAwaitingApproval,
        "description": (
            "The call came with a token, so it waits for approval. Nothing is "
            "written until a person approves the request."
        ),
    },
}

#: Error codes of the dispatcher that mean "the input is wrong".
_INVALID_INPUT_CODES: Final[frozenset[str]] = frozenset({"invalid_params"})


def runs_at_once(operator: Operator) -> bool:
    """True only for a person in the web interface.

    Only :func:`~meho_backplane.ui.auth.refresh.verify_access_token_with_refresh`
    sets ``web_session``; no token, header or body can set it.
    """
    return operator.web_session and is_human_principal(operator)


async def dispatch_rest_write(
    operator: Operator,
    *,
    connector_id: str,
    op_id: str,
    params: dict[str, Any],
) -> JSONResponse:
    """Send one REST write through the dispatcher and map the result to HTTP.

    * ``awaiting_approval`` -> 202 with :class:`WriteAwaitingApproval`.
    * ``ok`` (a rule runs the call at once) -> 200 with the result of
      the operation.
    * ``denied`` -> 403 ``denied`` with the reason of the policy gate.
    * ``error`` on bad input -> 422; any other ``error`` -> 500. The detail
      names the error code and the message.
    """
    from meho_backplane.operations.dispatcher import dispatch

    result = await dispatch(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        target=None,
        params=params,
    )
    if result.status == "awaiting_approval":
        request_id = uuid.UUID(str(result.extras["approval_request_id"]))
        body = WriteAwaitingApproval(
            status="awaiting_approval",
            op_id=op_id,
            approval_request_id=request_id,
            message=(
                f"{op_id} waits for approval (request {request_id}). It runs with "
                "these values once a person approves it in the console, with "
                f"`meho approvals approve {request_id}`, or on REST."
            ),
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content=body.model_dump(mode="json")
        )
    if result.status == "ok":
        return JSONResponse(status_code=status.HTTP_200_OK, content=result.result)
    if result.status == "denied":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "denied", "op_id": op_id, "message": result.error or "denied"},
        )
    error_code = str(result.extras.get("error_code") or result.status)
    message = str(result.extras.get("exception_message") or result.error or f"{op_id} failed")
    bad_input = (
        error_code in _INVALID_INPUT_CODES or result.extras.get("exception_class") == "ValueError"
    )
    raise HTTPException(
        status_code=(
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if bad_input
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
        detail={"error": error_code, "op_id": op_id, "message": message},
    )
