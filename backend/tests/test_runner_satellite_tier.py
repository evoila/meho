# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Satellite-mint tier ladder — the shared classifier + composed gate (#3188).

The single source of truth the three fail-closed layers mirror. These are
pure-function tests: no DB, no session.
"""

from __future__ import annotations

import pytest

from meho_backplane.runner.satellite_tier import (
    REMOTE_WRITE_SAFETY_LEVELS,
    RemoteWriteAllowEntry,
    SatelliteMintTier,
    classify_satellite_tier,
    evaluate_remote_write_gate,
    parse_runner_allowlist,
)

# Every ``safety_level`` value the classifier recognises (the closed
# ``safe < caution < dangerous < destructive`` enum, #3196), used to prove
# REMOTE_WRITE_SAFETY_LEVELS stays in lock-step with classify_satellite_tier.
_ALL_SAFETY_LEVELS = ("safe", "caution", "dangerous", "destructive")


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("safe", SatelliteMintTier.SAFE),
        ("caution", SatelliteMintTier.REMOTE_WRITE),
        ("dangerous", SatelliteMintTier.EXCLUDED),
        ("destructive", SatelliteMintTier.EXCLUDED),
    ],
)
def test_classify_maps_each_safety_level(level: str, expected: SatelliteMintTier) -> None:
    assert classify_satellite_tier(level) is expected


def test_classify_is_fail_closed_on_unknown_level() -> None:
    # A level added upstream without updating the ladder is EXCLUDED, never
    # silently minted to a runner.
    assert classify_satellite_tier("apocalyptic") is SatelliteMintTier.EXCLUDED


def test_remote_write_gate_is_fail_closed() -> None:
    # The composed gate (allowlist + approval/policy) is unprovisioned until
    # the sibling tasks wire it, so it refuses every remote-write op.
    decision = evaluate_remote_write_gate(op_id="net.ping", runner_id="runner-a")

    assert decision.permitted is False
    assert "net.ping" in decision.reason
    assert "runner-a" in decision.reason
    assert "remote-write" in decision.reason


def test_remote_write_gate_reason_omits_runner_when_absent() -> None:
    # The edge re-check has no runner_id to hand; the reason stays coherent.
    decision = evaluate_remote_write_gate(op_id="net.ping")

    assert decision.permitted is False
    assert "for runner" not in decision.reason


def test_remote_write_safety_levels_match_classifier() -> None:
    # Drift guard (#3192): REMOTE_WRITE_SAFETY_LEVELS is the SQL-expressible
    # twin of classify_satellite_tier, used to tier-scope the revocation
    # delivery filter. A level whose classification is REMOTE_WRITE must be in
    # the set, and no other level may be — else a revoked runner's delivery
    # filter refuses the wrong tier.
    classifier_remote_write = {
        level
        for level in _ALL_SAFETY_LEVELS
        if classify_satellite_tier(level) is SatelliteMintTier.REMOTE_WRITE
    }
    assert classifier_remote_write == REMOTE_WRITE_SAFETY_LEVELS
    # Concretely, only ``caution`` today.
    assert frozenset({"caution"}) == REMOTE_WRITE_SAFETY_LEVELS


# ---------------------------------------------------------------------------
# Allowlist gate (mechanism 2, #3190) — the shared matcher both layers run
# ---------------------------------------------------------------------------


def test_gate_admits_op_on_allowlist() -> None:
    # A provisioned allowlist that covers the op admits it (permitted, no reason).
    allowlist = (RemoteWriteAllowEntry("vmware.vm.tag_set", "*"),)
    decision = evaluate_remote_write_gate(
        op_id="vmware.vm.tag_set", allowlist=allowlist, target_scope="tgt-1"
    )
    assert decision.permitted is True
    assert decision.reason == ""


def test_gate_stage1_single_class_admits_exactly_that_class() -> None:
    # A single-enumerated-class Stage-1 allowlist admits exactly that op-class
    # and refuses every other — the minimal blast radius the rollout starts at.
    allowlist = (RemoteWriteAllowEntry("vmware.vm.tag_set", "*"),)

    admitted = evaluate_remote_write_gate(
        op_id="vmware.vm.tag_set", allowlist=allowlist, target_scope="tgt-1"
    )
    other = evaluate_remote_write_gate(
        op_id="vmware.vm.power_off", allowlist=allowlist, target_scope="tgt-1"
    )

    assert admitted.permitted is True
    assert other.permitted is False
    assert "not on this runner's remote-write allowlist" in other.reason
    assert "vmware.vm.power_off" in other.reason


def test_gate_off_allowlist_is_distinct_from_unprovisioned() -> None:
    # An empty allowlist is the *unprovisioned* fail-closed state (Stage 0);
    # a non-empty allowlist with no match is the *off-allowlist* refusal. Both
    # fail closed, but the reasons are distinct for observability.
    unprovisioned = evaluate_remote_write_gate(op_id="vmware.vm.tag_set", allowlist=())
    off_list = evaluate_remote_write_gate(
        op_id="vmware.vm.tag_set",
        allowlist=(RemoteWriteAllowEntry("vmware.vm.annotation_set", "*"),),
    )
    assert unprovisioned.permitted is False and "not provisioned" in unprovisioned.reason
    assert off_list.permitted is False and "not on this runner's remote-write" in off_list.reason


def test_gate_target_scope_cap_binds_to_one_target() -> None:
    # A target-scoped cap admits only the bound target; a re-pointed target is
    # refused even though the op-class matches.
    allowlist = (RemoteWriteAllowEntry("vmware.vm.tag_set", "tgt-blessed"),)

    on_target = evaluate_remote_write_gate(
        op_id="vmware.vm.tag_set", allowlist=allowlist, target_scope="tgt-blessed"
    )
    wrong_target = evaluate_remote_write_gate(
        op_id="vmware.vm.tag_set", allowlist=allowlist, target_scope="tgt-other"
    )
    assert on_target.permitted is True
    assert wrong_target.permitted is False


def test_gate_op_pattern_glob_matches_prefix() -> None:
    # An ``op_pattern`` glob covers a family of ops.
    allowlist = (RemoteWriteAllowEntry("vmware.vm.*", "*"),)
    assert evaluate_remote_write_gate(
        op_id="vmware.vm.tag_set", allowlist=allowlist, target_scope="t"
    ).permitted
    assert not evaluate_remote_write_gate(
        op_id="vmware.host.reboot", allowlist=allowlist, target_scope="t"
    ).permitted


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("   ", ()),
        ("vmware.vm.tag_set", (RemoteWriteAllowEntry("vmware.vm.tag_set", "*"),)),
        ("vmware.vm.tag_set@*", (RemoteWriteAllowEntry("vmware.vm.tag_set", "*"),)),
        ("vmware.vm.tag_set@tgt-1", (RemoteWriteAllowEntry("vmware.vm.tag_set", "tgt-1"),)),
        (
            " vmware.vm.tag_set , vmware.vm.annotation_set@tgt-2 ",
            (
                RemoteWriteAllowEntry("vmware.vm.tag_set", "*"),
                RemoteWriteAllowEntry("vmware.vm.annotation_set", "tgt-2"),
            ),
        ),
        # A blank token and a bare ``@`` (no op) are skipped, not crashed.
        ("vmware.vm.tag_set,,@scope", (RemoteWriteAllowEntry("vmware.vm.tag_set", "*"),)),
    ],
)
def test_parse_runner_allowlist(raw: str, expected: tuple[RemoteWriteAllowEntry, ...]) -> None:
    assert parse_runner_allowlist(raw) == expected
