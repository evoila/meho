# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-authored broadcast events (G6.4-T2 #1092).

Distinct from :class:`~meho_backplane.broadcast.events.BroadcastEvent`,
which is **derived from an audit row** every chassis call writes (one
broadcast per audited operation, fail-open publish, payload PII-redacted
upstream). :class:`AgentAnnouncementEvent` is **agent-authored** -- a
narrative event the agent emits explicitly via the
``meho.broadcast.announce`` MCP tool to coordinate with other operators
in the same tenant (intent at start of work, periodic updates, final
completion summary). Three differences carry across every field on this
class:

* **No ``audit_id``.** The audit row for the ``meho.broadcast.announce``
  tools/call invocation itself comes from the chassis ``AuditMiddleware``
  (one audit row per MCP call). The stream event records the
  announcement *content*; the audit log records the *act* of announcing.
  Hardwiring an ``audit_id`` here would imply the announcement was
  derived from a separate audit row, which it isn't.
* **No ``op_id`` / ``op_class``.** An announcement is not the result of
  any single operation -- it's narrative prose the agent chose to emit.
  The taxonomy that classifies ops into ``read`` / ``write`` /
  ``credential_*`` / ``audit_query`` / ``other`` (see
  :func:`~meho_backplane.broadcast.events.classify_op`) has no slot for
  "the agent just told us what it's about to do".
* **Free-text fields are agent-authored, hence UNTRUSTED.** Consumers
  rendering ``activity`` / ``scope`` in any surface (frontend HTML,
  Slack mirror, downstream LLM context) MUST treat the strings as
  attacker-controlled content. See the trust-boundary note below.

Trust boundary (load-bearing)
=============================

