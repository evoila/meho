# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time checks-alert advisory (#2718, Initiative #2716).

When the caller's tenant has a Dashboard whose ``last_rollup_state``
memo (#2506's column, maintained exactly-once-per-transition by #2507's
compare-and-swap at the persist seam) is ``degraded`` or ``critical``,
the next successful dispatch response carries one compact
``extras["checks_alert_advisory"]`` entry naming it -- once per
``(caller, dashboard, state)`` window, deduped by an atomic Valkey
``SET NX EX``. Ambient awareness for anyone actively working through
the backplane: agent ``call_operation`` and operator CLI dispatches ride
the same path, so the fragment is on the wire for both without anyone
polling anything. Reaching the *operator's terminal* is a separate
question -- ``meho operation call`` prints ``extras`` on its human
render only for non-ok statuses, so the advisory surfaces there through
``--json`` today (see ``docs/codebase/checks-advisory.md``).

This is the second ``extras`` fragment beside the #2550
``target_activity_advisory`` (:mod:`meho_backplane.broadcast.history`),
and it deliberately diverges from that precedent in one way: it applies
to **all** op classes, read dispatches included. The #2550 gate to
write-class ops exists because that advisory is target-overlap-specific
-- it warns about concurrent mutation crossfire, which only matters when
the caller is about to mutate. A non-green Dashboard concerns every
active caller in the tenant regardless of what they are dispatching, so
gating here would defeat the feature. The per-caller NX dedupe is what
keeps the all-classes reach from becoming spam: each caller hears about
a given ``(dashboard, state)`` once per window.

