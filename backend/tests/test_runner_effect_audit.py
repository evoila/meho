# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runner-side store-and-forward effect-audit chain (#2901, #3193, mechanism 4).

Covers the DB-free runner half: the hash-chained record writer produces a
strictly-monotonic, self-linking chain; the head persists across a restart (so a
runner cannot rewind ``seq`` — which would read as a gap at the centre); and the
forwarding drain removes only forwarded records while the head keeps climbing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meho_backplane.runner.effect_audit import (
    GENESIS_PREV_HASH,
    EffectAuditChain,
    EffectChainStateError,
    EffectPhase,
    canonical_record_body,
    compute_record_hash,
)

_RUNNER = "runner-eff"


def _chain(tmp_path: Path) -> EffectAuditChain:
    return EffectAuditChain(tmp_path / "effect-chain", runner_id=_RUNNER)


def _intent(chain: EffectAuditChain, *, command_id: str = "cmd-1"):
    return chain.record_intent(
        command_id=command_id,
        op_id="vmware.vm.tag.set",
        params_hash="ph-1",
        signature="sig-1",
        target_scope="scope-1",
    )


def test_intent_then_outcome_form_a_monotonic_self_linking_chain(tmp_path: Path) -> None:
    """AC: one intent record before, one outcome record after; hash-chained per seq."""
    chain = _chain(tmp_path)

    intent = _intent(chain)
    assert intent.seq == 0
    assert intent.phase is EffectPhase.INTENT
    assert intent.prev_hash == GENESIS_PREV_HASH
    assert intent.outcome is None

    outcome = chain.record_outcome(
        command_id="cmd-1",
        op_id="vmware.vm.tag.set",
        params_hash="ph-1",
        signature="sig-1",
        target_scope="scope-1",
        outcome="ok",
    )
    assert outcome.seq == 1
    assert outcome.phase is EffectPhase.OUTCOME
    assert outcome.outcome == "ok"
    # The link: the outcome's prev_hash is exactly the intent's record_hash.
    assert outcome.prev_hash == intent.record_hash

    # Each record_hash re-derives from its own canonical body (tamper anchor).
    for rec in (intent, outcome):
        canonical = canonical_record_body(
            runner_id=rec.runner_id,
            seq=rec.seq,
            phase=rec.phase,
            command_id=rec.command_id,
            op_id=rec.op_id,
            params_hash=rec.params_hash,
            signature=rec.signature,
            target_scope=rec.target_scope,
            outcome=rec.outcome,
            recorded_at=rec.recorded_at,
        )
        assert compute_record_hash(rec.prev_hash, canonical) == rec.record_hash


def test_head_persists_across_restart_no_rewind(tmp_path: Path) -> None:
    """A fresh chain object on the same dir continues the seq, never rewinds to 0."""
    first = _chain(tmp_path)
    r0 = _intent(first)
    r1 = first.record_outcome(
        command_id="cmd-1",
        op_id="op",
        params_hash="ph",
        signature="sig",
        target_scope="scope",
        outcome="ok",
    )

    # "Restart": a brand-new chain object bound to the same directory.
    restarted = _chain(tmp_path)
    r2 = restarted.record_intent(
        command_id="cmd-2",
        op_id="op",
        params_hash="ph",
        signature="sig",
        target_scope="scope",
    )
    assert r2.seq == 2, "the head must persist across a restart, not rewind seq"
    assert r2.prev_hash == r1.record_hash
    assert r0.seq == 0 and r1.seq == 1


def test_forwarding_drains_records_but_head_keeps_climbing(tmp_path: Path) -> None:
    """``mark_forwarded`` deletes forwarded records; the next seq still climbs."""
    chain = _chain(tmp_path)
    r0 = _intent(chain, command_id="cmd-1")
    r1 = chain.record_outcome(
        command_id="cmd-1",
        op_id="op",
        params_hash="ph",
        signature="sig",
        target_scope="scope",
        outcome="ok",
    )

    pending = chain.unforwarded()
    assert [r.seq for r in pending] == [0, 1]

    chain.mark_forwarded(r0.seq)
    assert [r.seq for r in chain.unforwarded()] == [1]

    # Head is untouched by the drain: a new record continues after seq 1.
    r2 = chain.record_intent(
        command_id="cmd-2",
        op_id="op",
        params_hash="ph",
        signature="sig",
        target_scope="scope",
    )
    assert r2.seq == 2
    assert r2.prev_hash == r1.record_hash

    chain.mark_forwarded(r2.seq)
    assert chain.unforwarded() == []


def test_compute_record_hash_is_body_sensitive() -> None:
    """A single body byte change yields a different chain hash (tamper anchor)."""
    body_a = canonical_record_body(
        runner_id="r",
        seq=0,
        phase=EffectPhase.INTENT,
        command_id="cmd",
        op_id="op",
        params_hash="ph",
        signature="sig",
        target_scope="scope",
        outcome=None,
        recorded_at="2026-09-01T00:00:00+00:00",
    )
    body_b = canonical_record_body(
        runner_id="r",
        seq=0,
        phase=EffectPhase.INTENT,
        command_id="cmd",
        op_id="op-EVIL",  # one field changed
        params_hash="ph",
        signature="sig",
        target_scope="scope",
        outcome=None,
        recorded_at="2026-09-01T00:00:00+00:00",
    )
    assert compute_record_hash(GENESIS_PREV_HASH, body_a) != compute_record_hash(
        GENESIS_PREV_HASH, body_b
    )


def test_corrupt_head_refuses_to_guess(tmp_path: Path) -> None:
    """A corrupt on-disk head raises rather than silently rewinding the chain."""
    chain = _chain(tmp_path)
    _intent(chain)
    # Corrupt the persisted head.
    head_file = tmp_path / "effect-chain" / "head.json"
    head_file.write_text("{ not json", encoding="utf-8")
    with pytest.raises(EffectChainStateError):
        _intent(chain, command_id="cmd-2")
