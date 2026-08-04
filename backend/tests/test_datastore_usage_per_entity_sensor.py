# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-entity capacity sensing on ``vmware.composite.datastore.usage`` (#2758).

Initiative #2780 (checks reliability & evidence). The v0.26.0 ops report
could not assert on a single datastore's free/capacity: the op returned a
sampled envelope with no way to narrow to one entity. The exact-match
``filter_names`` param already existed (#524) but two things were unproven:

* that a **one-name** ``filter_names`` result returns **inline** (unsampled)
  under the dispatcher's default JSONFlux thresholds, so a Sensor can select
  ``$.datastores[0].free_space`` and threshold it; and
* that a realistic **VM-dense** datastore does not blow the single row past
  the 4096-byte byte threshold via the unbounded ``vm_names`` enrichment,
  which would collapse ``{"datastores": [row]}`` to a sampled envelope and
  strip the sensor's selector.

These tests compose the three real components the sensor dispatch+evaluate
path chains (``runner.py`` calls ``dispatch`` -> the dispatcher runs the
JSONFlux reducer -> the runner feeds ``result.result`` to
``evaluate_assertion``): the real composite handler, a real
``JsonFluxReducer()`` (the same default 50-row / 4096-byte instance
installed at ``main.py``), and the real ``evaluate_assertion``. They are
deterministic (no vcsim, no DB) so the regression is pinned in CI's unit
sweep, not gated on a live upstream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import msgspec
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks import AssertionSpec, evaluate_assertion
from meho_backplane.connectors.vmware_rest.composites._read import (
    datastore_usage_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    DATASTORE_USAGE_MAX_VM_NAMES,
    DATASTORE_USAGE_PARAMETER_SCHEMA,
    DATASTORE_USAGE_RESPONSE_SCHEMA,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer

#: Fixed, timezone-aware instant (evaluate_assertion requires an aware now).
NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)

#: Bytes in a gibibyte -- free_space / capacity are bytes on the wire.
_GIB = 1024**3

#: The reducer's default byte threshold (``JsonFluxReducer()`` installed at
#: ``main.py``). A single row serialising past this collapses to an envelope.
_BYTE_THRESHOLD = 4096

#: A per-datastore free-space threshold: degrade below 500 GiB, crit below 100.
_FREE_SPACE_COMPARE: dict[str, Any] = {
    "type": "threshold",
    "op": "lt",
    "degraded": float(500 * _GIB),
    "critical": float(100 * _GIB),
}


def _reducer() -> JsonFluxReducer:
    """A default-threshold reducer (50 rows / 4096 bytes, matching the singleton
    installed at ``main.py``).

    An explicit ``sample_byte_budget`` is passed only so the over-threshold
    assemble path does not read app settings (``get_settings()``) in this
    pure-module test; it sizes the envelope's inline sample and does not
    change the byte threshold that decides whether a payload collapses.
    """
    return JsonFluxReducer(sample_byte_budget=_BYTE_THRESHOLD)


def _make_operator() -> Operator:
    """Synthetic operator for the composite-handler call."""
    return Operator(
        sub="op-2758",
        name="Per-entity Sensor Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


class _SequenceConnector:
    """Minimal ``VmwareRestConnector`` stand-in serving canned GETs in order.

    ``datastore.usage`` issues only GET sub-ops (list datastores -> per-DS
    detail -> per-DS VM list), each routed through ``_read_sub_op``'s
    ``mount_op_path`` -> ``adapt_op_query`` -> ``_get_json`` seam. This double
    serves the responses sequentially and records each call's ``(path, query)``
    so a test can assert the ``filter.names`` narrowing flowed to the listing.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return query

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator
        self.calls.append({"path": path, "query": params})
        payload = self._responses[self._index]
        self._index += 1
        return payload


async def _usage_one_datastore(
    *,
    name: str,
    capacity: int,
    free_space: int,
    vm_names: list[str],
) -> tuple[dict[str, Any], _SequenceConnector]:
    """Run the real handler for a single filtered datastore, return (payload, conn)."""
    listing = [{"datastore": "datastore-42", "name": name, "type": "vSAN"}]
    detail = {"capacity": capacity, "free_space": free_space, "type": "vSAN"}
    vms = [{"name": vm_name} for vm_name in vm_names]
    conn = _SequenceConnector([listing, detail, vms])
    payload = await datastore_usage_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter_names": [name]},
        connector=conn,  # type: ignore[arg-type]
    )
    return payload, conn


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
    """AC#1: one-name filter_names -> inline row; a free_space Sensor bands right.

    The filtered result arrives inline (no ``{row_count, ..., source_key}``
    envelope) under the default reducer thresholds, so
    ``$.datastores[0].free_space`` under a ``ThresholdCompare`` evaluates
    through the real ``evaluate_assertion`` to the expected band.
    """
    capacity = 20 * 1024 * _GIB
    raw, conn = await _usage_one_datastore(
        name="vsan-prod-01",
        capacity=capacity,
        free_space=free_space,
        vm_names=["vm-a", "vm-b"],
    )

    # The one name narrowed the listing sub-op (forwarded as filter.names).
    assert conn.calls[0]["query"] == {"filter.names": ["vsan-prod-01"]}
    # Exactly one row, full aggregated shape.
    assert raw == {
        "datastores": [
            {
                "id": "datastore-42",
                "name": "vsan-prod-01",
                "type": "vSAN",
                "capacity": capacity,
                "free_space": free_space,
                "vm_count": 2,
                "vm_names": ["vm-a", "vm-b"],
            }
        ]
    }

    reduced, handle = await _reducer().reduce(raw, None, {})
    # Inline: no result handle, payload unchanged, no sampled-envelope keys.
    assert handle is None
    assert reduced == raw
    assert "row_count" not in reduced
    assert "source_key" not in reduced

    spec = AssertionSpec.model_validate(
        {"select": {"path": "$.datastores[0].free_space"}, "compare": _FREE_SPACE_COMPARE}
    )
    outcome = evaluate_assertion(spec, reduced, now=NOW)
    assert outcome.state == expected_state
    assert outcome.value == free_space


@pytest.mark.asyncio
async def test_vm_dense_row_capped_stays_inline_and_assertable() -> None:
    """AC#2 guard: a VM-dense datastore's one-name result stays inline + assertable.

    ``vm_count`` stays exact; ``vm_names`` is bounded to the sample cap, so the
    single-row payload serialises under the reducer byte threshold, the real
    reducer passes it through inline, and ``$.datastores[0].free_space`` is
    still selectable. The uncapped row (full VM list) exceeds the threshold and
    the real reducer collapses it to a sampled envelope, stripping the selector
    -- the exact failure the cap prevents (#2758).
    """
    dense_names = [f"prod-workload-vm-{i:04d}" for i in range(300)]
    free_space = 40 * _GIB
    raw, _ = await _usage_one_datastore(
        name="vsan-dense-01",
        capacity=50 * 1024 * _GIB,
        free_space=free_space,
        vm_names=dense_names,
    )
    row = raw["datastores"][0]

    # vm_count is the exact total; vm_names is the bounded sample (truncated).
    assert row["vm_count"] == 300
    assert row["vm_names"] == dense_names[:DATASTORE_USAGE_MAX_VM_NAMES]
    assert len(row["vm_names"]) == DATASTORE_USAGE_MAX_VM_NAMES

    # Capped payload is under the reducer byte threshold ...
    assert len(msgspec.json.encode(raw)) <= _BYTE_THRESHOLD
    # ... so the real reducer passes it through inline (free_space selectable).
    reduced, handle = await _reducer().reduce(raw, None, {})
    assert handle is None
    assert reduced == raw

    spec = AssertionSpec.model_validate(
        {
            "select": {"path": "$.datastores[0].free_space"},
            "compare": {"type": "threshold", "op": "lt", "critical": float(100 * _GIB)},
        }
    )
    assert evaluate_assertion(spec, reduced, now=NOW).state == "critical"

    # Contrast: the UNCAPPED row (full 300 names) exceeds the threshold, so the
    # reducer collapses {"datastores": [row]} to a sampled envelope and the
    # sensor's selector is gone (evaluates "unknown").
    uncapped = {"datastores": [{**row, "vm_count": 300, "vm_names": dense_names}]}
    assert len(msgspec.json.encode(uncapped)) > _BYTE_THRESHOLD
    envelope, env_handle = await _reducer().reduce(uncapped, None, {})
    assert env_handle is not None
    assert "row_count" in envelope
    assert evaluate_assertion(spec, envelope, now=NOW).state == "unknown"


def test_response_schema_maxitems_matches_cap_constant() -> None:
    """The declared response-schema bound is the single source the handler caps to."""
    vm_names_schema = DATASTORE_USAGE_RESPONSE_SCHEMA["properties"]["datastores"]["items"][
        "properties"
    ]["vm_names"]
    assert vm_names_schema["maxItems"] == DATASTORE_USAGE_MAX_VM_NAMES


def test_param_surface_stays_filter_names_only() -> None:
    """AC#4: no new params -- the op's parameter surface is filter_names-only."""
    assert list(DATASTORE_USAGE_PARAMETER_SCHEMA["properties"]) == ["filter_names"]
    assert DATASTORE_USAGE_PARAMETER_SCHEMA["additionalProperties"] is False
