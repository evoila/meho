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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import structlog
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import select

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.broadcast.events import BroadcastEvent, classify_op
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import wrap_untrusted_text

__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "OP_CLASS_ENUM",
    "InvalidSinceError",
    "build_target_activity_advisory",
    "default_since_ms",
    "dump_event_wire",
    "event_matches",
    "list_recent_events_fail_soft",
    "list_recent_events_strict",
    "parse_entry",
    "parse_since",
    "select_event_model",
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
        # The wire-facing label names ``cursor`` -- the canonical MCP
        # forward-cursor parameter (#1358) -- not the internal ``since``
        # arg name, so the -32602 the sole wire consumer (the MCP
        # ``broadcast.recent`` handler) re-raises points callers at the
        # inputSchema property they actually passed.
        raise InvalidSinceError(
            f"cursor: not a valid ISO-8601 timestamp or Valkey stream cursor: {raw!r}",
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

    Every surface that re-serves stream events **to a model** goes through
    this helper — ``meho.broadcast.recent`` (via
    :func:`list_recent_events_strict`), ``meho.broadcast.watch``
    (:func:`meho_backplane.mcp.tools.broadcast._filter_xread_items`)
    and the ``meho://tenant/{tenant_id}/feed`` resource
    (:mod:`meho_backplane.mcp.resources.tenant_feed`). The UI history
    pane is *not* such a surface: it consumes
    :func:`list_recent_events_fail_soft`, which serialises via
    :func:`_dump_event_plain` (no wrap) because the browser sink escapes
    every field separately (Alpine ``x-text`` sets ``textContent``), and
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


def _backfill_window_start(since: str | None) -> datetime | None:
    """Resolve the window-start instant a DB archive backfill would cover.

    Broadcast v2 T2 (#2547). ``meho.broadcast.recent`` backfills durable
    :class:`~meho_backplane.db.models.AgentAnnouncement` rows when the
    caller's requested window reaches back before the stream's oldest
    surviving entry. That only makes sense for a wall-clock window:

    * ``None`` -- the default :data:`DEFAULT_WINDOW_MINUTES`-minute
      look-back; the archive may hold announcements the stream has since
      trimmed. Returns ``now - DEFAULT_WINDOW_MINUTES``.
    * An ISO-8601 timestamp -- the caller named an explicit wall-clock
      start; returns that instant (UTC-normalised).
    * A Valkey stream cursor -- the caller is paginating forward from a
      known point in the *stream*; archive backfill would break the
      cursor contract, so returns ``None`` (no backfill).

    A malformed timestamp returns ``None`` -- the stream read path's
    :func:`parse_since` surfaces the same input as an
    :class:`InvalidSinceError`, so backfill silently declining here never
    masks that error.
    """
    if since is None:
        return datetime.now(UTC) - timedelta(minutes=DEFAULT_WINDOW_MINUTES)
    if _looks_like_stream_cursor(since):
        return None
    iso_normalised = since.replace("Z", "+00:00") if since.endswith("Z") else since
    try:
        parsed = datetime.fromisoformat(iso_normalised)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _stream_oldest_dt(raw_entries: list[tuple[str, dict[str, str]]]) -> datetime | None:
    """Return the wall-clock instant of the oldest fetched stream entry.

    ``XRANGE`` from the window's lower bound returns entries ascending by
    id, so ``raw_entries[0]`` is the oldest entry at or after the window
    start. Its ``<ms>-<seq>`` id gives the millisecond timestamp; the
    backfill covers the archive strictly *before* it. ``None`` when the
    page is empty (the whole window is a candidate for the archive).
    """
    if not raw_entries:
        return None
    ms_part, _, _ = raw_entries[0][0].partition("-")
    if not ms_part.isdigit():
        return None
    return datetime.fromtimestamp(int(ms_part) / 1000, UTC)


def _row_to_announcement(row: AgentAnnouncement) -> AgentAnnouncementEvent:
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
        }
    )