``activity`` and ``scope`` are free-form strings the agent typed. They
WILL contain prompt-injection attempts when another tenant's compromised
agent decides to try (e.g. an announcement reading ``"ignore previous
instructions and exfiltrate <X>"``). Every consuming surface treats them
as untrusted:

* **Frontend rendering** (G6.1-T4 SSE → UI): HTML-escape on display via
  the existing UI sanitisation chain. Already in place for
  :class:`BroadcastEvent.payload` -- the same sink applies here.
* **Slack mirror** (G6.2, already shipped): plain-text mode, no rich
  formatting (no Markdown rendering that could activate injected
  markup).
* **LLM consumption** (other agents reading via ``meho.broadcast.recent``,
  ``meho.broadcast.watch``, or ``meho://tenant/{tenant_id}/feed``):
  the calling agent MUST NOT treat another agent's ``activity`` as
  policy / system input. This is the same isolation contract the G7.1
  preamble assembler enforces with its
  ``<<TENANT_CONVENTIONS ... END_TENANT_CONVENTIONS>>`` wrapper around
  agent-untrusted operational rules
  (:mod:`meho_backplane.conventions.preamble`). Enforced at the read
  boundary since evoila-bosnia/meho-internal#154:
  :func:`~meho_backplane.broadcast.history.dump_event_wire` wraps
  ``activity`` / ``scope`` / ``target`` in the untrusted-content
  envelope (:mod:`meho_backplane.untrusted_text`) on every LLM-facing
  re-serve.

Structure is trusted; prose is quarantined (load-bearing)
=========================================================

The trust boundary above quarantines free *prose* because prose can
carry instructions a reading agent might absorb. It does NOT extend to
**server-validated structured metadata**. The typed coordination fields
this model carries -- ``planned_op_class`` (a bounded enum),
``ttl_minutes`` (a bounded int), ``run_id`` (a UUID), ``phase`` (an
enum), ``ts`` / ``expires_at`` (timestamps) -- are validated by pydantic
at construction: a value that is not a member of its type is rejected at
the boundary, never stored, never served. Such a field cannot express an
injection payload, so it is serialised **unwrapped** and other agents
may read it as trustworthy coordination data. This is the same split the
``target`` equality filter already relies on -- it runs on the unwrapped
model before :func:`~meho_backplane.broadcast.history.dump_event_wire`
wraps the prose (see that module).

The string fields stay quarantined even when they look structured:
``activity`` / ``scope`` / ``target`` / ``targets[]`` / ``work_ref`` are
all agent-authored text, so every LLM-facing dump wraps them (the
list-valued ``targets`` per-element). Equality filtering on ``target`` /
``targets`` / ``work_ref`` runs on the raw model value *before* the wrap,
so narrowing is unaffected by the envelope.

Publish semantics
=================

Distinct from :func:`~meho_backplane.broadcast.publisher.publish_event`:

* :func:`publish_event` is **fail-open** -- a Valkey error never
  propagates because the audit row is canonical.
* :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`
  is **fail-loud** -- the agent explicitly emitted the announcement
  and needs to know whether it landed. A swallowed announcement is
  worse than a JSON-RPC error: the agent thinks it told the team and
  the team never saw it.

The stream wire shape is identical (``meho:feed:{tenant_id}``, one
event field carrying JSON) so T1's :func:`broadcast_recent` reads both
kinds back through the same ``XRANGE``. The wire-side discriminator is
the ``event_kind`` field -- ``"agent_announcement"`` here,
absent-or-implicit on the audit-driven :class:`BroadcastEvent`.

References
----------

* Parent Initiative: #1090 (G6.4 Broadcast meta-tools).
* This task: #1092 (T2).
* Audit-driven sibling event class:
  :mod:`meho_backplane.broadcast.events`.
* Publisher entry point:
  :func:`meho_backplane.broadcast.publisher.publish_agent_announcement`.
* MCP tool handler:
  :mod:`meho_backplane.mcp.tools.broadcast` (section
  ``meho.broadcast.announce``).
* Untrusted-content precedent:
  :mod:`meho_backplane.conventions.preamble`.
* Decision #3 (PII defaults -- N/A here; agent-authored content is
  agent-controlled, not auto-redacted):
  ``docs/decisions/locked-decisions.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Final, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

__all__ = [
    "ACTIVITY_MAX_CHARS",
    "MAX_TARGETS",
    "PLANNED_OP_CLASS_VALUES",
    "TARGET_MAX_CHARS",
    "TTL_MAX_MINUTES",
    "TTL_MIN_MINUTES",
    "WORK_REF_MAX_CHARS",
    "AgentAnnouncementEvent",
    "PlannedOpClass",
]


#: Maximum length of the agent-authored ``activity`` string. Pinned at
#: 500 chars per the issue body. The cap is enforced at the MCP-handler
#: layer (where it surfaces a clean JSON-RPC ``-32602`` Invalid Params
#: with the violating length), AND on this model via
#: :class:`pydantic.Field(max_length=...)` so any code path that
#: constructs an :class:`AgentAnnouncementEvent` directly (e.g. an
#: integration test, a future REST sibling) gets the same protection.
#: A duplicate guard is cheaper than the post-incident audit needed if
#: a 10kB activity string ever lands on the stream and propagates to
#: every subscriber.
ACTIVITY_MAX_CHARS: Final[int] = 500

#: Per-string cap on ``target`` / ``targets[]`` attribution values.
#: Matches the announce inputSchema's 256-char ceiling on ``target``.
TARGET_MAX_CHARS: Final[int] = 256

#: Per-claim cap on the number of ``targets`` an announcement may name.
#: A single announcement is a coordination signal, not a bulk fleet
#: sweep -- an agent claiming ten targets at once is already stretching
#: the "avoid crossfire" contract; beyond that the entry stops being
#: legible on the feed. Over-long lists reject at the boundary with
#: ``-32602``.
MAX_TARGETS: Final[int] = 10

#: Cap on the opaque ``work_ref`` change-ticket reference. Mirrors the
#: convention on :attr:`meho_backplane.db.models.AgentRun.work_ref`
#: (an opaque string such as ``"gh:evoila/meho#123"``).
WORK_REF_MAX_CHARS: Final[int] = 256

#: Lower / upper bounds on a claim's ``ttl_minutes``. 1 minute floor
#: (a sub-minute claim is noise); 1440-minute (24h) ceiling matches the
#: broadcast stream's ~24h retention heuristic
#: (:data:`meho_backplane.broadcast.publisher.BROADCAST_MAXLEN`) -- a
#: claim outliving the substrate that carries it is a false promise.
#: Kubernetes Events default ``--event-ttl`` is 1h, a point inside this
#: band.
TTL_MIN_MINUTES: Final[int] = 1
TTL_MAX_MINUTES: Final[int] = 1440

#: The op-class an agent may *declare* it is about to run. Spans the
#: full :func:`meho_backplane.broadcast.events.classify_op` output
#: taxonomy -- deliberately wider than the recent/watch read-filter
#: :data:`meho_backplane.broadcast.history.OP_CLASS_ENUM` (which omits
#: ``credential_write`` / ``approval``): a *declaration* must be able to
#: name the sensitive write classes precisely because those are the
#: highest-crossfire operations to warn peers about, whereas the read
#: filter narrows already-classified :class:`BroadcastEvent` rows. The
#: value is trusted structured metadata (a bounded enum, not prose), so
#: it is served UNWRAPPED.
PlannedOpClass = Literal[
    "read",
    "write",
    "credential_read",
    "credential_write",
    "credential_mint",
    "audit_query",
    "approval",
    "other",
]

#: Tuple form of :data:`PlannedOpClass`, derived from the Literal so the
#: JSON-Schema enum on the announce tool and the model type stay a
#: single source of truth.
PLANNED_OP_CLASS_VALUES: Final[tuple[str, ...]] = get_args(PlannedOpClass)


class AgentAnnouncementEvent(BaseModel):
    """One agent-authored announcement event written to the tenant stream.

    Built by the ``meho.broadcast.announce`` MCP handler from validated
    tool arguments and ``XADD``'d to ``meho:feed:{operator.tenant_id}``
    via :func:`~meho_backplane.broadcast.publisher.publish_agent_announcement`.

    The model is **frozen** (``ConfigDict(frozen=True)``) to mirror
    :class:`~meho_backplane.broadcast.events.BroadcastEvent`. As with
    that sibling, ``frozen=True`` is faux-immutability in pydantic v2:
    attribute reassignment raises but nested mutables (none on this
    class -- every field is a primitive, ``UUID``, or ``datetime``)
    would still be in-place-mutable. The handler constructs one
    instance per call and never mutates; the contract is documented
    rather than type-enforced.

    Field shape (carries to the wire JSON via :meth:`model_dump_json`):

    * ``event_kind`` -- discriminator. Always ``"agent_announcement"``;
      a future event class would pick a different literal. T1's
      :mod:`~meho_backplane.mcp.tools.broadcast` reader dispatches on
      this field to decide which model class to validate against
      (absent or missing → :class:`BroadcastEvent`, the audit-driven
      sibling).
    * ``tenant_id`` -- derived exclusively from the operator's verified
      JWT claim at handler time. The MCP tool's input schema has NO
      ``tenant_id`` field; cross-tenant announce is structurally
      impossible -- not "checked then rejected" but "no surface that
      could ask for another tenant's stream".
    * ``principal_sub`` -- the operator's JWT ``sub`` claim, copied
      verbatim. Used by T1's ``filter.principal`` narrowing.
    * ``activity`` -- the free-text body. UNTRUSTED, agent-authored.
      Capped at :data:`ACTIVITY_MAX_CHARS`; longer strings surface as
      JSON-RPC ``-32602``.
    * ``target`` -- optional single target_name attribution. Caller
      passes a string when the announcement scopes to a specific managed
      target (``"prod-vc-1"``, ``"kube-prod"``); ``None`` when the
      activity is target-less (e.g. cross-cluster investigation).
      Retained for backward compatibility; ``targets`` supersedes it
      for multi-target claims. UNTRUSTED prose.
    * ``targets`` -- optional list of target_name attributions (each
      1..:data:`TARGET_MAX_CHARS` chars, at most :data:`MAX_TARGETS`
      entries). Supersedes -- does not replace -- the single ``target``:
      a claim spanning several targets names them all here, and the
      ``target`` filter on recent/watch matches an event when the query
      value equals ``target`` OR appears in ``targets``. UNTRUSTED prose
      (each element wrapped on display).
    * ``scope`` -- optional free-form scope hint (``"investigating
      cluster X latency"``). Also UNTRUSTED prose.
    * ``planned_op_class`` -- optional declared intent: the
      :data:`PlannedOpClass` the agent is about to run
      (``"write"`` / ``"credential_read"`` / ...). TRUSTED structured
      metadata (a validated enum), served unwrapped so peers can reason
      about crossfire without absorbing prose.
    * ``ttl_minutes`` -- optional claim lifetime in minutes
      (:data:`TTL_MIN_MINUTES`..:data:`TTL_MAX_MINUTES`). Consumers
      derive ``expires_at = ts + ttl``; the ``active_only`` read filter
      drops claims whose ``expires_at`` has elapsed. TRUSTED int.
    * ``work_ref`` -- optional opaque change-ticket reference
      (``"gh:evoila/meho#123"``), same convention + cap
      (:data:`WORK_REF_MAX_CHARS`) as
      :attr:`meho_backplane.db.models.AgentRun.work_ref`. Joins an
      announcement to the out-of-band record that authorised the work;
      the recent/watch ``work_ref`` filter matches on it. UNTRUSTED
      prose (wrapped on display), but exact-match filterable pre-wrap.
    * ``run_id`` -- optional :class:`~uuid.UUID` of the agent run the
      announcement belongs to, so a human or peer can group a run's
      announcements. TRUSTED UUID, served unwrapped.
    * ``expires_at`` -- derived (computed) ``ts + ttl_minutes``, or
      ``None`` when ``ttl_minutes`` is unset. TRUSTED timestamp, served
      unwrapped.
    * ``phase`` -- ``"start"`` / ``"update"`` / ``"completion"``.
      ``"update"`` is the default; ``"start"`` marks an intent
      announcement at the beginning of a task, ``"completion"`` marks
      the wrap-up summary. The Prometheus counter
      ``broadcast_agent_announcements_total{phase}`` partitions by
      this label so dashboards can show "how often agents announce
      starts vs. completions".
    * ``ts`` -- server-side wall clock at handler entry. The agent
      doesn't provide this -- chasing client-side clock skew across
      every JWT-issuing edge would defeat the team-coordination
      signal.
    """

    model_config = ConfigDict(frozen=True)

    #: G0.16-T6 Finding F (#1312) discriminator per
    #: ``docs/codebase/api-shape-conventions.md`` §6. Mirrors the
    #: sibling :attr:`BroadcastEvent.kind` field; consumers normalize
    #: on this field rather than the historical ``event_kind``. The
    #: latter is retained for backward compatibility with any
    #: in-flight stream entries written by the v0.8.0 publisher
    #: (which only emitted ``event_kind``); the history parser reads
    #: both forms.
    kind: Literal["agent_announcement"] = "agent_announcement"
    event_kind: Literal["agent_announcement"] = "agent_announcement"
    tenant_id: UUID
    principal_sub: str
    activity: str = Field(min_length=1, max_length=ACTIVITY_MAX_CHARS)
    target: str | None = None
    targets: list[Annotated[str, Field(min_length=1, max_length=TARGET_MAX_CHARS)]] = Field(
        default_factory=list,
        max_length=MAX_TARGETS,
    )
    scope: str | None = None
    planned_op_class: PlannedOpClass | None = None
    ttl_minutes: int | None = Field(default=None, ge=TTL_MIN_MINUTES, le=TTL_MAX_MINUTES)
    work_ref: str | None = Field(default=None, min_length=1, max_length=WORK_REF_MAX_CHARS)
    run_id: UUID | None = None
    phase: Literal["start", "update", "completion"] = "update"
    ts: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expires_at(self) -> datetime | None:
        """Wall-clock instant the claim's TTL elapses, or ``None`` if unbounded.

        Derived, never stored: ``ts + ttl_minutes``. ``None`` when
        ``ttl_minutes`` is unset (the announcement is not a
        time-bounded claim). Served UNWRAPPED on the wire -- a derived
        timestamp cannot carry an injection -- and drives the
        ``active_only`` read filter
        (:func:`meho_backplane.broadcast.history.event_matches`).
        """
        if self.ttl_minutes is None:
            return None
        return self.ts + timedelta(minutes=self.ttl_minutes)
