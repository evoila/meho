# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Readiness probe for the broadcast (Valkey) substrate.

Registered with :mod:`meho_backplane.health` from the FastAPI lifespan
hook (:mod:`meho_backplane.main`). The probe issues a single ``PING``
against the per-process async client; success means Valkey is
reachable.

At T1 there are no streams to consult yet (T3 lands publish-on-write),
so a server-level ``PING`` is the correct liveness check —
``XINFO STREAM`` against an empty key would always return
``-ERR no such key`` regardless of broadcast health, conflating
"reachable but no streams" with "reachable and serving".

Detail strings follow the chassis-wide redaction convention
(:mod:`meho_backplane.auth.vault`, :mod:`meho_backplane.db.migrations`):
the broadcast URL, port, and any other operator-controlled substring
never appear in the payload — those are policy-controlled inputs and
surfacing them on ``/ready`` would broaden the leak surface for
misconfigured tenants.
"""

from __future__ import annotations

import logging

from redis import exceptions as redis_exceptions

from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.health import ProbeResult

__all__ = ["broadcast_readiness_probe"]


_log = logging.getLogger(__name__)


async def broadcast_readiness_probe() -> ProbeResult:
    """Return the broadcast probe verdict.

    Issues ``PING`` against the shared async client. Four observable
    outcomes:

    * **reachable** — ``PING`` returned. ``ok=True``,
      ``detail="reachable"``.
    * **timeout** — :class:`redis.exceptions.TimeoutError`. ``ok=False``,
      ``detail="timeout"``.
    * **unreachable** — :class:`redis.exceptions.ConnectionError`
      (covers DNS, TCP, TLS, refused). ``ok=False``,
      ``detail="unreachable: <ExcClass>"``.
    * **redis_error** — any other
      :class:`redis.exceptions.RedisError` subclass (auth, server
      error). ``ok=False``, ``detail="redis_error: <ExcClass>"``.

    A defensive ``except Exception`` catches anything outside the
    redis-py hierarchy (malformed URL surfacing at command time,
    attribute errors) — same safety net the DB migration probe uses
    (see :func:`meho_backplane.db.migrations.db_migration_probe`). The
    unexpected branch logs a structured warning so operators can chase
    a probe-implementation bug rather than confusing it with a Valkey
    outage.

    Detail strings carry only the exception class name; the broadcast
    URL, port, and credentials never reach the ``/ready`` payload.
    """
    client = get_broadcast_client()
    try:
        # redis-py 8's ``redis.asyncio`` stub types ``Redis.ping`` as a
        # plain ``Awaitable[bool]`` (no longer the ``Awaitable[bool] | bool``
        # union), so awaiting it directly type-checks under strict mypy --
        # the earlier ``cast`` is now redundant.
        await client.ping()
    except redis_exceptions.TimeoutError:
        return ProbeResult(name="broadcast", ok=False, detail="timeout")
    except redis_exceptions.ConnectionError as exc:
        return ProbeResult(
            name="broadcast",
            ok=False,
            detail=f"unreachable: {type(exc).__name__}",
        )
    except redis_exceptions.RedisError as exc:
        return ProbeResult(
            name="broadcast",
            ok=False,
            detail=f"redis_error: {type(exc).__name__}",
        )
    except Exception as exc:
        _log.warning(
            "broadcast_probe_failed",
            extra={"exc_type": type(exc).__name__},
        )
        return ProbeResult(
            name="broadcast",
            ok=False,
            detail=f"check_failed: {type(exc).__name__}",
        )
    return ProbeResult(name="broadcast", ok=True, detail="reachable")