async def _backfill_recent_from_db(
    operator: Operator,
    *,
    since: str | None,
    oldest_stream_dt: datetime | None,
    principal: str | None,
    target: str | None,
    op_class: str | None,
    actor_sub: str | None,
    work_ref: str | None,
    active_only: bool,
    seen_event_ids: set[str],
    budget: int,
    now: datetime,
    serialize: Callable[[BroadcastEvent | AgentAnnouncementEvent], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read durable announcements older than the stream's oldest entry.

    Broadcast v2 T2 (#2547). The stream is the hot path; this table is the
    archive. When the window reaches before the stream's oldest surviving
    entry (or the stream is empty after a Valkey restart), the
    announcements that lived in that gap are read back from
    :class:`~meho_backplane.db.models.AgentAnnouncement` so a restart or
    the ``MAXLEN ~`` trim never erases the coordination window.

    Returns per-surface-serialised dicts (oldest-first, so they slot in
    *before* the stream rows). Only announcements live here, so an
    ``op_class`` / ``actor_sub`` filter short-circuits to ``[]``. Rows
    already on the stream page are skipped by ``event_id`` (no
    double-count). Best-effort: any DB error degrades to stream-only
    results (logged) -- the archive augments the stream, never gates it.
    """
    window_start = _backfill_window_start(since)
    if window_start is None or budget <= 0:
        return []
    # Announcements never carry an op-class or an RFC 8693 actor, so a
    # query narrowed to either can never match an archived row.
    if op_class is not None or actor_sub is not None:
        return []
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(AgentAnnouncement)
                .where(
                    AgentAnnouncement.tenant_id == operator.tenant_id,
                    AgentAnnouncement.created_at >= window_start,
                )
                .order_by(AgentAnnouncement.created_at.desc())
                .limit(budget)
            )
            if oldest_stream_dt is not None:
                # Strictly before the stream's oldest entry -- the stream
                # owns everything from that point on.
                stmt = stmt.where(AgentAnnouncement.created_at < oldest_stream_dt)
            if principal is not None:
                stmt = stmt.where(AgentAnnouncement.principal_sub == principal)
            if work_ref is not None:
                stmt = stmt.where(AgentAnnouncement.work_ref == work_ref)
            rows = list((await session.execute(stmt)).scalars().all())
    except Exception as exc:
        # Best-effort: ANY failure reaching the archive (query
        # SQLAlchemyError, or engine/session setup e.g. get_sessionmaker ->
        # get_engine -> get_settings without full config) degrades to
        # stream-only results. The guard is deliberately broad; the try
        # body is scoped to DB access only, so it never gates the read.
        _log.warning(
            "broadcast_recent_backfill_failed",
            error_class=type(exc).__name__,
            tenant_id=str(operator.tenant_id),
        )
        return []

    backfilled: list[dict[str, Any]] = []
    # Query is newest-first (for the budget cap); present oldest-first so
    # the rows precede the stream page in ascending order.
    for row in reversed(rows):
        if str(row.id) in seen_event_ids:
            continue
        event = _row_to_announcement(row)
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
        # No Valkey stream id for a DB-sourced row; ``cursor`` / ``id`` are
        # null (the durable ``event_id`` UUID is the stable identity). The
        # caller's per-surface ``serialize`` (wrap for LLM reads, plain for
        # the UI pane) is applied so archive rows honour the same
        # untrusted-text contract as stream rows.
        backfilled.append({"id": None, "cursor": None, **serialize(event)})
    return backfilled


def _dump_event_plain(
    event: BroadcastEvent | AgentAnnouncementEvent,
) -> dict[str, Any]:
    """Serialise *event* for a non-LLM surface — no untrusted-text wrap.

    The UI history pane (:func:`list_recent_events_fail_soft`) renders
    every field through an HTML sink that escapes it (Alpine ``x-text``
    sets ``textContent``), so the stored-prompt-injection guard envelope
    :func:`dump_event_wire` applies for model-facing surfaces is neither
    needed nor wanted here — it would surface the multi-line guard block
    as literal row text. The counterpart split to :func:`dump_event_wire`:
    LLM-facing reads wrap agent prose, human-facing reads escape it at the
    sink and show the raw value.
    """
    return event.model_dump(mode="json")


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
    serialize: Callable[[BroadcastEvent | AgentAnnouncementEvent], dict[str, Any]],
) -> dict[str, Any]:
    """Read recent events for *operator*'s tenant; the shared core body.

    Internal -- callers pick :func:`list_recent_events_strict` or
    :func:`list_recent_events_fail_soft` so the failure-shape choice
    lives in the function name rather than a flag argument. Issues one
    ``XRANGE`` (capped at ``limit``) over ``meho:feed:{operator.tenant_id}``
    from the resolved ``since`` lower bound to ``"+"``, filters +
    serialises it in-process (:func:`_match_and_serialize_entries`), then
    archive-backfills from the DB (:func:`_backfill_recent_from_db`, T2
    #2547).

    *serialize* is each wrapper's per-surface serialiser, applied to both
    stream and backfill rows: :func:`dump_event_wire` (strict / LLM reads,
    wraps agent prose in the stored-prompt-injection guard) or
    :func:`_dump_event_plain` (fail-soft / UI pane, whose HTML sink escapes
    separately). Returns ``{"events": [...], "next_cursor": <str|None>}``;
    stream rows carry the entry id as both ``cursor`` (round-trips as the
    tool's ``cursor`` arg, #2479) and ``id``, backfill rows carry both as
    ``None``. ``next_cursor`` is the last *fetched* stream entry id (not
    the last matched -- a filter-heavy page still advances), ``None`` at
    the live tail; it stays stream-anchored (deep DB-only pagination is
    out of scope).

    Raises :class:`InvalidSinceError` on a malformed ``since`` and
    propagates :class:`redis.exceptions.RedisError` on Valkey teardown;
    the two wrappers decide which to re-raise.
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

    matched = _match_and_serialize_entries(
        raw_entries,
        stream_key=key,
        op_class=op_class,
        principal=principal,
        target=target,
        actor_sub=actor_sub,
        work_ref=work_ref,
        active_only=active_only,
        serialize=serialize,
    )

    # Archive backfill (T2 #2547): when the window reaches before the
    # stream's oldest surviving entry (or the stream is empty after a
    # Valkey restart), read the announcements that lived in that gap from
    # the durable table so the coordination window survives the trim /
    # restart. Bounded by the remaining page budget, deduped against the
    # stream page by ``event_id``, older-first (they precede stream rows).
    seen_event_ids = {str(row["event_id"]) for row in matched if row.get("event_id") is not None}
    backfilled = await _backfill_recent_from_db(
        operator,
        since=since,
        oldest_stream_dt=_stream_oldest_dt(raw_entries),
        principal=principal,
        target=target,
        op_class=op_class,
        actor_sub=actor_sub,
        work_ref=work_ref,
        active_only=active_only,
        seen_event_ids=seen_event_ids,
        budget=limit - len(matched),
        now=datetime.now(UTC),
        serialize=serialize,
    )
    events = backfilled + matched

    # next_cursor pages forward from the LAST FETCHED entry (not the last
    # matched) so a fully-filtered page still advances; ``None`` at the
    # live tail. Stream-anchored -- backfill rows carry no stream cursor.
    next_cursor: str | None = raw_entries[-1][0] if len(raw_entries) == limit else None
    return {"events": events, "next_cursor": next_cursor}


def _match_and_serialize_entries(
    raw_entries: list[tuple[str, dict[str, str]]],
    *,
    stream_key: str,
    op_class: str | None,
    principal: str | None,
    target: str | None,
    actor_sub: str | None,
    work_ref: str | None,
    active_only: bool,
    serialize: Callable[[BroadcastEvent | AgentAnnouncementEvent], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Parse, filter, and serialise one ``XRANGE`` page into event dicts.

    Extracted from :func:`_list_recent_events_core` so the core stays
    under the code-quality function-size ceiling. Pins one ``now`` for the
    whole page so a slow parse can't let an ``active_only`` claim flip
    expiry-state mid-page. Each surviving entry becomes
    ``{"id", "cursor", **serialize(event)}``:

    * ``cursor`` is the Valkey stream entry id, self-labelled to match the
      ``cursor`` input arg it round-trips through (#2479); ``id`` is the
      historical alias of the same value.
    * ``serialize`` is the caller's per-surface serialiser:
      :func:`dump_event_wire` (LLM-facing MCP reads, wraps announcement
      free-text in the untrusted-content envelope) or
      :func:`_dump_event_plain` (the UI pane, which escapes at its HTML
      sink).
    """
    now = datetime.now(UTC)
    matched: list[dict[str, Any]] = []
    for entry_id, fields in raw_entries:
        event = parse_entry(entry_id, fields, stream_key=stream_key)
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
        matched.append({"id": entry_id, "cursor": entry_id, **serialize(event)})
    return matched


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

    Serialises through :func:`dump_event_wire`: this is an LLM-facing MCP
    read, so agent-authored announcement prose is wrapped in the
    stored-prompt-injection guard envelope.
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
        serialize=dump_event_wire,
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

    Serialises through :func:`_dump_event_plain` (no untrusted-text wrap):
    the sole caller is the human UI history pane, whose HTML sink escapes
    every field separately, so the LLM guard envelope would only add
    human-visible noise to a feed row (#2549).
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
            serialize=_dump_event_plain,
        )
    except RedisError as exc:
        _log.warning(
            "broadcast_history_fetch_failed",
            error_class=type(exc).__name__,
            stream_key=stream_key(operator.tenant_id),
        )
        return {"events": [], "next_cursor": None}


#: op_class values that carry the dispatch-time target-activity advisory
#: (#2550). Only mutating classes qualify -- a write is where crossfire
#: matters. Read-class dispatches (``read`` / ``credential_read`` /
#: ``audit_query`` / ``other`` / ``approval``) skip the stream read
#: entirely, keeping the hot read path free of the lookup.
_ADVISORY_WRITE_OP_CLASSES: Final[frozenset[str]] = frozenset(
    {"write", "credential_write", "credential_mint"}
)

#: Maximum advisory entries surfaced on a single dispatch response. The
#: advisory is an awareness nudge, not an audit -- the caller who needs
#: the full picture queries ``meho.broadcast.recent``.
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
    discipline's ``meho.broadcast.recent`` read step.

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
