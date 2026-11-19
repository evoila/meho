# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "uncertain" outcome: MEHO does not know if a change happened.

An approved change can end in a way MEHO cannot judge. The vendor system
may have done the work, and MEHO lost the answer. Three cases:

* The request was sent, then the answer never came (a read timeout or a
  dropped connection) on an operation that changes something. This holds
  for every kind of connection: HTTP, SSH and the other client libraries
  the typed connectors use.
* The change ran, but the audit record could not be saved.
* MEHO stopped in the middle of an approved run (a restart), so no result
  was stored.

A fourth case is the product refusing the login session after MEHO sent
the change (``connector_auth_failed``). A refused login before anything was
sent is a plain failure.

In every case, every view shows ``uncertain`` with one piece of advice: do
not run it again, check the target first. MEHO itself never runs such a
change again (``retry`` is ``False``, and an approved request can start only
once).

This module holds the words and the two small rules every path shares, so
the views, the dispatcher and the restart recovery say the same thing.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final

import aiohttp
import asyncssh
import httpx
import requests.exceptions

from meho_backplane.connectors import OperationResult

__all__ = [
    "LOGIN_ENDED_ADVICE",
    "UNCERTAIN_ADVICE",
    "UNCERTAIN_OUTCOME",
    "changes_target",
    "connection_lost_after_send",
    "envelope_is_uncertain",
    "request_may_have_reached_vendor",
    "result_interrupted_by_restart",
    "with_uncertain_outcome",
]

#: Value of ``extras["execution_outcome"]`` when MEHO does not know.
UNCERTAIN_OUTCOME: Final[str] = "uncertain"

#: The one piece of advice every view shows for an uncertain run.
UNCERTAIN_ADVICE: Final[str] = (
    "MEHO does not know if the change happened. Do not run it again. Check the target first."
)

#: The advice when the product ended the login session after MEHO sent a
#: change. It names the one safe next step and no other.
LOGIN_ENDED_ADVICE: Final[str] = (
    "The change may or may not have been made. A person must check the target "
    "first. Then make a new request."
)

#: Transport faults after which the vendor may already have the request: the
#: answer did not come, or the connection dropped while or after MEHO wrote
#: it. A connect timeout, a pool timeout and a refused connection are not
#: here: nothing was sent.
_MAY_HAVE_SENT: Final[tuple[type[httpx.TransportError], ...]] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


#: Faults that end a call before MEHO sent anything: a refused connection, a
#: connect timeout, no server to talk to. Checked before
#: :data:`_LOST_AFTER_SEND`, because some of them are subclasses of the types
#: there (``ConnectionRefusedError`` is a ``ConnectionError``, aiohttp's
#: connect timeout is a ``TimeoutError``, the ``ConnectTimeout`` of
#: ``requests`` is its ``ConnectionError`` and its ``Timeout``). The httpx
#: types count when a connector wraps one in another error (see
#: :func:`connection_lost_after_send`); a bare httpx error is judged by the
#: dispatcher's httpx arms.
_NOT_SENT: Final[tuple[type[BaseException], ...]] = (
    ConnectionRefusedError,
    aiohttp.ClientConnectorError,
    aiohttp.ConnectionTimeoutError,
    requests.exceptions.ConnectTimeout,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)

#: Faults after which the target may already hold the call: a timeout, or a
#: connection that was lost. ``TimeoutError`` is what ``asyncio.wait_for``
#: raises around an SSH command. The ``requests`` types are the faults of
#: the Vault client during a Vault change. A fault of the read of the
#: target's secret does not count: the shared loader raises its own type for
#: it (``TargetSecretReadFailedError``), and that type is a not-sent type.
_LOST_AFTER_SEND: Final[tuple[type[BaseException], ...]] = (
    TimeoutError,
    ConnectionError,
    asyncssh.ConnectionLost,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientOSError,
    aiohttp.ClientPayloadError,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    *_MAY_HAVE_SENT,
)

#: How many links of the ``__cause__`` chain :func:`connection_lost_after_send`
#: reads. Enough for a connector that wraps a fault two or three times.
_CAUSE_DEPTH: Final[int] = 8


