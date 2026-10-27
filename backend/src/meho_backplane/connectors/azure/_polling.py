# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Following Azure's long-running operations.

A long-running Azure call answers 201 or 202 and names a URL to poll:
``Azure-AsyncOperation`` when present, else ``Location``. ``Retry-After``
says how long to wait between polls.

* An ``Azure-AsyncOperation`` poll answers ``{"status": ...}``. The end
  states are ``Succeeded``, ``Failed`` and ``Canceled``; every other value
  means the operation still runs. ``error`` holds the reason of a failure.
* A ``Location`` poll answers 202 while the operation runs, and 200 (or 204)
  with the result when it is done. An error answer means it failed.

:func:`operation_reference` turns a 201 or 202 answer into a small
:class:`OperationReference` (poll URL, kind, resource ID, start time).
:func:`read_operation_state` polls once; ``azure.operation.status`` uses it,
so a caller can learn the outcome of a write later, for example after a
lost answer or a restart, without sending the write again.
:func:`wait_for_operation` polls until the end or until its time is up and
returns the reference with the result, so the caller can poll later.

Every poll URL is checked with
:func:`~meho_backplane.connectors.azure._http.check_azure_url` before the
token is sent to it: https, the target's host, the target's subscription, at
most 4 KB. It must also be an operation status URL (a path part with
"operation" in it, such as ``operations``, ``operationStatuses`` or
``operationResults``).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import (
    AzureConflictError,
    AzureRequestError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure._http import check_azure_url, retry_after_seconds, send
from meho_backplane.connectors.azure.session import AzureTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "DEFAULT_POLL_SECONDS",
    "END_STATES",
    "MAX_POLL_WAIT_SECONDS",
    "OperationReference",
    "OperationState",
    "operation_reference",
    "read_operation_state",
    "wait_for_operation",
]

PollKind = Literal["azure-asyncoperation", "location"]

#: The states after which an operation does not change any more.
END_STATES: Final[frozenset[str]] = frozenset({"Succeeded", "Failed", "Canceled"})

#: Wait between polls when Azure sends no ``Retry-After``.
DEFAULT_POLL_SECONDS: Final[float] = 10.0

#: Longest wait between two polls, whatever ``Retry-After`` says.
MAX_POLL_WAIT_SECONDS: Final[float] = 60.0

#: How long :func:`wait_for_operation` polls by default before it returns.
DEFAULT_WAIT_SECONDS: Final[float] = 300.0

_HEADER_FOR_KIND: Final[dict[str, str]] = {
    "azure-asyncoperation": "azure-asyncoperation",
    "location": "location",
}

#: Longest error message from Azure kept in a state.
_MESSAGE_CAP: Final[int] = 2048

_monotonic = time.monotonic
_sleep = asyncio.sleep


@dataclass(frozen=True)
class OperationReference:
    """What MEHO needs to poll an operation later."""

    poll_url: str
    poll_kind: PollKind
    resource_id: str | None
    started_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "poll_url": self.poll_url,
            "poll_kind": self.poll_kind,
            "resource_id": self.resource_id,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> OperationReference:
        """Read a reference that a caller passed back. Raises ``ValueError``."""
        if not isinstance(value, dict):
            raise ValueError("operation must be an object with poll_url and poll_kind")
        poll_url = value.get("poll_url")
        poll_kind = value.get("poll_kind")
        if not isinstance(poll_url, str) or not poll_url:
            raise ValueError("operation.poll_url must be the poll URL MEHO returned")
        if poll_kind not in _HEADER_FOR_KIND:
            raise ValueError("operation.poll_kind must be 'azure-asyncoperation' or 'location'")
        resource_id = value.get("resource_id")
        started_at = value.get("started_at")
        return cls(
            poll_url=poll_url,
            poll_kind=poll_kind,
            resource_id=resource_id if isinstance(resource_id, str) else None,
            started_at=started_at if isinstance(started_at, str) else "",
        )


