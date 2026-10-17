# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the result-handle capacity benchmark (#3642).

The derivation core is pure and pinned exactly: the guard work count, the
Valkey allocation estimate, the recommendation rules and the table. The
measuring layer runs the real reducer and store codec on a small payload,
so only its shape and its invariants are checked (decode time and heap
depend on the machine).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from scripts.result_handle_capacity_benchmark import (
    CHART_MAXMEMORY_BYTES,
    KIB,
    MIB,
    PROFILES,
    VALKEY_BASELINE_BYTES,
    ProfileMeasurement,
    derive_recommendations,
    main,
    measure_admission_work,
    measure_profile,
    render_markdown,
    round_up,
    valkey_allocation_bytes,
)

from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard

_CAP = 16 * MIB


def _measurement(**overrides: Any) -> ProfileMeasurement:
    values: dict[str, Any] = {
        "profile": "p",
        "captured_rows": 100,
        "stored_rows": 100,
        "record_bytes": 1 * MIB,
        "cache_bytes": valkey_allocation_bytes(1 * MIB),
        "nodes": 50_000,
        "decoded_bytes": 2 * MIB,
        "depth": 3,
        "page_rows": 500,
        "page_wire_bytes": 300_000,
        "complete_wire_bytes": 10_000,
        "decode_heap_bytes": 4 * MIB,
        "decode_ms": 12.0,
    }
    values.update(overrides)
    return ProfileMeasurement(**values)


# ---------------------------------------------------------------------------
# Guard work count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_work_count_is_the_tightest_guard_that_admits_the_payload(profile: Any) -> None:
    payload = profile.payload(25)
    work = measure_admission_work(payload)

    exact = AdmissionGuard(
        max_decoded_bytes=work.decoded_bytes, max_depth=work.depth, max_nodes=work.nodes
    )
    assert exact.check(payload)
    assert not AdmissionGuard(
        max_decoded_bytes=work.decoded_bytes - 1, max_depth=work.depth, max_nodes=work.nodes
    ).check(payload)
    assert not AdmissionGuard(
        max_decoded_bytes=work.decoded_bytes, max_depth=work.depth, max_nodes=work.nodes - 1
    ).check(payload)
    if work.depth > 1:
        assert not AdmissionGuard(
            max_decoded_bytes=work.decoded_bytes, max_depth=work.depth - 1, max_nodes=work.nodes
        ).check(payload)


def test_work_count_charges_each_scalar_like_the_guard() -> None:
    work = measure_admission_work({"a": None, "b": True, "c": -120, "d": 1.5, "é": "ü"})
    # {} 2 + keys 1+1+1+1+2 + null 4 + true 5 + "-120" 4 + float 24 + "ü" 2
    assert work.decoded_bytes == 2 + 6 + 4 + 5 + 4 + 24 + 2
    assert work.nodes == 1 + 5 * 2
    assert work.depth == 1


def test_work_count_rejects_a_non_json_value() -> None:
    with pytest.raises(TypeError):
        measure_admission_work({"a": object()})


# ---------------------------------------------------------------------------
# Allocation estimate and rounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value_bytes", "expected"),
    [
        (1, 32),
        (100, 128),
        (16 * KIB - 16, 16 * KIB),
        (16 * KIB, 20 * KIB),
        (3 * MIB, 3 * MIB + 512 * KIB),
        (16 * MIB - 16, 16 * MIB),
        (16 * MIB, 20 * MIB),
    ],
)
def test_valkey_allocation_follows_jemalloc_size_classes(value_bytes: int, expected: int) -> None:
    assert valkey_allocation_bytes(value_bytes) == expected


