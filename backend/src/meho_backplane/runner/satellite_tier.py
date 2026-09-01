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

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase

__all__ = [
    "REMOTE_WRITE_SAFETY_LEVELS",
    "TARGETLESS_SCOPE",
    "RemoteWriteAllowEntry",
    "RemoteWriteGateDecision",
    "SatelliteMintTier",
    "classify_satellite_tier",
    "evaluate_remote_write_gate",
    "parse_runner_allowlist",
]

#: The canonical ``target_scope`` token for a targetless op (no resolved
#: target). Mirrors
#: :data:`meho_backplane.runner.work_item_signing.TARGETLESS_SCOPE`; duplicated
#: here (rather than imported) so this DB-free module keeps its stdlib-only
#: import surface. A concrete op scopes to ``str(target.id)``.
TARGETLESS_SCOPE = ""

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


@dataclass(frozen=True)
class RemoteWriteAllowEntry:
    """One entry of a runner's remote-write capability allowlist (#3190).

    :attr:`op_pattern` is a glob over ``op_id`` (``fnmatch`` semantics: an
    exact op-class, or a ``*`` prefix); :attr:`target_scope` is a cap on the
    target — ``*`` for any target in the tenant, or a concrete
    ``str(target.id)`` binding the capability to one target.

    The **single source of truth** the two enforcement layers share: the
    central mint builds these from the ``runner_write_allowlist`` rows and the
    DB-free edge builds them from its own provisioning config
    (:func:`parse_runner_allowlist`), so both feed the *same*
    :func:`evaluate_remote_write_gate` matcher — the defence-in-depth mould the
    safe wall uses with :func:`classify_satellite_tier`.
    """

    op_pattern: str
    target_scope: str = "*"


#: The invariant part of the fail-closed refusal reason when a runner has **no**
#: remote-write allowlist at all (the unprovisioned Stage-0 state — every runner
#: today). Kept verbatim so the #3188 fail-closed conformance assertions hold.
_UNPROVISIONED_REASON = (
    "the composed write-path gate (per-runner allowlist + approval/policy "
    "binding) is not provisioned; no runner is authorised for the "
    "remote-write tier"
)


def parse_runner_allowlist(raw: str) -> tuple[RemoteWriteAllowEntry, ...]:
    """Parse a runner's own allowlist config string into allow-entries (edge side).

    The runner is DB-free, so it learns its allowlist from its provisioning
    config (:attr:`meho_backplane.settings.Settings.satellite_write_allowlist`,
    provisioned at enrollment beside the verification key), not from the DB.
    The format is a comma-separated list of ``op_pattern`` or
    ``op_pattern@target_scope`` tokens (``@`` omitted ⇒ ``target_scope='*'``);
    blank tokens are skipped. Deliberately trivial + stdlib-only so it never
    pulls the central stack onto the edge. Example:
    ``"vmware.vm.tag_set@*,vmware.vm.annotation_set"``.
    """
    entries: list[RemoteWriteAllowEntry] = []
    for token in raw.split(","):
        spec = token.strip()
        if not spec:
            continue
        op_pattern, sep, target_scope = spec.partition("@")
        op_pattern = op_pattern.strip()
        if not op_pattern:
            continue
        scope = target_scope.strip() if sep else "*"
        entries.append(RemoteWriteAllowEntry(op_pattern=op_pattern, target_scope=scope or "*"))
    return tuple(entries)


def _entry_admits(entry: RemoteWriteAllowEntry, *, op_id: str, target_scope: str) -> bool:
    """Whether *entry* covers this ``op_id`` + resolved ``target_scope``.

    Case-sensitive glob on both axes (``op_id`` is case-sensitive): the op must
    match ``op_pattern`` and the concrete target must fall within the entry's
    ``target_scope`` cap (``*`` admits any target, including the targetless
    scope).
    """
    return fnmatchcase(op_id, entry.op_pattern) and fnmatchcase(target_scope, entry.target_scope)


def evaluate_remote_write_gate(
    *,
    op_id: str,
    allowlist: Sequence[RemoteWriteAllowEntry] = (),
    target_scope: str = TARGETLESS_SCOPE,
    runner_id: str | None = None,
) -> RemoteWriteGateDecision:
    """The composed remote-write gate's **allowlist half** — mechanism 2 (#3190).

    A ``remote-write`` op is admitted **only** when its ``op_id`` + resolved
    ``target_scope`` matches one entry of the runner's *allowlist*. That
    allowlist is the runner's write blast radius (design §3, threats T1/T8): the
    central mint passes the ``runner_write_allowlist`` rows; the DB-free edge
    passes its own provisioning-config mirror (:func:`parse_runner_allowlist`).
    Both call this same matcher — "checked at mint *and* re-checked at the edge"
    — so a remote-write item is screened independently at each layer.

    This is one half of the tier's authorization: the caller ANDs it with the
    approval binding (#3189/#3246) — at the mint a committed ``ApprovalRequest``,
    at the edge the centre's Ed25519 signature — so the tier is satisfiable only
    when **both** the allowlist and the approval binding pass. Fail-closed on
    each side: an **empty** allowlist (no capability provisioned — every runner
    today) keeps the invariant unprovisioned reason; a **non-empty** allowlist
    with no matching entry refuses as off-allowlist. Neither writes a row.
    """
    subject = f"remote-write op {op_id!r}"
    if runner_id is not None:
        subject += f" for runner {runner_id!r}"

    if any(_entry_admits(entry, op_id=op_id, target_scope=target_scope) for entry in allowlist):
        return RemoteWriteGateDecision(permitted=True, reason="")

    if not allowlist:
        reason = f"{subject} refused: {_UNPROVISIONED_REASON}"
    else:
        reason = (
            f"{subject} refused: op-class + target (scope {target_scope!r}) is not on "
            "this runner's remote-write allowlist"
        )
    return RemoteWriteGateDecision(permitted=False, reason=reason)