@dataclass(frozen=True)
class OperationState:
    """The state of an operation after one poll."""

    status: str
    done: bool
    retry_after_seconds: float | None
    error: dict[str, str | None] | None
    result: Any

    def as_dict(self, reference: OperationReference) -> dict[str, Any]:
        return {
            "status": self.status,
            "done": self.done,
            "error": self.error,
            "result": self.result,
            "retry_after_seconds": self.retry_after_seconds,
            "operation": reference.as_dict(),
        }


def check_poll_url(target: AzureTargetLike, url: str) -> None:
    """Refuse a poll URL that breaks the URL rules or is no operation status URL."""
    parsed = check_azure_url(target, url)
    if not any("operation" in part.lower() for part in parsed.path.split("/")):
        raise AzureUrlRefusedError(
            f"Azure target {target.name!r}: the poll URL is not an Azure operation "
            "status URL. MEHO sent no token to it."
        )


def operation_reference(
    target: AzureTargetLike, response: httpx.Response, *, resource_id: str | None
) -> OperationReference | None:
    """Return the reference of a long-running operation, or ``None``.

    ``None`` means the answer is not a 201 or 202 with a poll URL: the call
    is already done. The poll URL is checked here, so a bad URL fails at
    once and is never stored.
    """
    if response.status_code not in (201, 202):
        return None
    for kind in ("azure-asyncoperation", "location"):
        url = response.headers.get(_HEADER_FOR_KIND[kind])
        if url:
            check_poll_url(target, url)
            return OperationReference(
                poll_url=url,
                poll_kind=kind,
                resource_id=resource_id,
                started_at=datetime.now(UTC).isoformat(),
            )
    return None


def _error_of(value: object) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    code, message = value.get("code"), value.get("message")
    return {
        "code": code if isinstance(code, str) else None,
        "message": message[:_MESSAGE_CAP] if isinstance(message, str) else None,
    }


async def read_operation_state(
    connector: AzureConnector,
    target: AzureTargetLike,
    reference: OperationReference,
    *,
    operator: Operator,
) -> OperationState:
    """Poll the operation once and return its state. Sends no write."""
    check_poll_url(target, reference.poll_url)
    try:
        response = await send(connector, target, "GET", reference.poll_url, operator=operator)
    except (AzureRequestError, AzureConflictError) as exc:
        if reference.poll_kind != "location":
            raise
        # A Location poll answers the operation's own failure as an error status.
        return OperationState(
            status="Failed",
            done=True,
            retry_after_seconds=None,
            error={"code": exc.azure_error_code, "message": exc.azure_message},
            result=None,
        )
    wait_hint = retry_after_seconds(response)
    body = json_payload_or_empty(response)
    if reference.poll_kind == "location":
        if response.status_code == 202:
            return OperationState("InProgress", False, wait_hint, None, None)
        return OperationState("Succeeded", True, None, None, body or None)
    raw_status = body.get("status") if isinstance(body, dict) else None
    status = raw_status if isinstance(raw_status, str) and raw_status else "InProgress"
    canonical = next((end for end in END_STATES if end.lower() == status.lower()), None)
    return OperationState(
        status=canonical or status,
        done=canonical is not None,
        retry_after_seconds=None if canonical else wait_hint,
        error=_error_of(body.get("error")) if isinstance(body, dict) else None,
        result=body.get("properties") if canonical == "Succeeded" else None,
    )


async def wait_for_operation(
    connector: AzureConnector,
    target: AzureTargetLike,
    reference: OperationReference,
    *,
    operator: Operator,
    max_seconds: float = DEFAULT_WAIT_SECONDS,
) -> dict[str, Any]:
    """Poll until the operation ends or *max_seconds* pass.

    Returns the last state with the reference. When the time is up first,
    ``done`` is ``False``; the caller can poll later with
    ``azure.operation.status`` and the returned ``operation``.
    """
    started = _monotonic()
    while True:
        state = await read_operation_state(connector, target, reference, operator=operator)
        if state.done:
            return state.as_dict(reference)
        wait = state.retry_after_seconds
        wait = DEFAULT_POLL_SECONDS if wait is None else min(wait, MAX_POLL_WAIT_SECONDS)
        if _monotonic() - started + wait > max_seconds:
            return state.as_dict(reference)
        await _sleep(wait)