def test_round_up() -> None:
    assert round_up(1, 64) == 64
    assert round_up(64, 64) == 64
    assert round_up(64.1, 64) == 128


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def test_recommendations_follow_the_documented_rules() -> None:
    recs = {
        r.setting: r
        for r in derive_recommendations(
            [_measurement(), _measurement(profile="wide", nodes=200_000, page_wire_bytes=900_000)],
            full_size_handles=32,
            headroom=1.25,
            max_record_bytes=_CAP,
        )
    }

    cache = recs["resultCache.maxmemory"]
    assert cache.needed == 32 * 20 * MIB + VALKEY_BASELINE_BYTES
    assert cache.recommended == round_up(cache.needed, 64 * MIB)
    assert cache.current == CHART_MAXMEMORY_BYTES
    assert cache.action == "keep"

    decoded = recs["RESULT_REDUCTION_MAX_DECODED_BYTES"]
    assert decoded.needed == 2 * _CAP  # 2 decoded bytes per stored byte
    assert decoded.recommended == round_up(2 * _CAP * 1.25, 16 * MIB)

    nodes = recs["RESULT_REDUCTION_MAX_NODES"]
    assert nodes.needed == 200_000 * 16  # the wider profile wins
    assert nodes.recommended == 4_000_000
    assert nodes.action == "raise"

    complete = recs["RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES"]
    assert complete.recommended == 64 * KIB
    assert complete.action == "lower"

    query = recs["RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES"]
    assert query.needed == 900_000
    assert query.recommended == round_up(900_000 * 1.25, 256 * KIB)


def test_completed_ceiling_never_drops_below_the_receipt_floor() -> None:
    recs = derive_recommendations(
        [_measurement(complete_wire_bytes=10)],
        full_size_handles=1,
        headroom=1.0,
        max_record_bytes=_CAP,
    )
    complete = next(r for r in recs if r.setting == "RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES")
    assert complete.recommended >= 2048


def test_recommendations_need_a_stored_record() -> None:
    with pytest.raises(ValueError):
        derive_recommendations([], full_size_handles=1, headroom=1.0, max_record_bytes=_CAP)
    with pytest.raises(ValueError):
        derive_recommendations(
            [_measurement(record_bytes=0)], full_size_handles=1, headroom=1.0, max_record_bytes=_CAP
        )


def test_markdown_table_names_every_limit_and_profile() -> None:
    measurements = [_measurement(profile=p.name) for p in PROFILES]
    recs = derive_recommendations(
        measurements, full_size_handles=32, headroom=1.25, max_record_bytes=_CAP
    )
    table = render_markdown(measurements, recs, workers=4, concurrent_peak_bytes=123)

    for setting in (
        "resultCache.maxmemory",
        "RESULT_REDUCTION_MAX_DECODED_BYTES",
        "RESULT_REDUCTION_MAX_NODES",
        "RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES",
        "RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES",
    ):
        assert f"`{setting}`" in table
    for profile in PROFILES:
        assert f"| {profile.name} |" in table
    assert "4 parallel decodes" in table


# ---------------------------------------------------------------------------
# Measuring layer and CLI
# ---------------------------------------------------------------------------


def test_profiles_are_deterministic() -> None:
    for profile in PROFILES:
        assert profile.payload(3) == profile.payload(3)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_measure_profile_runs_the_real_store_path(profile: Any) -> None:
    measurement, raw = measure_profile(profile, 120, max_record_bytes=_CAP)

    assert measurement.stored_rows == measurement.captured_rows == 120
    assert measurement.record_bytes == len(raw) > 0
    assert measurement.cache_bytes >= measurement.record_bytes
    assert 0 < measurement.page_rows <= 120
    assert measurement.page_wire_bytes > 0
    assert 2048 < measurement.complete_wire_bytes < 256 * KIB
    assert measurement.decode_heap_bytes > 0


def test_measure_profile_honours_the_record_cap() -> None:
    profile = next(p for p in PROFILES if p.name == "metric-stats")
    measurement, raw = measure_profile(profile, 200, max_record_bytes=64 * KIB)

    assert len(raw) <= 64 * KIB
    assert 0 < measurement.stored_rows < 200


def test_cli_prints_json(capfd: pytest.CaptureFixture[str]) -> None:
    assert main(["--rows", "60", "--format", "json"]) == 0
    stdout = capfd.readouterr().out
    # Another test may leave a cached logger that still prints spill log
    # lines; the JSON document is the last thing written.
    out = json.loads(stdout[stdout.index("{\n") :])
    assert len(out["measurements"]) == len(PROFILES)
    assert len(out["recommendations"]) == 5
    assert out["workers"] == 4


def test_cli_prints_markdown(capfd: pytest.CaptureFixture[str]) -> None:
    assert main(["--rows", "60"]) == 0
    assert "| Setting |" in capfd.readouterr().out


def test_cli_rejects_bad_arguments(capfd: pytest.CaptureFixture[str]) -> None:
    assert main(["--rows", "0"]) == 2
    assert "--rows" in capfd.readouterr().err
