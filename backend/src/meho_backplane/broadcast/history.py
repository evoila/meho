# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared Broadcast read primitives: filter, parse, serialise, advisory.

The Broadcast readers (``meho_broadcast_recent`` / ``meho_broadcast_watch``,
``GET /api/v1/broadcast/recent``, the SSE feeds, the UI history pane and
the MCP tenant feed resource) read the announcement archive by commit
order (:mod:`meho_backplane.broadcast.archive_reader`, #3748, #3758).
This module holds the primitives they share: :func:`event_matches`,
:func:`row_to_announcement` and :func:`dump_event_wire`.

It also holds the dispatch-time target-activity advisory and the
announce-gate claim lookup, which still scan the per-tenant Valkey
stream (:func:`parse_entry` / :func:`select_event_model`). Since #3758
only agent announcements are written to that stream.

Tenant scoping
==============

The stream key and every archive read are derived from the
:class:`Operator`'s verified ``tenant_id``. The helpers take no separate
``tenant_id`` argument; cross-tenant reads are structurally impossible.

References
----------

* Initiative: #1090 (G6.4 broadcast meta-tools).
* Valkey XREVRANGE: https://valkey.io/commands/xrevrange/
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import structlog
from pydantic import ValidationError

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent, classify_op
from meho_backplane.db.models import AgentAnnouncement
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import wrap_untrusted_text

__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "OP_CLASS_ENUM",
    "WRITE_OP_CLASSES",
    "build_target_activity_advisory",
    "caller_has_active_announce_claim",
    "dump_event_wire",
    "event_matches",
    "parse_entry",
    "row_to_announcement",
    "select_event_model",
    "stream_key",
]

_log = structlog.get_logger(__name__)

#: Default look-back window when the caller omits ``since``. 30 minutes
#: -- smaller than the 24h retention ceiling so the default response
#: stays bounded on a busy tenant. The MCP tool's input schema documents
#: this default; the UI history route does NOT use it (the UI always
#: passes an explicit retention-bound ``since``).
DEFAULT_WINDOW_MINUTES: Final[int] = 30

#: ``XREVRANGE`` upper anchor. ``"+"`` is Valkey's "latest available
#: entry" sentinel.
_XRANGE_END: Final[str] = "+"

#: Allowed ``op_class`` filter values -- exactly the classes
#: :func:`classify_op` can return, no more and no fewer
#: (``tests/test_broadcast_events.py`` pins the set equality, so a
#: class cannot be added to one side without the other). The tuple is
#: enforced, not cosmetic: :mod:`meho_backplane.mcp.handlers` validates
#: every tool call against the advertised ``inputSchema`` with
#: ``jsonschema``, so an op_class absent from here is a JSON-RPC
#: ``-32602`` before the handler runs -- the class would be published
#: on the feed but unreachable from ``meho_broadcast_recent`` /
#: ``.watch`` / ``query_audit`` (#2731 closed that gap for
#: ``credential_write`` and ``approval``).
#:
#: Two members never appear as a *persisted* ``payload["op_class"]``,
#: so a ``query_audit`` narrowed to them is always empty:
#:
#: * ``checks`` (#2720) -- the ``checks.transition`` event is published
#:   outside the audit path (there is no audited operation behind a
#:   rollup edge).
#: * ``approval`` -- no audit row persists
#:   ``payload["op_class"] == "approval"``: the approval REST routes
#:   bind ``audit_op_class`` to ``read`` / ``write``
#:   (``api/v1/approvals.py``), and the queue's own request/decision
#:   rows persist no ``op_class`` key at all. (Drawers still *display*
#:   ``approval`` for those routes' ``approval.*`` op-ids -- the
#:   read-time classifier -- but ``query_audit`` filters on the
#:   persisted key.)
#:
#: The broadcast and audit tools share this tuple as one taxonomy by
#: design (``tests/test_mcp_tools_list_shape_conventions.py`` §14.1
#: pins them together).
OP_CLASS_ENUM: Final[tuple[str, ...]] = (
    "read",
    "write",
    "credential_read",
    "credential_write",
    "credential_mint",
    "audit_query",
    "approval",
    "checks",
    "other",
)


