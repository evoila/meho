# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projection + liveness derivation for the ``/ui/runners`` console surface (#2589).

The list handler projects a
:class:`~meho_backplane.auth.runner_principals.RunnerPrincipalRead` plus the
runner's ``runner_assignments.stale_at`` dead-man marker into the flat dict
the template reads. Keeping the projection here (not in the route module)
makes the row-to-view mapping unit-testable without a FastAPI request fixture.

Liveness is derived **at render only** from persisted state -- staleness is
never recomputed client-side. The precedence (revoked, then stale, then
fresh) is a display concern distinct from #2416's five-state check rollup:

* ``revoked`` -> the principal was killed (``meho runner-principal revoke``);
  a decommissioned identity, muted rather than alarming.
* ``stale_at IS NOT NULL`` -> the central dead-man sweeper (#2501) declared
  this runner's workloads unknown; reuses the five-state ``unknown`` badge
  vocabulary (:func:`~meho_backplane.ui.routes.checks.views.state_badge_class`)
  -- no new state vocabulary.
* otherwise -> live; the template renders a relative ``last_seen_at``.

UTC coercion + the five-state badge map are reused from the checks surface so
the two console pages stay visually and behaviourally consistent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from meho_backplane.auth.runner_principals import RunnerPrincipalRead
from meho_backplane.ui.routes.checks.views import coerce_utc_aware, state_badge_class

__all__ = [
    "project_runner_to_row",
    "runner_liveness",
]

#: Liveness state -> DaisyUI badge modifier. ``revoked`` is a muted neutral
#: (a decommissioned runner is not an alarm), ``unknown`` reuses the checks
#: five-state ghost badge (dead-man flip), ``live`` is the healthy success
#: badge. Distinct from the check five-state vocabulary on purpose: this is a
#: per-runner liveness label, not a rolled-up check state.
_LIVENESS_BADGE: Final[dict[str, str]] = {
    "revoked": "badge-neutral",
    "unknown": state_badge_class("unknown"),
    "live": "badge-success",
}


def runner_liveness(*, revoked: bool, stale_at: datetime | None) -> tuple[str, str]:
    """Return the ``(state, badge_class)`` pair for a runner's liveness.

    Precedence: ``revoked`` wins over ``stale`` wins over ``live`` -- a
    revoked runner reads as decommissioned even if its last assignment row
    was also flipped stale. Rendered from persisted state; never recomputed.
    """
    if revoked:
        state = "revoked"
    elif stale_at is not None:
        state = "unknown"
    else:
        state = "live"
    return state, _LIVENESS_BADGE[state]


def project_runner_to_row(
    principal: RunnerPrincipalRead,
    *,
    stale_at: datetime | None,
) -> dict[str, object]:
    """Project one runner principal (+ its dead-man marker) into a template row.

    *stale_at* is the ``runner_assignments.stale_at`` value for this runner
    (``None`` when fresh or when the runner has no assignment row yet).
    """
    state, badge = runner_liveness(revoked=principal.revoked, stale_at=stale_at)
    return {
        "id": str(principal.id),
        "name": principal.name,
        "revoked": principal.revoked,
        "liveness_state": state,
        "liveness_badge": badge,
        "stale": stale_at is not None,
        "stale_at": coerce_utc_aware(stale_at),
        "last_seen_at": coerce_utc_aware(principal.last_seen_at),
        "created_at": coerce_utc_aware(principal.created_at),
    }
