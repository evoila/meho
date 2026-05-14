# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho://tenant/{tenant_id}/feed`` — recent broadcast events snapshot (G6.1-T6a).

A polling MCP surface onto the same per-tenant Valkey stream the SSE
endpoint at :mod:`meho_backplane.api.v1.feed` streams in real time.
The resource returns the most recent :data:`_FEED_SNAPSHOT_COUNT`
events in chronological order; MCP clients that need live updates
re-read the resource on their own cadence. v0.2 advertises no
``subscribe`` capability (per the MCP 2025-06-18 spec, omitting the
field declares no subscription support, which is the correct shape
for a poll-only resource).

Tenant-boundary enforcement
===========================

The handler validates the URI-bound ``tenant_id`` against the
operator's JWT-derived ``tenant_id`` before issuing any Valkey
command. The check runs *before* the stream read so a probe attempt
against an arbitrary tenant UUID can't time-channel-leak whether
that tenant produces events. Cross-tenant reads collapse onto
:class:`~meho_backplane.mcp.server.McpInvalidParamsError` (-32602),
matching the convention :mod:`tenant_info` established for the
``meho://tenant/{tenant_id}/info`` resource.

Why XREVRANGE + reverse instead of XRANGE
==========================================

``XREVRANGE meho:feed:{tenant_id} + - COUNT N`` reads the
most-recent N entries directly from the stream tail without
scanning the whole key. A naive ``XRANGE ... + COUNT N`` (start from
the head) would return the oldest N entries, exactly the wrong
half. The handler then reverses the result so the JSON output
reads chronologically (oldest-first), which matches operator
intuition when scanning a feed in a terminal.

Failure shapes (each maps to ``McpInvalidParamsError``)
========================================================

* **Malformed ``tenant_id``** — URI bound a non-UUID string.
* **Cross-tenant read** — bound tenant != operator's JWT tenant.
* Other failure modes (Valkey unreachable, redis-py teardown race)
  bubble up to the dispatcher as ``McpInternalError`` (-32603); the
  read path is not fail-open the way the publisher is — a failing
  resource read is a real signal to the operator, not a degraded
  feed.

Skipped entries during deserialisation
=======================================

Same safety net as the SSE generator at
:mod:`meho_backplane.api.v1.feed`: entries XADD'd with an unknown
field shape, or whose ``event`` field doesn't parse as a
:class:`BroadcastEvent`, are logged and skipped. T3's publisher is
the only writer today; this branch is forward-compat against a
future Slack-mirror / downstream tool writing alternate shapes.

References
----------

* Valkey ``XREVRANGE``: https://valkey.io/commands/xrevrange/
* MCP 2025-06-18 Resources:
  https://modelcontextprotocol.io/specification/2025-06-18/server/resources