Design contract (mirrors the #2550 mould):

* **Advisory only.** Never gates, blocks, or fails a dispatch. The
  fragment is appended to an already-successful response.
* **Fail-open.** A broad guard swallows any DB or Valkey error,
  warn-logs ``checks_alert_advisory_failed``, and returns ``{}`` -- the
  lookup never converts a successful dispatch into a failure.
* **Bounded.** Two round-trips per successful dispatch, whatever the
  tenant's state: one indexed read (``check_dashboard_tenant_idx``) of
  the memo column, capped at :data:`_ADVISORY_MAX_DASHBOARDS` rows, then
  one pipelined Valkey batch staging at most that many ``SET`` commands.
  A green tenant pays only the SELECT. Neither the cost nor the fragment
  grows when a correlated failure reddens a whole tenant at once -- the
  case both bounds exist for.
* **``0`` disables.** ``CHECKS_ALERT_ADVISORY_WINDOW_MINUTES`` (default
  30) short-circuits before any I/O when ``0``.
* **Memo-backed states only.** The advisory reads the transition memo,
  not the on-read rollup, so a staleness-derived ``unknown`` (which the
  memo never holds mid-window) is not reflected -- a deliberate,
  documented limitation acceptable for an awareness nudge. A ``NULL``
  memo (never-transitioned Dashboard) yields no advisory.
* **Server-derived fields only.** Each entry is
  ``{dashboard_id, name, state}`` read straight off the row -- no
  free-form text lifted from sensor evidence enters the op response
  (the untrusted-prose discipline of Initiative #2543).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from sqlalchemy import select

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.client import get_broadcast_client
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard
from meho_backplane.settings import get_settings

__all__ = [
    "CHECKS_ADVISORY_EXTRAS_KEY",
    "build_checks_alert_advisory",
]

_log = structlog.get_logger(__name__)

#: The ``extras`` key the advisory rides on the ``OperationResult`` --
#: sibling of :data:`meho_backplane.broadcast.history.ADVISORY_EXTRAS_KEY`.
CHECKS_ADVISORY_EXTRAS_KEY: Final[str] = "checks_alert_advisory"

#: Memo states that surface. ``degraded`` / ``critical`` only -- ``ok``
#: needs no nudge, ``skip`` is a deliberate opt-out, and ``unknown`` in
#: the memo means "cannot evaluate", which the investigator surface owns.
_ADVISORY_STATES: Final[tuple[str, ...]] = ("degraded", "critical")

#: How many non-green Dashboards one dispatch pays for. One SELECT is
#: both the read and the entry source here, so this single cap bounds
#: the query, the ``SET`` commands staged, and the fragment together --
#: and keeping claims and entries equal is what makes a claimed dedupe
#: key mean "this caller was told".
#:
#: Sized as an awareness nudge, not an audit: the rationale of the #2550
#: precedent's
#: :data:`~meho_backplane.broadcast.history._ADVISORY_MAX_ENTRIES`
#: (``5``), which is the constant this one plays the role of. Its
#: sibling ``_ADVISORY_SCAN_LIMIT = 100`` bounds a single ``XREVRANGE``
#: -- one round-trip whatever the count -- so its *value* carries no
#: argument for a cap that also decides what the agent reads. What the
#: value buys here is bytes of unsolicited agent context, and entry 11
#: of a nudge is worth ~nothing against that; a tenant that far into the
#: red is the checks surface's problem, not an advisory's.
#: ``ORDER BY name`` makes the truncation deterministic.
_ADVISORY_MAX_DASHBOARDS: Final[int] = 10


def _dedupe_key(
    tenant_id: uuid.UUID,
    principal_sub: str,
    actor_sub: str | None,
    dashboard_id: uuid.UUID,
    state: str,
) -> str:
    """Valkey key deduping one ``(caller, dashboard, state)`` per window.

    Caller identity is the ``(principal_sub, actor_sub)`` pair -- the
    same pair the #2550 advisory uses for its self-activity drop -- so a
    delegated agent and its human principal each get their own reminder.
    An unbound actor renders as ``-``. A state change mints a new key,
    so escalation (``degraded`` -> ``critical``) re-announces
    immediately; window expiry re-reminds on unchanged state.

    The two sub segments are free-form and the delimiter is ``:``, so
    two distinct ``(principal, actor)`` pairs could in principle render
    the same key (e.g. ``a:b``/``-`` vs ``a``/``b:-``). The UUID + closed-
    vocabulary segments anchor both ends, the aliasing window is one
    reminder wide, and the payload is advisory-only -- not worth an
    escaping scheme.
    """
    return (
        f"meho:checks:advisory:{tenant_id}:{principal_sub}:"
        f"{actor_sub or '-'}:{dashboard_id}:{state}"
    )


async def _non_green_dashboards(
    tenant_id: uuid.UUID,
) -> list[tuple[uuid.UUID, str, str]]:
    """Read ``(id, name, last_rollup_state)`` for non-green memo rows.

    One SELECT riding ``check_dashboard_tenant_idx``; rows whose memo is
    ``NULL`` (never transitioned) or outside :data:`_ADVISORY_STATES`
    never match. Ordered by name and capped at
    :data:`_ADVISORY_MAX_DASHBOARDS`, so the fragment is deterministic
    and the caller's per-dispatch cost does not scale with how much of
    the tenant is broken.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.execute(
            select(CheckDashboard.id, CheckDashboard.name, CheckDashboard.last_rollup_state)
            .where(
                CheckDashboard.tenant_id == tenant_id,
                CheckDashboard.last_rollup_state.in_(_ADVISORY_STATES),
            )
            .order_by(CheckDashboard.name)
            .limit(_ADVISORY_MAX_DASHBOARDS)
        )
        return [(row[0], row[1], row[2]) for row in rows.all()]


async def _claim_unannounced(
    candidates: list[tuple[str, dict[str, Any]]],
    window_seconds: int,
) -> list[dict[str, Any]]:
    """Claim each candidate's dedupe key; return the entries that won.

    Every key is claimed with ``SET key 1 NX EX <window-seconds>``, all
    of them staged into one pipelined ``MULTI``/``EXEC`` batch: a single
    awaited round-trip however many Dashboards are red. Batching is what
    bounds the cost -- a claim has to be issued before anyone can know
    whether it will succeed, so the NX dedupe cannot save the round-trip
    it rides on, and an unpipelined loop would pay one per row on
    *every* successful dispatch for as long as the tenant is red.

    An entry survives only when its own claim succeeded
    (``parse_set_result`` yields ``True`` for OK and ``None`` when the
    key already exists -- verified against the pinned redis-py 8.0.1,
    whose ``Pipeline`` shares the client's response-callback table, so
    buffering does not change the shape).

    ``transaction=True`` (MULTI/EXEC, the idiom in
    ``broadcast/rate_limit.py``) so a mid-batch teardown cannot leave
    some keys claimed while the caller's fail-open guard discards the
    entries they stood for.
    """
    client = get_broadcast_client()
    async with client.pipeline(transaction=True) as pipe:
        for key, _entry in candidates:
            pipe.set(key, "1", nx=True, ex=window_seconds)
        claims = await pipe.execute()
    return [entry for (_key, entry), claimed in zip(candidates, claims, strict=True) if claimed]


async def build_checks_alert_advisory(operator: Operator) -> dict[str, Any]:
    """Build the ``extras`` fragment naming the tenant's non-green Dashboards.

    Returns ``{"checks_alert_advisory": [{"dashboard_id", "name",
    "state"}, ...]}`` or an empty dict (no key added) when the advisory
    does not apply. The empty-dict short-circuits, in order:

    * the feature is disabled (``checks_alert_advisory_window_minutes
      == 0`` -- checked before any DB or Valkey I/O);
    * the tenant has no Dashboard with a ``degraded`` / ``critical``
      memo (the indexed SELECT is the only I/O paid);
    * every non-green Dashboard was already announced to this caller in
      the current window (each ``SET NX`` found its key present).

    Each surviving row's ``(caller, dashboard, state)`` dedupe key is
    claimed through :func:`_claim_unannounced`, which batches every
    claim into one Valkey round-trip and keeps only the Dashboards whose
    key it won. The Valkey TTL key IS the delivery state: nothing
    durable is persisted; a Valkey flush re-reminds each caller once.

    Note for sizing :data:`_ADVISORY_MAX_DASHBOARDS`: ``extras`` is
    attached after the dispatcher has already reduced the payload, so
    this fragment never passes through JSONFlux/result-handle
    reduction -- the cap is the only bound on the context it adds.

    Fail-open: any error -- DB teardown, Valkey teardown, anything --
    is swallowed by the broad guard, warn-logged as
    ``checks_alert_advisory_failed``, and yields ``{}``.
    """
    window_minutes = get_settings().checks_alert_advisory_window_minutes
    if window_minutes <= 0:
        return {}
    try:
        rows = await _non_green_dashboards(operator.tenant_id)
        if not rows:
            return {}
        actor_sub = resolve_actor_sub()
        entries = await _claim_unannounced(
            [
                (
                    _dedupe_key(operator.tenant_id, operator.sub, actor_sub, dashboard_id, state),
                    {"dashboard_id": str(dashboard_id), "name": name, "state": state},
                )
                for dashboard_id, name, state in rows
            ],
            window_minutes * 60,
        )
    except Exception:
        # Advisory is best-effort awareness; any failure (a DB or Valkey
        # teardown included) must never convert a successful dispatch
        # into a failure.
        _log.warning(
            "checks_alert_advisory_failed",
            tenant_id=str(operator.tenant_id),
        )
        return {}
    if not entries:
        return {}
    return {CHECKS_ADVISORY_EXTRAS_KEY: entries}
