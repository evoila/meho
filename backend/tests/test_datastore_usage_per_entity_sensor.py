# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-entity capacity sensing survives the result reduction (#2758).

A Sensor pins an operation, its params and an assertion, for example
"``$.volumes[0].free_space`` below 100 GiB is critical". The sensor runner
calls ``dispatch``, the dispatcher runs the JSONFlux reducer on the result,
and the runner feeds the reduced result to ``evaluate_assertion``. Two things
must hold, or the assertion never sees its value:

* a **one-entity** result (an exact-name filter) returns **inline** under the
  reducer's default thresholds, not as a sampled envelope with a result
  handle; and
* an entity with many members (a volume that holds hundreds of VMs) does not
  push its one row past the 4096-byte threshold through an unbounded name
  list. That would collapse ``{"volumes": [row]}`` to a sampled envelope and
  strip the sensor's selector.

These tests chain the real components of that path: a capacity operation
that the test declares itself (the public tree holds no VMware connector),
a real ``JsonFluxReducer()`` with the default thresholds ``main.py`` installs,
and the real ``evaluate_assertion``. They need no database and no live
system, so the regression stays pinned in the unit sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import msgspec
import pytest

from meho_backplane.checks import AssertionSpec, evaluate_assertion
from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

#: Fixed, timezone-aware instant (evaluate_assertion requires an aware now).
NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)

#: Bytes in a gibibyte -- free_space / capacity are bytes on the wire.
_GIB = 1024**3

#: The reducer's default byte threshold (``JsonFluxReducer()`` installed at
#: ``main.py``). A single row serialising past this collapses to an envelope.
_BYTE_THRESHOLD = 4096

#: The cap on ``member_names`` in one row. The count stays exact; the names
#: are a bounded sample, so one row always fits under the byte threshold.
MAX_MEMBER_NAMES = 20

#: A per-volume free-space threshold: degrade below 500 GiB, crit below 100.
_FREE_SPACE_COMPARE: dict[str, Any] = {
    "type": "threshold",
    "op": "lt",
    "degraded": float(500 * _GIB),
    "critical": float(100 * _GIB),
}

#: A critical-below-100-GiB free-space compare used by the dense-row guards.
_CRIT_ONLY_COMPARE: dict[str, Any] = {
    "type": "threshold",
    "op": "lt",
    "critical": float(100 * _GIB),
}


