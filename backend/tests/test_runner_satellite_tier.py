# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Satellite-mint tier ladder — the shared classifier + composed gate (#3188).

The single source of truth the three fail-closed layers mirror. These are
pure-function tests: no DB, no session.
"""

from __future__ import annotations

import pytest

from meho_backplane.runner.satellite_tier import (
    SatelliteMintTier,
    classify_satellite_tier,
    evaluate_remote_write_gate,
)


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
