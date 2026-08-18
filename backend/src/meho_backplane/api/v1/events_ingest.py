# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Inbound event ingest endpoint ``POST /api/v1/events/ingest/{source_slug}`` (#2881).

The JWT-less webhook surface a registered ``event_source`` (#2880) delivers
to. Unlike every other ``/api/v1`` route it carries **no** operator
dependency: the sender is authenticated per-source against the source's
Vault-custodied secret, not by a Keycloak JWT, so the chassis
``AuditMiddleware`` (which keys on the JWT-bound ``operator_sub`` contextvar)
writes no row for it -- the ingest service writes its own synchronous
``__ingest__`` audit row instead.

Request path (the order is load-bearing)
=======================================

1. **Resolve** the source by slug (global, tenant-less -- the webhook carries
   no tenant). A missing *or* paused source returns the identical uniform
   ``404`` so the two are indistinguishable (no paused-vs-missing oracle).
2. **Body cap** (``413``) enforced *before* buffering the whole body -- a
   ``Content-Length`` fast-reject plus a streaming running-total guard so a
   lying/absent length header cannot bypass it (the ``ui/csrf.py`` DoS
   lesson). The cap is the source's ``extras`` override clamped to 256 KiB.
3-6. **Auth / rate-limit / dedupe / publish+audit / broadcast** run in
   :func:`~meho_backplane.events.ingest.service.accept_ingest_event`.

CSRF does not apply: ``/api/*`` is structurally outside the ``/ui/`` prefix
the CSRF middleware guards.

Reconciliation note (404 vs 401)
================================

The #2880 resolver's docstring suggests mapping a missing slug to the *same*
response as an auth failure. This endpoint instead follows the #2881 issue
contract: missing/paused -> ``404``, bad signature -> ``401``. The residual
"a 404 vs 401 distinguishes a real slug" oracle is acceptable because a slug
is a high-entropy routing token (not enumerable), and the uniform 404 still
hides the operationally sensitive paused-vs-missing distinction.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_raw_session
from meho_backplane.event_source.resolver import resolve_event_source_by_slug
from meho_backplane.event_source.schemas import EventSourceAuthStrategy, EventSourceStatus
from meho_backplane.events.ingest.auth import IngestAuthError
from meho_backplane.events.ingest.config import resolve_ingest_config
from meho_backplane.events.ingest.rate_limit import IngestRateLimitError
from meho_backplane.events.ingest.service import (
    IngestPayloadError,
    IngestVaultError,
    ResolvedSource,
    accept_ingest_event,
)

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])

#: Uniform not-found detail. Missing and paused sources return this same
#: body so the response leaks neither existence nor pause-state.
_NOT_FOUND_DETAIL = {"error": "no_event_source"}


class EventIngestResponse(BaseModel):
    """Accepted (``202``) or deduplicated (``200``) acknowledgement.

    ``event_id`` is the ``event_outbox`` row id the delivery landed on (the
    original row's id on a duplicate); ``None`` only in the rare race where a
    duplicate collided but the original row was concurrently removed.
    """

    event_id: int | None
    deduplicated: bool


class _IngestBodyTooLargeError(Exception):
    """Raised by the capped reader when the body exceeds the source's cap."""


async def _read_capped_body(request: Request, max_bytes: int) -> bytes:
    """Read the request body, rejecting oversize input before full buffering.

    A ``Content-Length`` over the cap is rejected up front; then the body is
    streamed with a running byte total so an absent or understated length
    header cannot smuggle an oversize body past the guard. At most
    ``max_bytes`` (plus one final chunk) is ever held in memory.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise _IngestBodyTooLargeError
        except ValueError:
            # A malformed Content-Length is ignored here; the streaming
            # running-total below is the real guard.
            pass

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _IngestBodyTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


def _snapshot_source(row: object) -> ResolvedSource:
    """Snapshot the resolved ORM row's routing/auth fields off the read session."""
    return ResolvedSource(
        id=row.id,  # type: ignore[attr-defined]
        tenant_id=row.tenant_id,  # type: ignore[attr-defined]
        slug=row.slug,  # type: ignore[attr-defined]
        kind=row.kind,  # type: ignore[attr-defined]
        auth_strategy=EventSourceAuthStrategy(row.auth_strategy),  # type: ignore[attr-defined]
        secret_ref=row.secret_ref,  # type: ignore[attr-defined]
        extras=dict(row.extras),  # type: ignore[attr-defined]
    )


@router.post(
    "/ingest/{source_slug}",
    response_model=EventIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_event(
    source_slug: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_raw_session),
) -> EventIngestResponse:
    """Accept one external event for *source_slug*. See the module docstring."""
    row = await resolve_event_source_by_slug(session, source_slug)
    if row is None or row.status != EventSourceStatus.ACTIVE.value:
        # Uniform 404 for both missing and paused -- no existence oracle.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

    source = _snapshot_source(row)
    config = resolve_ingest_config(source.extras)

    try:
        raw_body = await _read_capped_body(request, config.max_body_bytes)
    except _IngestBodyTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error": "payload_too_large", "max_bytes": config.max_body_bytes},
        ) from exc

    try:
        outcome = await accept_ingest_event(
            source=source, config=config, headers=request.headers, raw_body=raw_body
        )
    except IngestAuthError as exc:
        # Uniform 401; the specific reason is logged, never returned.
        _log.info("ingest_auth_rejected", slug=source.slug, reason=exc.reason)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "unauthorized"}
        ) from exc
    except IngestRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited"},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except IngestVaultError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "secret_unavailable"},
        ) from exc
    except IngestPayloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_payload"}
        ) from exc

    # A duplicate delivery is an idempotent 200, an accepted one a 202.
    response.status_code = status.HTTP_200_OK if outcome.deduplicated else status.HTTP_202_ACCEPTED
    return EventIngestResponse(event_id=outcome.event_id, deduplicated=outcome.deduplicated)