def stream_key(tenant_id: object) -> str:
    """Build the per-tenant Valkey stream key.

    Mirrors :func:`meho_backplane.broadcast.publisher._stream_key` and
    every reader-side helper (SSE generator, UI live stream, MCP
    resource) so this module reads the same key the publisher writes.
    """
    return f"meho:feed:{tenant_id}"


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
    :class:`BroadcastEvent` (one event per audited operation; no longer
    written since #3758, but older entries may still be read) and
    the agent-authored :class:`AgentAnnouncementEvent` (one event per
    ``meho_broadcast_announce`` call, written by
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


def select_event_model(
    raw_event_json: str,
) -> type[BroadcastEvent] | type[AgentAnnouncementEvent] | None:
    """Peek the wire discriminator; pick the model class to validate against.

    Reads only the top-level ``kind`` field (falling back to the historical
    ``event_kind`` alias) via a cheap :func:`json.loads` pass, then returns
    the concrete model class the caller should full-validate the same JSON
    against:

    * ``"agent_announcement"`` → :class:`AgentAnnouncementEvent`.
    * anything else (including a missing discriminator on a pre-migration
      entry) → :class:`BroadcastEvent`, the audit-driven default.

    Returns ``None`` when *raw_event_json* is not decodable JSON, so each
    caller can emit its own surface-specific skip log
    (``broadcast_history_skipped_malformed_event`` here,
    ``feed_skipped_malformed_event`` on the SSE edge) rather than sharing
    one log name across two surfaces.

    Single-sources the discriminator rule so the XRANGE read path
    (:func:`parse_entry`) and the XREAD SSE path
    (:func:`meho_backplane.api.v1.feed._process_entries`) dispatch on the
    same field with the same fallback. BroadcastEvent's ``kind`` is an open
    ``str`` (not a :class:`typing.Literal`) and pre-migration entries omit
    it, so a pydantic tagged discriminated union is not usable; this manual
    peek is the in-tree mold both callers share.
    """
    try:
        peek = json.loads(raw_event_json)
    except json.JSONDecodeError:
        return None
    discriminator = peek.get("kind") or peek.get("event_kind") if isinstance(peek, dict) else None
    return AgentAnnouncementEvent if discriminator == "agent_announcement" else BroadcastEvent


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
      the automatic publisher per audited operation until #3758. Post-G0.16-T6 entries carry
      ``"kind": "operation"`` on the wire; pre-migration entries
      omit the field and are inferred as ``"operation"`` (the
      audit-derived majority shape — pre-migration writes carried
      ``op_id`` / ``op_class`` / ``payload``, not the
      announcement-shape ``activity`` / ``phase`` fields).
    * Agent-authored :class:`AgentAnnouncementEvent` -- written by
      :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`
      per ``meho_broadcast_announce`` call. JSON carries
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
    # G0.16-T6 Finding F (#1312). :func:`select_event_model` peeks the new
    # top-level ``kind`` discriminator, falling back to the historical
    # ``event_kind`` field for v0.8.0 in-flight stream entries that haven't
    # aged out via the publisher's ``MAXLEN ~`` trim yet. It returns ``None``
    # on undecodable JSON; the full pydantic validation runs on the chosen
    # model class below.
    model_cls = select_event_model(raw_event_json)
    if model_cls is None:
        _log.warning(
            "broadcast_history_skipped_malformed_event",
            stream_key=stream_key,
            entry_id=entry_id,
        )
        return None
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

    Every surface that re-serves events **to a model** goes through
    this helper — ``meho_broadcast_recent`` and ``meho_broadcast_watch``
    (:mod:`meho_backplane.broadcast.archive_reader`, #3748)
    and the ``meho://tenant/{tenant_id}/feed`` resource
    (:mod:`meho_backplane.mcp.resources.tenant_feed`). The UI history
    pane and the SSE feeds are *not* such surfaces: they send the plain
    ``model_dump`` (no wrap) because the browser sink escapes every
    field separately (Alpine ``x-text`` sets ``textContent``), and
    the multi-line LLM guard envelope would be human-visible noise on a
    feed row. #2549 renders announcements on that pane; the free text is
    rendered as escaped quoted prose, not fed to a model.

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


def row_to_announcement(row: AgentAnnouncement) -> AgentAnnouncementEvent:
    """Reconstruct an :class:`AgentAnnouncementEvent` from a durable row.

    Uses :meth:`pydantic.BaseModel.model_validate` (not the typed
    constructor) so the ``phase`` / ``planned_op_class`` strings coming
    off the DB validate against the model's ``Literal`` unions without a
    typed-cast dance. ``created_at`` is UTC-normalised because the SQLite
    dev/test driver returns naive datetimes; a naive ``ts`` would raise
    when :func:`_is_expired_claim` compares ``expires_at`` against the
    timezone-aware ``now``.
    """
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return AgentAnnouncementEvent.model_validate(
        {
            "event_id": row.id,
            "tenant_id": row.tenant_id,
            "principal_sub": row.principal_sub,
            "activity": row.activity,
            "target": row.target,
            "targets": list(row.targets or []),
            "scope": row.scope,
            "planned_op_class": row.planned_op_class,
            "ttl_minutes": row.ttl_minutes,
            "work_ref": row.work_ref,
            "run_id": row.run_id,
            "phase": row.phase,
            "ts": created_at,
            "claim_id": row.claim_id,
            "owner_sub": row.owner_sub,
            "actor_sub": row.actor_sub,
            "admission": row.admission,
        }
    )


#: The mutating op classes -- ``write`` / ``credential_write`` /
#: ``credential_mint``. A single source of truth for "is this a
#: write-class op" shared by the dispatch-time advisories (#2550
#: target-activity, #3133 reflex) and the #3133 announce gate, so a
#: change to the mutating-class set lands in one place. Read-class
#: dispatches (``read`` / ``credential_read`` / ``audit_query`` /
#: ``other`` / ``approval`` / ``checks``) are not members.
WRITE_OP_CLASSES: Final[frozenset[str]] = frozenset(
    {"write", "credential_write", "credential_mint"}
)

#: op_class values that carry the dispatch-time target-activity advisory
#: (#2550). Only mutating classes qualify -- a write is where crossfire
#: matters. Read-class dispatches skip the stream read entirely, keeping
#: the hot read path free of the lookup. Aliases :data:`WRITE_OP_CLASSES`
#: so the #2550 gate and the #3133 features stay in lockstep.
_ADVISORY_WRITE_OP_CLASSES: Final[frozenset[str]] = WRITE_OP_CLASSES

#: Maximum advisory entries surfaced on a single dispatch response. The
#: advisory is an awareness nudge, not an audit -- the caller who needs
#: the full picture queries ``meho_broadcast_recent``.
_ADVISORY_MAX_ENTRIES: Final[int] = 5

#: Bounded scan cap for the advisory stream read. The window (settings
#: knob) already bounds the range in time; this caps the entry count so
#: a burst inside the window can't turn one dispatch into an unbounded
#: read. The read is newest-first (``XREVRANGE``), so this cap keeps the
#: NEWEST entries in the window -- the ones the advisory cares about --
#: rather than clipping them off the tail (the busy-target crossfire case
#: an oldest-first ``XRANGE`` + ``COUNT`` would silently invert).
_ADVISORY_SCAN_LIMIT: Final[int] = 100

#: The ``extras`` key the advisory rides on the :class:`OperationResult`.
ADVISORY_EXTRAS_KEY: Final[str] = "target_activity_advisory"


def _advisory_entry(event: BroadcastEvent | AgentAnnouncementEvent) -> dict[str, Any]:
    """Project one parsed stream event onto the compact advisory shape.

    Structured, server-derived fields ONLY -- ``principal_sub`` /
    ``actor_sub`` (lineage), ``kind``, the operation's ``op_id`` or the
    announcement's ``phase``, and ``ts`` (ISO-8601). Read straight off the
    parsed model, so agent-authored free text (``activity`` / ``scope`` /
    ``target`` / ``targets``) is never even touched: the untrusted-prose
    envelope cannot enter an op response (Initiative #2543, review finding
    27). An :class:`AgentAnnouncementEvent` is labelled ``announcement``;
    a :class:`BroadcastEvent` is an ``operation``.
    """
    if isinstance(event, AgentAnnouncementEvent):
        return {
            "principal_sub": event.principal_sub,
            "kind": "announcement",
            "phase": event.phase,
            "ts": event.ts.isoformat(),
        }
    entry: dict[str, Any] = {
        "principal_sub": event.principal_sub,
        "kind": "operation",
        "op_id": event.op_id,
        "ts": event.ts.isoformat(),
    }
    if event.actor_sub is not None:
        entry["actor_sub"] = event.actor_sub
    return entry


async def _recent_target_events_newest_first(
    operator: Operator,
    *,
    target_name: str,
    window_minutes: int,
    limit: int,
) -> list[BroadcastEvent | AgentAnnouncementEvent]:
    """Read active, target-matched events in the window, newest first.

    A single ``XREVRANGE meho:feed:{tenant} + <since-ms>`` capped at
    *limit*: reverse order means the ``COUNT`` cap keeps the NEWEST
    entries in the window, so the advisory samples the tail (recent
    crossfire) rather than the head. Target and ``active_only`` filtering
    run in-process on the parsed model -- the same
    :func:`event_matches` narrowing every other reader uses, before any
    prose wrap. Propagates :class:`redis.exceptions.RedisError` to the
    caller's fail-open guard.
    """
    client = get_broadcast_client()
    key = stream_key(operator.tenant_id)
    since_ms = max(
        int((datetime.now(UTC) - timedelta(minutes=window_minutes)).timestamp() * 1000), 0
    )
    raw_entries = cast(
        "list[tuple[str, dict[str, str]]]",
        await client.xrevrange(key, max=_XRANGE_END, min=str(since_ms), count=limit),
    )
    now = datetime.now(UTC)
    events: list[BroadcastEvent | AgentAnnouncementEvent] = []
    for entry_id, fields in raw_entries:
        event = parse_entry(entry_id, fields, stream_key=key)
        if event is None:
            continue
        if event_matches(
            event, op_class=None, principal=None, target=target_name, active_only=True, now=now
        ):
            events.append(event)
    return events


async def build_target_activity_advisory(
    operator: Operator,
    *,
    op_id: str,
    target_name: str | None,
) -> dict[str, Any]:
    """Build the ``extras`` fragment advising of recent peer activity.

    Post-op awareness for a write-class dispatch (#2550): when the op
    mutates a target that another principal has touched recently -- an
    operation or an active announcement claim -- the response carries a
    compact advisory so the caller learns about the overlap at the moment
    it matters. This is NOT a lock or a block; pre-op checking remains the
    discipline's ``meho_broadcast_recent`` read step.

    Returns ``{"target_activity_advisory": [...]}`` with up to
    :data:`_ADVISORY_MAX_ENTRIES` most-recent peer entries in chronological
    order, or an empty dict (no key added) when the advisory does not
    apply. The empty-dict short-circuits, in order:

    * the feature is disabled (``dispatch_activity_advisory_window_minutes
      == 0``);
    * the op is not write-class (read-class dispatches perform no stream
      read -- the frozenset check returns before any Valkey call);
    * the dispatch has no target;
    * no peer activity was found in the window.

    Fail-open and bounded: the success path awaits a single, count-capped
    ``XREVRANGE`` (a small, bounded cost -- not free), and a broad guard
    swallows any error (a Valkey teardown included) so the advisory lookup
    never converts a successful op into a failure. Because the read is
    newest-first, the ``COUNT`` cap keeps the newest window entries, so
    ``[-_ADVISORY_MAX_ENTRIES:]`` after re-chronologising is the genuinely
    most-recent peer activity even on a target emitting more than
    :data:`_ADVISORY_SCAN_LIMIT` events in the window.
    """
    window_minutes = get_settings().dispatch_activity_advisory_window_minutes
    if window_minutes <= 0:
        return {}
    if classify_op(op_id) not in _ADVISORY_WRITE_OP_CLASSES:
        return {}
    if not target_name:
        return {}

    caller_principal = operator.sub
    caller_actor = resolve_actor_sub()
    try:
        events = await _recent_target_events_newest_first(
            operator,
            target_name=target_name,
            window_minutes=window_minutes,
            limit=_ADVISORY_SCAN_LIMIT,
        )
    except Exception:
        # Advisory is best-effort awareness; any failure (a Valkey
        # teardown, a parse bug) must never convert a successful dispatch
        # into a failure.
        _log.warning(
            "target_activity_advisory_failed",
            stream_key=stream_key(operator.tenant_id),
        )
        return {}

    # ``events`` is newest-first; drop the caller's own activity, keep the
    # newest _ADVISORY_MAX_ENTRIES, then restore chronological order.
    peers = [
        event
        for event in events
        if not (
            event.principal_sub == caller_principal
            and getattr(event, "actor_sub", None) == caller_actor
        )
    ]
    if not peers:
        return {}
    newest = peers[:_ADVISORY_MAX_ENTRIES]
    return {ADVISORY_EXTRAS_KEY: [_advisory_entry(event) for event in reversed(newest)]}


#: Lookback for the caller's own active-claim scan. Announcement TTLs are
#: bounded at 1440 minutes (``AgentAnnouncementEvent.ttl_minutes`` upper
#: bound), which is also the stream's retention ceiling, so a claim older
#: than this window can no longer be active. Paired with
#: :data:`_ADVISORY_SCAN_LIMIT` (newest-first ``COUNT`` cap), the scan is
#: one bounded round-trip; a caller who announced more than
#: :data:`_ADVISORY_SCAN_LIMIT` events ago in a very busy tenant may fall
#: off the tail (a bounded false-negative acceptable for an awareness
#: nudge, and unlikely for the announce-then-write flow the gate targets).
_CLAIM_SCAN_WINDOW_MINUTES: Final[int] = 1440


def _claim_covers_target(event: AgentAnnouncementEvent, target_name: str | None) -> bool:
    """Whether an announce claim's target attribution covers the op's target.

    A claim that named no target (neither ``target`` nor ``targets``) is
    target-agnostic and covers any op. A target-less op
    (``target_name is None``) is covered by any claim. Otherwise the claim
    must name the op's target (exact match via :func:`_target_matches`).
    """
    claim_has_target = event.target is not None or bool(event.targets)
    if not claim_has_target or target_name is None:
        return True
    return _target_matches(event, target_name)


async def caller_has_active_announce_claim(
    operator: Operator,
    *,
    op_id: str,
    target_name: str | None,
) -> bool:
    """Return ``True`` iff the caller holds an active claim covering this op.

    Reads the caller's own unexpired :class:`AgentAnnouncementEvent`
    claims off the per-tenant stream (one bounded newest-first
    ``XREVRANGE``) and returns ``True`` as soon as one covers the
    dispatched op. Shared by the #3133 reflex advisory's
    announce-before-mutate heuristic and the #3133 opt-in announce gate,
    so the two agree on what "an active claim covering this op" means.

    A claim covers the op when all hold:

    * it was authored by this principal (``principal_sub == operator.sub``
      -- announcements carry no ``actor_sub``, so a delegated agent's
      claim rides its human principal's ``sub``);
    * it has not expired (a claim with no ``ttl_minutes`` never expires);
    * its declared ``planned_op_class`` matches the op's
      :func:`classify_op` class, or the claim declared no class (a
      class-agnostic claim covers any op); and
    * its target attribution covers the op (see :func:`_claim_covers_target`).

    Propagates :class:`redis.exceptions.RedisError` to the caller's
    fail-open guard: the reflex advisory swallows it to ``{}`` and the
    announce gate swallows it to "do not block".
    """
    client = get_broadcast_client()
    key = stream_key(operator.tenant_id)
    since_ms = max(
        int((datetime.now(UTC) - timedelta(minutes=_CLAIM_SCAN_WINDOW_MINUTES)).timestamp() * 1000),
        0,
    )
    raw_entries = cast(
        "list[tuple[str, dict[str, str]]]",
        await client.xrevrange(key, max=_XRANGE_END, min=str(since_ms), count=_ADVISORY_SCAN_LIMIT),
    )
    now = datetime.now(UTC)
    op_class = classify_op(op_id)
    for entry_id, fields in raw_entries:
        event = parse_entry(entry_id, fields, stream_key=key)
        if not isinstance(event, AgentAnnouncementEvent):
            continue
        if event.principal_sub != operator.sub:
            continue
        if _is_expired_claim(event, now):
            continue
        if event.planned_op_class is not None and event.planned_op_class != op_class:
            continue
        if _claim_covers_target(event, target_name):
            return True
    return False