@lru_cache(maxsize=1)
def _lazy_types() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """The not-sent and lost-after-send types that are imported when first needed.

    The database drivers are imported here, so the dispatcher does not load
    three database drivers at start. An error of a driver can only exist
    once the driver is loaded. The credential errors of MEHO are here too:
    they are raised before the call to the target, also when their cause is
    a timeout.
    """
    import asyncpg
    import pymongo.errors
    import pytds

    from meho_backplane.auth.vault import VaultClientError
    from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
    from meho_backplane.connectors._shared.vault_creds import (
        TargetSecretNotFoundError,
        TargetSecretReadFailedError,
    )

    not_sent: tuple[type[BaseException], ...] = (
        pymongo.errors.ServerSelectionTimeoutError,
        VaultClientError,
        CredentialsReadError,
        TargetSecretNotFoundError,
        TargetSecretReadFailedError,
    )
    lost: tuple[type[BaseException], ...] = (
        pymongo.errors.AutoReconnect,
        asyncpg.exceptions.ConnectionDoesNotExistError,
        pytds.TimeoutError,
        pytds.ClosedConnectionError,
    )
    return not_sent, lost


def changes_target(safety_level: str | None) -> bool:
    """Whether an operation with this risk level may change the target.

    Only ``safe`` is a read. An unknown level counts as a change, so a doubt
    ends as ``uncertain`` and never as a plain error.
    """
    return safety_level != "safe"


def request_may_have_reached_vendor(exc: httpx.TransportError) -> bool:
    """Whether the vendor may already hold the request that raised *exc*."""
    return isinstance(exc, _MAY_HAVE_SENT)


def connection_lost_after_send(exc: BaseException) -> bool:
    """Whether *exc* is a timeout or a lost connection after MEHO may have sent the call.

    For the client libraries that the dispatcher's httpx arms do not judge:
    asyncssh, aiohttp (the Kubernetes client), ``requests`` (the Vault
    client) and the database drivers. A connector may wrap such a fault in
    another error (``raise RuntimeError(...) from exc``), so the check reads
    the ``__cause__`` chain from the outside in. The first link whose type
    is on a list decides: a not-sent type (see :data:`_NOT_SENT`) is
    ``False``, a lost-after-send type is ``True``. A chain with neither is
    ``False``.
    """
    lazy_not_sent, lazy_lost = _lazy_types()
    not_sent = _NOT_SENT + lazy_not_sent
    lost = _LOST_AFTER_SEND + lazy_lost
    link: BaseException | None = exc
    for _ in range(_CAUSE_DEPTH):
        if link is None:
            break
        if isinstance(link, not_sent):
            return False
        if isinstance(link, lost):
            return True
        link = link.__cause__
    return False


def with_uncertain_outcome(result: OperationResult) -> OperationResult:
    """*result* marked as uncertain: ``execution_outcome``, ``retry=False`` and the advice."""
    extras = dict(result.extras)
    extras["execution_outcome"] = UNCERTAIN_OUTCOME
    extras["retry"] = False
    extras["remediation"] = UNCERTAIN_ADVICE
    error = f"{result.error}. {UNCERTAIN_ADVICE}" if result.error else UNCERTAIN_ADVICE
    return OperationResult.model_validate({**dict(result), "error": error, "extras": extras})


def envelope_is_uncertain(envelope: Mapping[str, Any] | None) -> bool:
    """Whether a stored result says MEHO does not know what happened."""
    if not envelope or envelope.get("status") == "ok":
        return False
    extras = envelope.get("extras")
    return isinstance(extras, Mapping) and extras.get("execution_outcome") == UNCERTAIN_OUTCOME


def result_interrupted_by_restart(op_id: str) -> OperationResult:
    """The result stored for an approved run that MEHO lost in a restart."""
    return OperationResult(
        status="error",
        op_id=op_id,
        duration_ms=0.0,
        error=(
            "interrupted_by_restart: MEHO stopped while this approved operation was "
            "running. Do not run it again. Check the target first."
        ),
        extras={
            "error_code": "interrupted_by_restart",
            "execution_outcome": UNCERTAIN_OUTCOME,
            "retry": False,
            "remediation": UNCERTAIN_ADVICE,
        },
    )