async def volume_usage(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """The synthetic capacity read: one row per volume of *target*.

    Has the shape of a typed op handler. ``filter_names`` narrows the rows to
    the named volumes. ``member_count`` is the exact total; ``member_names``
    is capped to :data:`MAX_MEMBER_NAMES`.
    """
    names = params.get("filter_names")
    rows = []
    for volume in target.volumes:
        if names is not None and volume["name"] not in names:
            continue
        members = volume["members"]
        rows.append(
            {
                "id": volume["id"],
                "name": volume["name"],
                "capacity": volume["capacity"],
                "free_space": volume["free_space"],
                "member_count": len(members),
                "member_names": members[:MAX_MEMBER_NAMES],
            }
        )
    return {"volumes": rows}


def _reducer() -> JsonFluxReducer:
    """A reducer pinned to the production default thresholds (50 rows /
    ``_BYTE_THRESHOLD`` bytes, matching ``JsonFluxReducer()`` installed at
    ``main.py``).

    ``byte_threshold`` is passed explicitly rather than relying on the
    constructor default, so the guard tests' byte assertions and the reducer's
    own collapse decision share one source of truth if the library default
    ever moves. ``sample_byte_budget`` is set only so the over-threshold
    assemble path does not read app settings (``get_settings()``) in this
    pure-module test; it sizes the envelope's inline sample, not the collapse
    decision. The explicit admission guard keeps the pre-threshold payload
    walk equally configuration-free.
    """
    return JsonFluxReducer(
        byte_threshold=_BYTE_THRESHOLD,
        sample_byte_budget=_BYTE_THRESHOLD,
        admission_guard=AdmissionGuard(),
    )


def _target(*volumes: dict[str, Any]) -> Any:
    """A target that carries the canned volumes the synthetic op reads."""
    return SimpleNamespace(name="capacity-sensor-target", volumes=list(volumes))


def _volume(name: str, *, capacity: int, free_space: int, members: list[str]) -> dict[str, Any]:
    return {
        "id": f"volume-{name}",
        "name": name,
        "capacity": capacity,
        "free_space": free_space,
        "members": members,
    }


def _free_space_spec(compare: dict[str, Any]) -> AssertionSpec:
    return AssertionSpec.model_validate(
        {"select": {"path": "$.volumes[0].free_space"}, "compare": compare}
    )


@pytest.mark.parametrize(
    ("free_space", "expected_state"),
    [
        (50 * _GIB, "critical"),
        (200 * _GIB, "degraded"),
        (800 * _GIB, "ok"),
    ],
)
@pytest.mark.asyncio
async def test_one_name_filter_returns_inline_and_sensor_bands(
    free_space: int, expected_state: str
) -> None:
    """One-name filter -> one inline row; a free_space Sensor bands right.

    The filtered result arrives inline (no ``{row_count, ..., source_key}``
    envelope) under the default reducer thresholds, so
    ``$.volumes[0].free_space`` under a ``ThresholdCompare`` evaluates through
    the real ``evaluate_assertion`` to the expected band.
    """
    capacity = 20 * 1024 * _GIB
    target = _target(
        _volume("other-01", capacity=capacity, free_space=10 * _GIB, members=["vm-z"]),
        _volume("prod-01", capacity=capacity, free_space=free_space, members=["vm-a", "vm-b"]),
    )

    raw = await volume_usage(target, {"filter_names": ["prod-01"]})

    # Exactly the one named row, full aggregated shape.
    assert raw == {
        "volumes": [
            {
                "id": "volume-prod-01",
                "name": "prod-01",
                "capacity": capacity,
                "free_space": free_space,
                "member_count": 2,
                "member_names": ["vm-a", "vm-b"],
            }
        ]
    }

    reduced, handle = await _reducer().reduce(raw, None, {})
    # Inline: no result handle, payload unchanged, no sampled-envelope keys.
    assert handle is None
    assert reduced == raw
    assert "row_count" not in reduced
    assert "source_key" not in reduced

    outcome = evaluate_assertion(_free_space_spec(_FREE_SPACE_COMPARE), reduced, now=NOW)
    assert outcome.state == expected_state
    assert outcome.value == free_space


@pytest.mark.asyncio
async def test_member_dense_row_capped_stays_inline_and_assertable() -> None:
    """A member-dense volume's one-name result stays inline and assertable.

    ``member_count`` stays the exact total; ``member_names`` is bounded to the
    cap, so the single-row payload serialises under the reducer byte
    threshold, the real reducer passes it through inline, and
    ``$.volumes[0].free_space`` is still selectable and thresholdable.
    """
    dense_names = [f"prod-workload-vm-{i:04d}" for i in range(300)]
    target = _target(
        _volume("dense-01", capacity=50 * 1024 * _GIB, free_space=40 * _GIB, members=dense_names)
    )

    raw = await volume_usage(target, {"filter_names": ["dense-01"]})
    row = raw["volumes"][0]

    # The count is the exact total; the names are the bounded sample.
    assert row["member_count"] == 300
    assert row["member_names"] == dense_names[:MAX_MEMBER_NAMES]

    # Under the byte threshold, so the real reducer passes it through inline.
    assert len(msgspec.json.encode(raw)) <= _BYTE_THRESHOLD
    reduced, handle = await _reducer().reduce(raw, None, {})
    assert handle is None
    assert reduced == raw

    outcome = evaluate_assertion(_free_space_spec(_CRIT_ONLY_COMPARE), reduced, now=NOW)
    assert outcome.state == "critical"


@pytest.mark.asyncio
async def test_uncapped_dense_row_would_collapse_and_lose_selector() -> None:
    """Why the cap is load-bearing: an unbounded single row blows the threshold.

    A volume row carrying its full (unbounded) ``member_names`` list
    serialises past the reducer byte threshold, so the reducer collapses
    ``{"volumes": [row]}`` to a sampled envelope and
    ``$.volumes[0].free_space`` is no longer selectable (evaluates
    ``unknown``) -- the exact failure the cap prevents.
    """
    uncapped: dict[str, Any] = {
        "volumes": [
            {
                "id": "volume-dense-01",
                "name": "dense-01",
                "capacity": 50 * 1024 * _GIB,
                "free_space": 40 * _GIB,
                "member_count": 300,
                "member_names": [f"prod-workload-vm-{i:04d}" for i in range(300)],
            }
        ]
    }
    assert len(msgspec.json.encode(uncapped)) > _BYTE_THRESHOLD

    envelope, handle = await _reducer().reduce(uncapped, None, {})
    assert handle is not None
    assert "row_count" in envelope

    outcome = evaluate_assertion(_free_space_spec(_CRIT_ONLY_COMPARE), envelope, now=NOW)
    assert outcome.state == "unknown"