* Sibling SSE surface: :mod:`meho_backplane.api.v1.feed` (G6.1-T4).
* Cross-repo onboarding: ``docs/cross-repo/broadcast-onboarding.md``.
"""

from __future__ import annotations

from typing import Any, Final, cast
from uuid import UUID

import structlog
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent, get_broadcast_client
from meho_backplane.mcp.registry import (
    ResourceTemplateDefinition,
    register_mcp_resource,
)
from meho_backplane.mcp.server import McpInvalidParamsError

__all__: list[str] = []

_log = structlog.get_logger(__name__)


#: How many most-recent entries the resource returns per read. Pinned
#: to 50 per the issue body ("Returns the last N events; default 50").
#: Bounded by :data:`~meho_backplane.broadcast.publisher.BROADCAST_MAXLEN`
#: (10000) — a fresh-deploy tenant with fewer than 50 events returns
#: whatever's there without padding. Operators who need a deeper window
#: subscribe via the SSE feed (T4); the MCP poll-shaped resource caps
#: at 50 to keep one ``resources/read`` response bounded in size.
_FEED_SNAPSHOT_COUNT: Final[int] = 50


def _stream_key(tenant_id: UUID) -> str:
    """Build the per-tenant stream key. Mirrors the publisher + SSE helpers."""
    return f"meho:feed:{tenant_id}"


def _parse_entry(
    entry_id: str,
    fields: dict[str, str],
    *,
    stream_key: str,
) -> BroadcastEvent | None:
    """Deserialise one stream entry; log + skip on shape / parse failure.

    Returns ``None`` rather than raising so the surrounding loop can
    drop the entry without tearing down the whole resource read. The
    skip paths match the SSE generator's at
    :func:`meho_backplane.api.v1.feed._process_entries`:

    * **Unknown field shape** — entry was XADD'd without an ``event``
      field, or with the ``event`` value as non-string. T3's publisher
      always emits ``{"event": <json>}``; this branch is the
      forward-compat safety net.
    * **Malformed JSON** — ``event`` field doesn't deserialise to a
      :class:`BroadcastEvent`. Same shape as the SSE side; logged with
      ``entry_id`` so an operator chasing the event trail can correlate.
    """
    raw_event_json = fields.get("event")
    if not isinstance(raw_event_json, str):
        _log.warning(
            "tenant_feed_skipped_unknown_field_shape",
            stream_key=stream_key,
            entry_id=entry_id,
            fields=list(fields.keys()),
        )
        return None
    try:
        return BroadcastEvent.model_validate_json(raw_event_json)
    except ValidationError:
        _log.warning(
            "tenant_feed_skipped_malformed_event",
            stream_key=stream_key,
            entry_id=entry_id,
        )
        return None


async def _tenant_feed_handler(
    operator: Operator,
    bound: dict[str, str],
) -> dict[str, Any]:
    """Return the most recent :data:`_FEED_SNAPSHOT_COUNT` events for the tenant.

    Two rejection arms mapped onto ``McpInvalidParamsError`` (-32602):
    the URI bound a non-UUID string, or the bound tenant differs from
    the operator's JWT tenant. Both run *before* the Valkey read so a
    probe attempt against an arbitrary UUID doesn't reach the stream
    layer.

    Successful response shape::

        {
            "tenant_id": "<uuid>",
            "count": <int>,        # actual events in this read; ≤ 50
            "events": [
                <BroadcastEvent.model_dump(mode="json") for each event>,
                ...
            ]
        }

    Events are in chronological order (oldest-first). An operator
    scanning the feed in a terminal reads top-to-bottom as time-forward,
    which matches the SSE-stream tail. Empty stream → ``count: 0``
    + ``events: []``; never a 404 — the resource always exists for
    every tenant.
    """
    raw_id = bound["tenant_id"]
    try:
        bound_uuid = UUID(raw_id)
    except ValueError as exc:
        raise McpInvalidParamsError(
            f"tenant_feed: invalid tenant_id (not a UUID): {raw_id!r}",
        ) from exc

    if bound_uuid != operator.tenant_id:
        raise McpInvalidParamsError(
            "tenant_feed: cross-tenant access denied — bound "
            f"tenant_id {raw_id!r} does not match the operator's tenant",
        )

    stream_key = _stream_key(bound_uuid)
    client = get_broadcast_client()
    # XREVRANGE returns newest-first; for the response we reverse to
    # chronological (oldest-first). The redis-py signature is
    # ``xrevrange(name, max='+', min='-', count=None)``: with
    # ``+``/``-`` (full range, no replay cursor), ``count`` is the
    # bound on entries returned from the tail.
    raw_entries = cast(
        "list[tuple[str, dict[str, str]]]",
        await client.xrevrange(
            stream_key,
            max="+",
            min="-",
            count=_FEED_SNAPSHOT_COUNT,
        ),
    )

    events: list[BroadcastEvent] = []
    # raw_entries is newest-first; iterate reversed to produce
    # oldest-first output.
    for entry_id, fields in reversed(raw_entries):
        event = _parse_entry(entry_id, fields, stream_key=stream_key)
        if event is not None:
            events.append(event)

    return {
        "tenant_id": str(bound_uuid),
        "count": len(events),
        "events": [event.model_dump(mode="json") for event in events],
    }


register_mcp_resource(
    definition=ResourceTemplateDefinition(
        uriTemplate="meho://tenant/{tenant_id}/feed",
        name="Tenant activity feed",
        description=(
            "Snapshot of the operator's tenant activity feed — the "
            "most recent 50 audited operations in chronological order. "
            "Cross-tenant reads return INVALID_PARAMS. Clients needing "
            "live updates use GET /api/v1/feed (Server-Sent Events) or "
            "re-read this resource on their own cadence; the MCP "
            "server advertises no subscribe capability."
        ),
        mimeType="application/json",
        required_role=TenantRole.OPERATOR,
    ),
    handler=_tenant_feed_handler,
)
