# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Satellite-mint tier ladder — which safety levels may ride a runner (#3188).

The single source of truth for the satellite write-path gate, shared by the
three fail-closed layers that must move in lockstep (design §1.1 /
`docs/decisions/satellite-write-path.md`):

* the **central mint** (:func:`meho_backplane.operations.gateway_commands.mint_gateway_command`),
* the **assignment materialiser** (:mod:`meho_backplane.gateway.assignment_service`), and
* the **edge executor** (:func:`meho_backplane.runner.executor._screen_item`).

Each layer classifies a descriptor's ``safety_level`` into a
:class:`SatelliteMintTier` and enforces the same ladder independently — the
defence-in-depth premise the read path relies on (a compromised or buggy
single layer cannot alone punch a write through).

This module lives beside :mod:`meho_backplane.runner.wire` — the other
central+edge shared contract — precisely because the edge (runner) is
**DB-free**: the classifier and the gate seam import only the standard
library, so importing them on the runner never pulls the central DB stack.

The ladder (``safety_level`` → tier), composing with the destructive tier
(#3196/#3183):

* ``safe`` → :attr:`SatelliteMintTier.SAFE` — mints on ``AUTO_EXECUTE``, the
  Stage-0 read path, semantics **unchanged**.
* ``caution`` → :attr:`SatelliteMintTier.REMOTE_WRITE` — the additive,
  separately-gated write tier. Mints **only** through the composed gate
  (:func:`evaluate_remote_write_gate`); until the sibling tasks (#3189-#3193)
  wire an allowlist + approval/signing mechanism the gate is fail-closed, so
  no remote-write capability is ever authorised.
* ``dangerous`` / ``destructive`` (and any unknown level) →
  :attr:`SatelliteMintTier.EXCLUDED` — **never** minted to a satellite. This
  is where the write path composes with #3183: the destructive tier is
  excluded by the satellite gate by default everywhere, so delete-shaped work
  stays central-or-break-glass and never rides a runner.

The write tier does **not** widen what ``safe`` means and is **not** a new
``safety_level`` value: it is a runtime classification of the existing
``safe < caution < dangerous < destructive`` enum (#3196), so it needs no
schema migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "REMOTE_WRITE_SAFETY_LEVELS",
    "RemoteWriteGateDecision",
    "SatelliteMintTier",
    "classify_satellite_tier",
    "evaluate_remote_write_gate",
]

#: The ``safety_level`` values that classify into
#: :attr:`SatelliteMintTier.REMOTE_WRITE`. The SQL-expressible companion of
#: :func:`classify_satellite_tier` for callers that must tier-scope a query
#: without importing the classifier into the database layer — the
#: revocation-hardening delivery filter (#3192,
#: :func:`meho_backplane.gateway.queue.claim_next_command`) narrows a revoked
#: runner's claim to non-remote-write rows via
#: ``safety_level NOT IN REMOTE_WRITE_SAFETY_LEVELS``. Kept in lock-step with
#: :func:`classify_satellite_tier` by ``tests/test_runner_satellite_tier.py``:
#: a level added to the classifier's ``REMOTE_WRITE`` branch must be added
#: here, or the drift guard fails.
REMOTE_WRITE_SAFETY_LEVELS: frozenset[str] = frozenset({"caution"})


class SatelliteMintTier(StrEnum):
    """Which satellite-mint tier an op's ``safety_level`` falls into.

    Distinct from the ``safety_level`` enum itself: this is the gate a runner
    dispatch is screened against, not the intrinsic danger classification.
    """

    #: ``safe`` — mints on ``AUTO_EXECUTE`` (Stage-0 read path, unchanged).
    SAFE = "safe"
    #: ``caution`` — the additive write tier, gated by :func:`evaluate_remote_write_gate`.
    REMOTE_WRITE = "remote_write"
    #: ``dangerous`` / ``destructive`` / unknown — never minted to a satellite.
    EXCLUDED = "excluded"


def classify_satellite_tier(safety_level: str) -> SatelliteMintTier:
    """Map a descriptor ``safety_level`` onto its :class:`SatelliteMintTier`.

    Fail-closed on the default: any level other than ``safe`` / ``caution``
    (i.e. ``dangerous``, ``destructive``, or an unrecognised value) is
    :attr:`SatelliteMintTier.EXCLUDED`, so a level added upstream is refused
    from riding a runner until this ladder is deliberately updated.
    """
    if safety_level == "safe":
        return SatelliteMintTier.SAFE
    if safety_level == "caution":
        return SatelliteMintTier.REMOTE_WRITE
    return SatelliteMintTier.EXCLUDED


@dataclass(frozen=True)
class RemoteWriteGateDecision:
    """The outcome of the composed remote-write gate.

    :attr:`permitted` is ``True`` only when the op-class is authorised to ride
    the runner as a remote write; :attr:`reason` explains a refusal (and is
    surfaced verbatim in the mint refusal / edge refusal).
    """

    permitted: bool
    reason: str


#: The invariant part of every fail-closed remote-write refusal reason.
_UNPROVISIONED_REASON = (
    "the composed write-path gate (per-runner allowlist + approval/policy "
    "binding) is not provisioned; no runner is authorised for the "
    "remote-write tier"
)


def evaluate_remote_write_gate(
    *, op_id: str, runner_id: str | None = None
) -> RemoteWriteGateDecision:
    """The composed remote-write gate — fail-closed until the siblings wire it.

    A ``remote-write`` op mints (and executes at the edge) **only** when its
    op-class is on the runner's enrollment allowlist **and** either policy
    ``AUTO_EXECUTE`` (the idempotent subset) or a committed ``ApprovalRequest``
    (the caution subset) authorises it — the four-mechanism composition of
    design §3. That allowlist + approval/signing machinery is filed as sibling
    tasks (#3189-#3193) and does not exist yet, so there is no way to authorise
    a remote-write capability: this seam refuses **every** remote-write op.

    Both the central mint and the edge executor call this independently —
    mechanism 2's "checked at mint *and* re-checked at the edge" — so a
    remote-write item fails closed at each layer on its own.
    """
    subject = f"remote-write op {op_id!r}"
    if runner_id is not None:
        subject += f" for runner {runner_id!r}"
    return RemoteWriteGateDecision(
        permitted=False,
        reason=f"{subject} refused: {_UNPROVISIONED_REASON}",
    )
