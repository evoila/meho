# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""When an announcement claim is active (#3762).

A claim is a chain of steps an agent publishes: ``start``, then any
``update`` steps, then one ``completion`` (#3736). The newest step says
where the claim stands. A claim is **active** when its newest step:

* is a trusted claim step (``claim_id`` and ``owner_sub`` set, so a
  checked agent identity owns it). Rows written before trusted admission
  are history only;
* is not the ``completion`` step; and
* has a finite TTL that has not run out yet.

Readers that ask "who is working on this target right now" use these
rules: the dispatch-time target advisory
(:mod:`meho_backplane.broadcast.target_advisory`), the caller's own claim
lookup (:mod:`meho_backplane.broadcast.announce_claim_lookup`), and the
``active_only`` read filter
(:func:`meho_backplane.broadcast.history.event_matches` for one row, and
:func:`meho_backplane.broadcast.claim_archive.ended_claim_ids` for the
newest stored step of its claim).

A claim is advisory. It never locks a target and never blocks a write.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent

__all__ = ["is_active_claim_step", "latest_claim_steps"]


def is_active_claim_step(step: AgentAnnouncementEvent, now: datetime) -> bool:
    """Whether *step* keeps its claim active at *now*.

    Looks at one step only. A ``completion`` that came later is in
    another row; :func:`latest_claim_steps` finds it when the caller
    reads the whole claim.
    """
    if not step.is_trusted_claim or step.phase == "completion":
        return False
    expires_at = step.expires_at
    return expires_at is not None and expires_at > now


def latest_claim_steps(steps: Iterable[AgentAnnouncementEvent]) -> list[AgentAnnouncementEvent]:
    """Return the newest step of each trusted claim in *steps*.

    *steps* must be ordered newest committed first
    (:data:`~meho_backplane.broadcast.claim_archive.CLAIM_STEPS_NEWEST_FIRST`).
    Steps that are not trusted
    claim steps are left out. The result keeps the newest-first order.
    """
    seen: set[object] = set()
    latest: list[AgentAnnouncementEvent] = []
    for step in steps:
        if not step.is_trusted_claim or step.claim_id in seen:
            continue
        seen.add(step.claim_id)
        latest.append(step)
    return latest
