# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared read-side helper for the per-tenant Valkey broadcast stream.

G6.4-T4 (#1103) factors the xrange + filter + redaction-aware parse
loop into a single implementation that both the MCP ``broadcast.recent``
tool (Task #1091) and the UI ``/ui/broadcast/history`` route (Task #869)
go through. Two callers had previously duplicated this body because
their **failure shape** differs:

* The MCP tool is **fail-loud** -- an agent calling ``broadcast.recent``
  needs a Valkey teardown to surface as a JSON-RPC ``-32603`` Internal
  Error; a silent empty result would be a correctness bug.
* The UI history route is **fail-soft** -- a transient broadcast-subchart
  blip should degrade the Last-24h pane to its empty state, not 500 the
  fragment.

The previous design solved this by writing the xrange + parse loop
twice. Every redaction-policy or schema tweak then had to land in two
places and stay in sync, which is exactly the kind of drift that
produces incidents (see PR #1097 follow-up notes).

This module factors the loop into a single core helper plus two named
wrappers, one per failure shape. New callers should pick the wrapper
whose name matches the failure contract their call site holds; the
core helper is internal because a bare call elides the contract choice.

Why two wrappers instead of ``fail_soft: bool``
================================================

A ``fail_soft: bool`` flag would compress the API to one function, but
it gates the function's error-handling semantics on a parameter -- the
"flag argument" anti-pattern Beck / Martin both flag as a code smell.
The caller's intent ("I tolerate a missing read") would live in the
*value* of an argument rather than the *name* of the function, which
hurts grep ("show me every fail-soft call site"), hurts diff review
(a one-character flip changes a load-bearing contract), and reads
ambiguously at the call site. Two named wrappers cost one extra
function definition each; that is a cheap trade for a clearer contract.

PII inheritance
===============

Every entry on ``meho:feed:{tenant_id}`` was already redacted by
:func:`~meho_backplane.broadcast.events.redact_payload` at publish time
(the publish-on-write hook in
:mod:`~meho_backplane.broadcast.publisher`). The helpers in this module
surface what's on the stream verbatim -- they do NOT re-redact, do NOT
mutate the payload field, and do NOT call ``redact_payload``. The
contract flows: publisher redacts -> stream stores redacted -> every
reader (SSE, UI history, MCP tools) surfaces redacted-as-is.

Tenant scoping
==============

The stream key is derived exclusively from the :class:`Operator`'s
verified ``tenant_id`` JWT claim. The helpers take no separate
``tenant_id`` argument; cross-tenant reads are structurally impossible.

References
----------

* Initiative: #1090 (G6.4 broadcast meta-tools).
* T1 origin (the MCP tool that first factored the helper out):
  Task #1091 / PR #1097.
* T4 unifier (this module): Task #1103.
* Sibling primitive: :mod:`meho_backplane.broadcast.publisher`
  (write-side; the fail-loud / fail-open split there mirrors the
  fail-loud / fail-soft split here).
* Sibling read surface, unmodified: the SSE feed generator at
  :func:`meho_backplane.api.v1.feed._feed_generator`. It uses XREAD
  BLOCK (open-ended stream tail), not XRANGE (finite window), so it
  shares the parse primitives in spirit but cannot reuse this body
  verbatim.
* Valkey XRANGE: https://valkey.io/commands/xrange/
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from pydantic import ValidationError
from redis.exceptions import RedisError

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.untrusted_text import wrap_untrusted_text

__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "OP_CLASS_ENUM",
    "InvalidSinceError",
    "default_since_ms",
    "dump_event_wire",
    "event_matches",
    "list_recent_events_fail_soft",
    "list_recent_events_strict",
    "parse_entry",
    "parse_since",
    "stream_key",
]

_log = structlog.get_logger(__name__)

#: Milliseconds per minute. The default ``since`` window is the last
#: :data:`DEFAULT_WINDOW_MINUTES` minutes; computed as
#: ``now_ms - DEFAULT_WINDOW_MINUTES * _MS_PER_MINUTE``.
_MS_PER_MINUTE: Final[int] = 60_000

#: Default look-back window when the caller omits ``since``. 30 minutes
#: -- smaller than the 24h retention ceiling so the default response
#: stays bounded on a busy tenant. The MCP tool's input schema documents
#: this default; the UI history route does NOT use it (the UI always
#: passes an explicit retention-bound ``since``).
DEFAULT_WINDOW_MINUTES: Final[int] = 30

#: ``XRANGE`` end anchor. ``"+"`` is Valkey's "latest available entry"
#: sentinel, so the pull spans from the ``since`` cursor to the live
#: tail. Mirrors :mod:`~meho_backplane.ui.routes.broadcast.stream` and
#: :mod:`~meho_backplane.api.v1.feed`.
_XRANGE_END: Final[str] = "+"

#: Allowed ``op_class`` filter values. The four values the issue body
#: example listed (``read|write|credential_read|audit_query``) plus
#: ``credential_mint`` and ``other`` -- :func:`classify_op` emits those
#: too. Restricting to the four would force the caller to omit the
#: filter when chasing a freshly-minted credential (e.g.
#: ``harbor.robot.create``) or an unmapped HTTP route, which defeats
#: the filter's purpose.
OP_CLASS_ENUM: Final[tuple[str, ...]] = (
    "read",
    "write",
    "credential_read",
    "credential_mint",
    "audit_query",
    "other",
)


class InvalidSinceError(ValueError):
    """Raised when :func:`parse_since` rejects the caller's input.

    Surfaced as a domain error so a wire-layer caller (the MCP handler)
    can map it to ``-32602`` Invalid Params, and a non-wire caller (the
    UI fail-soft path) can choose to swallow it. The exception body is
    a human-readable description of the offending value.
    """


def stream_key(tenant_id: object) -> str:
    """Build the per-tenant Valkey stream key.

    Mirrors :func:`meho_backplane.broadcast.publisher._stream_key` and
    every reader-side helper (SSE generator, UI live stream, MCP
    resource) so this module reads the same key the publisher writes.
    """
    return f"meho:feed:{tenant_id}"


def default_since_ms() -> int:
    """Return the inclusive bare-millisecond cursor for the default window.

    Default window is :data:`DEFAULT_WINDOW_MINUTES` minutes before
    ``now``. A bare ms timestamp as the ``XRANGE`` ``min`` auto-completes
    the sequence to ``0`` (Valkey semantics), so the range is inclusive
    of every entry from that millisecond onward. Clamped at ``0`` so a
    clock skew producing a negative start never reaches Valkey.
    """
    now_ms = int(time.time() * 1000)
    return max(now_ms - DEFAULT_WINDOW_MINUTES * _MS_PER_MINUTE, 0)


def parse_since(raw: str | None) -> str:
    """Translate the caller-supplied ``since`` into an ``XRANGE`` ``min``.

    Three shapes accepted:

    * ``None`` -- the default :data:`DEFAULT_WINDOW_MINUTES`-minute
      window. Returns the bare millisecond timestamp for ``now -
      DEFAULT_WINDOW_MINUTES`` (inclusive lower bound; Valkey
      auto-completes to sequence ``0``).
    * A Valkey stream cursor (``"<ms>-<seq>"``) -- when the caller is
      paginating a previous response's ``next_cursor``. Returned as
      ``"(<cursor>"`` to make the lower bound EXCLUSIVE, the Valkey-
      blessed pagination shape (https://valkey.io/commands/xrange/
      under "Iterating a stream"). Without the ``(``, the next call
      would re-fetch the last event of the previous page.
    * An ISO-8601 timestamp (``"2026-05-25T10:00:00Z"`` or any value
      :meth:`datetime.fromisoformat` accepts on 3.13) -- converted to
      bare milliseconds (inclusive). The caller's first wall-clock
      query uses this shape; subsequent paginated calls use the
      cursor.

    A trailing ``Z`` is normalised to ``+00:00`` for
    :meth:`datetime.fromisoformat` (the stdlib parser accepts the
    ``Z`` suffix only from 3.11 onwards; the explicit replace makes
    the contract version-independent and reads consistently in tests).

    Invalid inputs raise :class:`InvalidSinceError`.
    """
    if raw is None:
        return str(default_since_ms())
    if _looks_like_stream_cursor(raw):
        return f"({raw}"
    return _iso8601_to_min_cursor(raw)


def _looks_like_stream_cursor(raw: str) -> bool:
    """Match the Valkey stream entry id shape ``"<ms>-<seq>"``.

    A stream id is always two integers joined by ``-``. ISO-8601
    timestamps contain ``T`` / ``:`` / ``Z`` / ``+`` characters that
    a cursor never carries, so the discriminant is a simple
    ``<digits>-<digits>`` pattern. Loose enough to accept the
    bare-ms form when a caller copy-pastes one back, strict enough
    to reject any ISO-8601 shape.
    """
    if "-" not in raw:
        return False
    ms_part, sep, seq_part = raw.partition("-")
    return bool(sep) and ms_part.isdigit() and seq_part.isdigit()


def _iso8601_to_min_cursor(raw: str) -> str:
    """Convert an ISO-8601 timestamp string to a bare-ms inclusive cursor."""
    iso_normalised = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(iso_normalised)
    except ValueError as exc:
        raise InvalidSinceError(
            f"since: not a valid ISO-8601 timestamp or Valkey stream cursor: {raw!r}",
        ) from exc
    if parsed.tzinfo is None:
        # Treat a naive timestamp as UTC explicitly; otherwise
        # ``.timestamp()`` would interpret it in the worker's local
        # timezone and produce off-by-hours window starts.
        parsed = parsed.replace(tzinfo=UTC)
    return str(int(parsed.timestamp() * 1000))


def _target_matches(event: BroadcastEvent | AgentAnnouncementEvent, target: str) -> bool:
    """Exact-match the ``target`` filter against an event's attribution.

    :class:`BroadcastEvent` carries a single ``target_name``.
    :class:`AgentAnnouncementEvent` carries a single ``target`` AND a
    ``targets`` list (which supersedes-not-replaces the single field);
    a query matches when it equals ``target`` OR appears in ``targets``.
    Events with no target attribution never qualify.
    """
    if isinstance(event, BroadcastEvent):
        return event.target_name == target
    return event.target == target or target in event.targets


def _work_ref_matches(event: BroadcastEvent | AgentAnnouncementEvent, work_ref: str) -> bool:
    """Exact-match the ``work_ref`` filter against either event kind.

    Two event kinds now carry a ``work_ref``: an
    :class:`AgentAnnouncementEvent`'s declared change-ticket reference
    (T1 #2544) and a :class:`BroadcastEvent`'s projected lineage
    ``work_ref`` (T3 #2545, read off the publish-site contextvar). A
    single ``work_ref`` filter matches whichever kind carries the ref,
    so a query for one ticket surfaces both the agent's declared intent
    and the operations it drove. Events with an unset ``work_ref`` never
    qualify (``None != <filter>``).
    """
    return event.work_ref == work_ref


def _is_expired_claim(
    event: BroadcastEvent | AgentAnnouncementEvent,
    now: datetime,
) -> bool:
    """Return ``True`` iff *event* is a TTL'd claim whose expiry has passed.

    Only :class:`AgentAnnouncementEvent` with a bound ``ttl_minutes``
    can expire. Everything else -- audit-driven :class:`BroadcastEvent`
    rows and announcements without a TTL -- is never "expired" and so
    always survives an ``active_only`` filter.
    """
    if not isinstance(event, AgentAnnouncementEvent):
        return False
    expires_at = event.expires_at
    return expires_at is not None and expires_at <= now


def event_matches(
    event: BroadcastEvent | AgentAnnouncementEvent,
    *,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    actor_sub: str | None = None,
    work_ref: str | None = None,
    active_only: bool = False,
    now: datetime | None = None,
) -> bool:
    """Return ``True`` iff *event* matches every active filter.

    Exact-match semantics on each non-``None`` field; mirrors
    :func:`meho_backplane.api.v1.feed._passes_filter`. All matching runs
    on the raw model *before* :func:`dump_event_wire` wraps agent prose,
    so the untrusted envelope never affects narrowing.

    Two event classes share the broadcast stream: the audit-driven
    :class:`BroadcastEvent` (one event per audited operation, written
    by :func:`meho_backplane.broadcast.publisher.publish_event`) and
    the agent-authored :class:`AgentAnnouncementEvent` (one event per
    ``meho.broadcast.announce`` call, written by
    :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`).
    They share ``principal_sub`` but diverge on the other filters:

    * **op_class filter:** Only audit-driven events carry an
      ``op_class``. Announcements have no operation classification, so
      an announcement never qualifies regardless of its content.
    * **target filter:** matches a :class:`BroadcastEvent`'s
      ``target_name`` or an :class:`AgentAnnouncementEvent`'s ``target``
      / any of its ``targets`` (see :func:`_target_matches`).
    * **actor_sub filter:** the RFC 8693 actor projected onto
      :class:`BroadcastEvent` (T3 #2545) -- answers "what has this
      delegated agent been doing", distinct from ``principal`` (the
      human/subject the agent acted for). Audit-driven events only; an
      announcement never qualifies, same rule as ``op_class``.
    * **work_ref filter:** matches any event tied to that external
      change ticket -- an :class:`AgentAnnouncementEvent`'s declared
      ``work_ref`` (T1 #2544) OR a :class:`BroadcastEvent`'s projected
      lineage ``work_ref`` (T3 #2545) (see :func:`_work_ref_matches`).
    * **active_only:** when ``True``, drops TTL'd announcement claims
      whose ``expires_at`` (``ts + ttl_minutes``) is at or before
      ``now`` (defaulting to the current UTC instant). Non-TTL events
      always pass (see :func:`_is_expired_claim`).
    """
    if op_class is not None:
        # AgentAnnouncementEvent has no ``op_class``; the caller asked
        # for an operation classification, an announcement isn't one.
        if isinstance(event, AgentAnnouncementEvent):
            return False
        if event.op_class != op_class:
            return False
    if principal is not None and event.principal_sub != principal:
        return False
    if target is not None and not _target_matches(event, target):
        return False
    if actor_sub is not None:
        # Lineage is audit-driven only; an announcement carries no actor.
        if isinstance(event, AgentAnnouncementEvent):
            return False
        if event.actor_sub != actor_sub:
            return False
    if work_ref is not None and not _work_ref_matches(event, work_ref):
        return False
    return not (active_only and _is_expired_claim(event, now or datetime.now(UTC)))


def parse_entry(
    entry_id: str,
    fields: dict[str, str],
    *,
    stream_key: str,
) -> BroadcastEvent | AgentAnnouncementEvent | None:
    """Deserialise one ``XRANGE`` entry; log + skip on shape failure.

    Mirrors the safety net in
    :func:`meho_backplane.mcp.resources.tenant_feed._parse_entry` and
    :func:`meho_backplane.api.v1.feed._process_entries`. The publisher
    always emits ``{"event": <json>}`` today; this branch is the
    forward-compat guard against a future Slack-mirror or third-party
    writer XADD'ing a different shape onto the same key.

    Two event kinds live on the per-tenant stream (G6.4-T2 #1092):

    * Audit-driven :class:`BroadcastEvent` -- written by
      :func:`~meho_backplane.broadcast.publisher.publish_event` per
      audited operation. Post-G0.16-T6 entries carry
      ``"kind": "operation"`` on the wire; pre-migration entries
      omit the field and are inferred as ``"operation"`` (the
      audit-derived majority shape — pre-migration writes carried
      ``op_id`` / ``op_class`` / ``payload``, not the
      announcement-shape ``activity`` / ``phase`` fields).
    * Agent-authored :class:`AgentAnnouncementEvent` -- written by
      :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`
      per ``meho.broadcast.announce`` call. JSON carries
      ``"kind": "agent_announcement"`` (the new top-level discriminator
      per :doc:`/docs/codebase/api-shape-conventions` §6) and also
      ``"event_kind": "agent_announcement"`` (backward-compat alias).

    Dispatch is on the top-level ``kind`` field first
    (G0.16-T6 Finding F #1312), falling back to ``event_kind`` for
    backward compatibility with v0.8.0 stream entries that lack the
    new field. Present and matching ``"agent_announcement"`` →
    :class:`AgentAnnouncementEvent`, everything else →
    :class:`BroadcastEvent` (the operation default). A one-off
    pre-parse via :func:`json.loads` reads only the discriminator;
    the chosen model class re-parses the same JSON via
    :meth:`pydantic.BaseModel.model_validate_json` so validation
    stays full-fidelity.
    """
    raw_event_json = fields.get("event")
    if not isinstance(raw_event_json, str):
        _log.warning(
            "broadcast_history_skipped_unknown_field_shape",
            stream_key=stream_key,
            entry_id=entry_id,
            fields=list(fields.keys()),
        )
        return None
    try:
        # Cheap discriminator peek -- json.loads on a short string is
        # microseconds; an XRANGE page of ``limit <= 1000`` events
        # incurs at most one extra parse pass per entry. The full
        # pydantic validation runs on the chosen model class below.
        peek = json.loads(raw_event_json)
    except json.JSONDecodeError:
        _log.warning(
            "broadcast_history_skipped_malformed_event",
            stream_key=stream_key,
            entry_id=entry_id,
        )
        return None
    # G0.16-T6 Finding F (#1312). Prefer the new top-level ``kind``
    # discriminator; fall back to the historical ``event_kind`` field
    # for v0.8.0 in-flight stream entries that haven't aged out via
    # the publisher's ``MAXLEN ~`` trim yet.
    discriminator = peek.get("kind") or peek.get("event_kind") if isinstance(peek, dict) else None
    model_cls: type[BroadcastEvent] | type[AgentAnnouncementEvent] = (
        AgentAnnouncementEvent if discriminator == "agent_announcement" else BroadcastEvent
    )
    try:
        return model_cls.model_validate_json(raw_event_json)
    except ValidationError:
        _log.warning(
            "broadcast_history_skipped_malformed_event",
            stream_key=stream_key,
            entry_id=entry_id,
        )
        return None


#: Scalar announcement fields that carry agent-authored free text. Kept
#: in one place so every LLM-facing dump site wraps the same set; the
#: audit-driven :class:`BroadcastEvent` has no counterpart (its
#: ``payload`` is server-derived and redacted upstream, its op fields
#: are chassis-classified — none are agent prose). The structured
#: coordination fields (``planned_op_class`` / ``ttl_minutes`` /
#: ``run_id`` / ``phase`` / ``ts`` / ``expires_at``) are deliberately
#: absent: they are server-validated typed values that cannot carry an
#: injection, so they are served UNWRAPPED (see
#: :mod:`meho_backplane.broadcast.agent_events`).
_ANNOUNCEMENT_UNTRUSTED_FIELDS: Final[tuple[str, ...]] = (
    "activity",
    "scope",
    "target",
    "work_ref",
)

#: List-valued announcement fields whose *elements* carry agent-authored
#: free text. Wrapped per-element on display (the value is a
#: ``list[str]``, not a scalar, so it needs its own dump branch).
_ANNOUNCEMENT_UNTRUSTED_LIST_FIELDS: Final[tuple[str, ...]] = ("targets",)


def dump_event_wire(
    event: BroadcastEvent | AgentAnnouncementEvent,
) -> dict[str, Any]:
    """Serialise *event* for an LLM-facing response, guarding agent prose.

    ``model_dump(mode="json")`` plus the stored-prompt-injection guard
    (evoila-bosnia/meho-internal#154): on an agent-authored
    :class:`AgentAnnouncementEvent`, each free-text field
    (:data:`_ANNOUNCEMENT_UNTRUSTED_FIELDS`) is wrapped via
    :func:`~meho_backplane.untrusted_text.wrap_untrusted_text` so a
    reading agent sees the text delimited as untrusted data rather
    than as bare context it might absorb as instructions. Audit-driven
    :class:`BroadcastEvent` dumps pass through unchanged.

    Every surface that re-serves stream events to a model goes through
    this helper — ``meho.broadcast.recent`` (via
    :func:`_list_recent_events_core`), ``meho.broadcast.watch``
    (:func:`meho_backplane.mcp.tools.broadcast._filter_xread_items`)
    and the ``meho://tenant/{tenant_id}/feed`` resource
    (:mod:`meho_backplane.mcp.resources.tenant_feed`). The UI history
    pane is *not* such a surface: it consumes
    :func:`list_recent_events_fail_soft` but drops announcement-kind
    events before rendering (``_is_audit_event``), and its HTML sink
    escapes content separately.

    Filtering (:func:`event_matches`) runs on the *model*, before this
    dump, so ``target`` equality matching is unaffected by the wrap.
    """
    data = event.model_dump(mode="json")
    if isinstance(event, AgentAnnouncementEvent):
        for field in _ANNOUNCEMENT_UNTRUSTED_FIELDS:
            value = data.get(field)
            if isinstance(value, str):
                data[field] = wrap_untrusted_text(value)
        for field in _ANNOUNCEMENT_UNTRUSTED_LIST_FIELDS:
            value = data.get(field)
            if isinstance(value, list):
                data[field] = [
                    wrap_untrusted_text(item) if isinstance(item, str) else item for item in value
                ]
    return data


async def _list_recent_events_core(
    operator: Operator,
    *,
    since: str | None,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    actor_sub: str | None,
    work_ref: str | None,
    active_only: bool,
    limit: int,
) -> dict[str, Any]:
    """Read recent events for *operator*'s tenant; the shared core body.

    Internal -- callers pick :func:`list_recent_events_strict` or
    :func:`list_recent_events_fail_soft` so the failure-shape choice
    lives in the function name rather than a flag argument.

    Issues a single ``XRANGE`` over ``meho:feed:{operator.tenant_id}``
    from the resolved ``since`` lower bound to ``"+"`` (latest),
    capped at ``limit``. Filters the result in-process (the filter
    surface area is small; per-event server-side filtering would
    require a Lua script and isn't worth the complexity for ``limit
    <= 1000``).

    Returns ``{"events": [<event dict with id + cursor>, ...],
    "next_cursor": <str or None>}``. Each event dict carries the
    entry's Valkey stream id twice: as ``cursor`` (self-labelled to
    match the tool-input arg it round-trips through, #2479) and as
    ``id`` (the historical alias). ``next_cursor`` is the **stream
    entry id of the last fetched entry** (NOT the last *matched* one)
    -- the caller
    can pass it back as ``since`` to walk forward without overlap or
    gaps even when the filter dropped every entry in this page. The
    cursor is ``None`` when the stream returned fewer entries than
    ``limit`` (caller has reached the live tail).

    Raises :class:`InvalidSinceError` on a malformed ``since`` and
    propagates :class:`redis.exceptions.RedisError` on Valkey teardown.
    The two wrappers around this core decide which subset of those to
    re-raise.
    """
    client = get_broadcast_client()
    key = stream_key(operator.tenant_id)
    min_cursor = parse_since(since)
    raw_entries = cast(
        "list[tuple[str, dict[str, str]]]",
        await client.xrange(
            key,
            min=min_cursor,
            max=_XRANGE_END,
            count=limit,
        ),
    )

    # Pin one ``now`` for the whole page so a slow XRANGE parse can't
    # let an ``active_only`` claim flip expiry-state mid-page.
    now = datetime.now(UTC)
    matched: list[dict[str, Any]] = []
    for entry_id, fields in raw_entries:
        event = parse_entry(entry_id, fields, stream_key=key)
        if event is None:
            continue
        if not event_matches(
            event,
            op_class=op_class,
            principal=principal,
            target=target,
            actor_sub=actor_sub,
            work_ref=work_ref,
            active_only=active_only,
            now=now,
        ):
            continue
        # ``cursor`` is the Valkey stream entry id, self-labelled to
        # match the ``cursor`` input arg it round-trips through
        # (#2479); ``id`` is the historical alias of the same value —
        # kept because every other MCP surface uses ``id`` for the
        # row's domain UUID and broadcast rows predate that
        # convention. The remaining fields come from the event model
        # (including ``event_id`` as the durable UUID for BroadcastEvent
        # or the announcement-id equivalent for AgentAnnouncementEvent).
        # All coexist so the caller can correlate a stream-cursor walk
        # with the canonical audit row via ``event_id`` / ``audit_id``.
        # ``dump_event_wire`` wraps announcement free-text in the
        # untrusted-content envelope (stored-prompt-injection guard).
        matched.append({"id": entry_id, "cursor": entry_id, **dump_event_wire(event)})

    # next_cursor pages forward from the LAST FETCHED entry, not the
    # last matched one -- a page where every entry was filtered out
    # still produces a non-null cursor so the caller can keep walking.
    # When fewer entries came back than the limit, the caller has
    # reached the live tail; ``None`` signals "no more pages right now"
    # without precluding a later call.
    next_cursor: str | None = raw_entries[-1][0] if len(raw_entries) == limit else None
    return {"events": matched, "next_cursor": next_cursor}


async def list_recent_events_strict(
    operator: Operator,
    *,
    since: str | None = None,
    op_class: str | None = None,
    principal: str | None = None,
    target: str | None = None,
    actor_sub: str | None = None,
    work_ref: str | None = None,
    active_only: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Fail-loud variant: re-raises any :class:`RedisError` from Valkey.

    Use at call sites that need to know about Valkey teardowns -- e.g.
    the MCP ``broadcast.recent`` tool, where a silent empty result
    would mislead the calling agent into thinking the stream is empty.
    The MCP dispatcher's generic error handler maps the propagated
    exception to JSON-RPC ``-32603`` Internal Error.

    A malformed ``since`` argument surfaces as
    :class:`InvalidSinceError`; wire-layer callers map that to
    ``-32602`` Invalid Params.

    ``actor_sub`` narrows to a delegated agent's own operations (T3
    #2545); ``work_ref`` narrows to any event -- an announcement claim
    or an audit-driven operation -- linked to that opaque change-ticket
    reference; ``active_only`` drops TTL'd claims whose ``expires_at``
    has already elapsed (see :func:`event_matches`).

    Mirrors the fail-loud contract on
    :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`
    (its write-side counterpart).
    """
    return await _list_recent_events_core(
        operator,
        since=since,
        op_class=op_class,
        principal=principal,
        target=target,
        actor_sub=actor_sub,
        work_ref=work_ref,
        active_only=active_only,
        limit=limit,
    )


async def list_recent_events_fail_soft(
    operator: Operator,
    *,
    since: str | None = None,
    op_class: str | None = None,
    principal: str | None = None,
    target: str | None = None,
    actor_sub: str | None = None,
    work_ref: str | None = None,
    active_only: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Fail-soft variant: a :class:`RedisError` returns the empty result.

    On a Valkey teardown the call returns
    ``{"events": [], "next_cursor": None}`` rather than raising, so a
    caller that surfaces a user-facing pane (the UI history fragment,
    a status dashboard) degrades to the empty state rather than 500-ing
    the response. The error class is logged with a structured warning so
    operators see the teardown in the log; the redis-py exception's
    string form is NOT logged because redis-py exceptions can embed the
    endpoint URL.

    An :class:`InvalidSinceError` is treated as a bug, not a Valkey
    teardown, and is **not** caught here. Fail-soft is "the data store
    is unavailable", not "the caller passed garbage" -- the latter is a
    programming error the caller should surface.

    Mirrors the fail-open contract on
    :func:`~meho_backplane.broadcast.publisher.publish_event` (its
    write-side counterpart for non-load-bearing publishes).
    """
    try:
        return await _list_recent_events_core(
            operator,
            since=since,
            op_class=op_class,
            principal=principal,
            target=target,
            actor_sub=actor_sub,
            work_ref=work_ref,
            active_only=active_only,
            limit=limit,
        )
    except RedisError as exc:
        _log.warning(
            "broadcast_history_fetch_failed",
            error_class=type(exc).__name__,
            stream_key=stream_key(operator.tenant_id),
        )
        return {"events": [], "next_cursor": None}
