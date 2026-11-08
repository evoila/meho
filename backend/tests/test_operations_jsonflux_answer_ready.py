# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Answer-ready reduced replies, behind the ``JSONFLUX_ANSWER_READY`` setting.

With the setting on, a reduced reply also carries ``items`` (every row in 1
to 3 short columns, when it fits) and ``counts`` (values per column with few
values), and the handle carries one read-more line instead of two long
texts. With the setting off (the default) nothing changes. These tests use
the real result store over an in-memory Valkey double.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.operations.answer_ready import (
    ITEMS_BYTE_BUDGET,
    column_counts,
    compact_items,
    fit_items,
    pick_columns,
    read_more_line,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_document import json_text_bytes
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000a4171")
_SUB = "op-answer-ready"
_LIMIT = 4096


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv("JSONFLUX_ANSWER_READY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Valkey:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


def _context(op_id: str = "example.vm.list", **extra: Any) -> dict[str, Any]:
    return {"op_id": op_id, "operator_sub": _SUB, "tenant_id": str(_TENANT), **extra}


def _vms(count: int) -> list[dict[str, Any]]:
    return [
        {
            "vm": f"vm-{i}",
            "name": f"web-{i:02d}",
            "power_state": "POWERED_ON" if i % 3 else "POWERED_OFF",
            "cpu_count": 2 if i % 2 else 4,
            "memory_size_MiB": 4096,
            "notes": "x" * 200,
        }
        for i in range(count)
    ]


def _reducer(*, answer_ready: bool | None = True, max_spill_rows: int = 10_000) -> JsonFluxReducer:
    return JsonFluxReducer(
        store=ResultHandleStore(_Valkey()),  # type: ignore[arg-type]
        max_spill_rows=max_spill_rows,
        answer_ready=answer_ready,
    )


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_setting_is_off_by_default() -> None:
    assert get_settings().jsonflux_answer_ready is False


def test_setting_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JSONFLUX_ANSWER_READY", "true")
    get_settings.cache_clear()
    assert get_settings().jsonflux_answer_ready is True


async def test_off_by_default_keeps_todays_reply() -> None:
    summary, handle = await _reducer(answer_ready=None).reduce(
        {"value": _vms(69)}, None, _context()
    )
    assert handle is not None
    assert "items" not in summary
    assert "counts" not in summary
    assert summary["sample_rows_returned"] == 5
    assert handle.sample_rows is not None and len(handle.sample_rows) == 5
    assert handle.fetch_more is not None
    assert "offset" in handle.fetch_more.drill_in.rationale
    assert handle.fetch_more.native_pagination.rationale is not None


async def test_setting_on_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JSONFLUX_ANSWER_READY", "true")
    get_settings.cache_clear()
    summary, _handle = await _reducer(answer_ready=None).reduce(
        {"value": _vms(20)}, None, _context()
    )
    assert "items" in summary


# ---------------------------------------------------------------------------
# The reply with the switch on
# ---------------------------------------------------------------------------


async def test_table_and_counts_replace_the_sample() -> None:
    summary, handle = await _reducer().reduce({"value": _vms(20)}, None, _context())
    assert handle is not None and handle.fetch_more is not None
    # Today's keys stay.
    assert summary["row_count"] == 20
    assert summary["total"] == 20
    assert summary["source_key"] == "value"
    # Every row, in short columns; the 200-character column is never used.
    assert summary["items"][0] == {"name": "web-00", "power_state": "POWERED_OFF", "cpu_count": 4}
    assert len(summary["items"]) == 20
    assert summary["counts"] == {
        "power_state": {"POWERED_ON": 13, "POWERED_OFF": 7},
        "cpu_count": {"2": 10, "4": 10},
        "memory_size_MiB": {"4096": 20},
    }
    # The sample would repeat rows the table has, so it is dropped.
    assert handle.sample_rows is None
    assert summary["sample_rows_returned"] == 0
    assert summary["sample_bytes"] == 0
    # One line instead of the two long texts.
    line = handle.fetch_more.drill_in.rationale
    assert line.startswith("Showing all 20 rows this call returned in items (name, power_state")
    assert "The counts cover all 20 rows." in line
    assert f"handle_id={handle.handle_id}" in line
    assert handle.fetch_more.native_pagination.available is False
    assert handle.fetch_more.native_pagination.rationale is None
    assert json_text_bytes(summary) < _LIMIT


async def test_long_text_column_is_not_counted_or_shown() -> None:
    summary, _handle = await _reducer().reduce({"value": _vms(20)}, None, _context())
    assert "notes" not in summary["counts"]
    assert all("notes" not in item for item in summary["items"])


async def test_wide_table_drops_columns_until_it_fits() -> None:
    summary, handle = await _reducer().reduce({"value": _vms(69)}, None, _context())
    assert handle is not None and handle.fetch_more is not None
    assert summary["items"] == [{"name": f"web-{i:02d}"} for i in range(69)]
    assert summary["counts"]["power_state"] == {"POWERED_ON": 46, "POWERED_OFF": 23}
    assert "in items (name)." in handle.fetch_more.drill_in.rationale


async def test_table_too_big_keeps_the_sample() -> None:
    rows = [{"name": f"server-with-a-long-name-{i:04d}", "state": "on"} for i in range(400)]
    summary, handle = await _reducer().reduce({"value": rows}, None, _context())
    assert handle is not None and handle.fetch_more is not None
    assert "items" not in summary
    assert summary["counts"] == {"state": {"on": 400}}
    assert summary["sample_rows_returned"] == 5
    assert handle.sample_rows is not None and len(handle.sample_rows) == 5
    assert handle.fetch_more.drill_in.rationale.startswith(
        "Showing 5 of 400 rows this call returned. The counts cover all 400 rows."
    )


async def test_cut_store_says_so() -> None:
    summary, handle = await _reducer(max_spill_rows=10).reduce(
        {"value": _vms(20)}, None, _context()
    )
    assert handle is not None and handle.fetch_more is not None
    # The table and the counts cover every row the call returned.
    assert len(summary["items"]) == 20
    assert sum(summary["counts"]["power_state"].values()) == 20
    line = handle.fetch_more.drill_in.rationale
    assert "Only the first 10 rows are stored; the rest cannot be read." in line
    assert handle.fetch_more.drill_in.stored_rows == 10


async def test_read_more_line_says_the_source_may_hold_more() -> None:
    """The short line never calls the stored rows the whole source (#3676)."""
    _summary, handle = await _reducer().reduce({"value": _vms(20)}, None, _context())
    assert handle is not None and handle.fetch_more is not None
    line = handle.fetch_more.drill_in.rationale
    assert line.startswith("Showing all 20 rows this call returned in items")
    assert line.endswith("The vendor may hold more rows than this call returned.")
    assert "complete" not in line


async def test_not_stored_says_result_query_cannot_read() -> None:
    context = {"op_id": "example.vm.list"}  # no tenant: the spill is skipped
    summary, handle = await _reducer().reduce({"value": _vms(20)}, None, context)
    assert handle is not None and handle.fetch_more is not None
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is False
    assert drill_in.reason == "no_tenant_context"
    assert "not stored (no_tenant_context)" in drill_in.rationale
    assert "result_query cannot read them" in drill_in.rationale
    assert len(summary["items"]) == 20


async def test_pagination_hint_still_ships() -> None:
    hint = {"params": {"limit": "Page size."}, "example_next_call": {"limit": 10}}
    _summary, handle = await _reducer().reduce(
        {"value": _vms(20)}, None, _context(pagination_hint=hint)
    )
    assert handle is not None and handle.fetch_more is not None
    native = handle.fetch_more.native_pagination
    assert native.available is True
    assert dict(native.params or {}) == {"limit": "Page size."}


@pytest.mark.parametrize("context", [_context("vault.kv.list"), {"tenant_id": str(_TENANT)}])
async def test_credential_or_unknown_op_keeps_todays_reply(context: dict[str, Any]) -> None:
    rows = [{"name": f"secret-{i}", "kind": "kv"} for i in range(60)]
    summary, handle = await _reducer().reduce({"keys": rows}, None, context)
    assert handle is not None and handle.fetch_more is not None
    assert "items" not in summary
    assert "counts" not in summary
    assert not handle.fetch_more.drill_in.rationale.startswith("Showing")


async def test_a_part_named_counts_is_not_replaced() -> None:
    payload = {"value": _vms(20), "counts": {"ok": 18, "bad": 2}, "items": {"a": 1}}
    summary, handle = await _reducer().reduce(payload, None, _context())
    assert handle is not None and handle.fetch_more is not None
    assert summary["counts"] == {"ok": 18, "bad": 2}
    assert summary["items"] == {"a": 1}
    assert handle.sample_rows is not None
    assert "counts cover" not in handle.fetch_more.drill_in.rationale


async def test_a_full_summary_stays_under_the_limit() -> None:
    parts = {f"part_{i}": {"text": "y" * 300} for i in range(11)}
    summary, handle = await _reducer().reduce({"value": _vms(20), **parts}, None, _context())
    assert handle is not None
    assert summary["parts_not_inline"]
    assert "parts_note" in summary
    assert json_text_bytes(summary) < _LIMIT


async def test_no_room_means_no_table_and_the_sample_stays() -> None:
    parts = {f"part_{i}": {"text": "y" * 300} for i in range(11)}
    rows = [{"name": f"host-{i:03d}", "state": "on" if i % 2 else "off"} for i in range(60)]
    summary, handle = await _reducer().reduce({"value": rows, **parts}, None, _context())
    assert handle is not None
    assert "items" not in summary
    assert summary["sample_rows_returned"] == 5
    assert json_text_bytes(summary) < _LIMIT


async def test_digest_hint_names_the_columns() -> None:
    rows = [
        {"id": f"t-{i}", "label": f"step-{i}", "status": "DONE" if i % 4 else "FAILED"}
        for i in range(60)
    ]
    digest = {"collection": "tasks", "status_field": "status", "name_field": "label"}
    summary, _handle = await _reducer().reduce(
        {"tasks": rows, "milestones": []}, None, _context(result_digest=digest)
    )
    assert summary["items"][0] == {"label": "step-0", "status": "FAILED"}


async def test_admission_limited_rows_get_no_table() -> None:
    from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard

    reducer = JsonFluxReducer(
        store=ResultHandleStore(_Valkey()),  # type: ignore[arg-type]
        answer_ready=True,
        admission_guard=AdmissionGuard(max_decoded_bytes=10, max_depth=64, max_nodes=10),
    )
    summary, handle = await reducer.reduce({"value": _vms(20)}, None, _context())
    assert handle is not None
    assert summary["status"] == "unprofiled"
    assert "items" not in summary


# ---------------------------------------------------------------------------
# The helpers, edge inputs
# ---------------------------------------------------------------------------


def test_counts_edge_inputs() -> None:
    rows: list[dict[str, Any]] = [
        {"state": "on", "flag": True, "tag": None, "obj": {"a": 1}, "uniq": 1},
        {"state": "off", "flag": False, "uniq": 2},
        {"state": "on", "flag": True, "tag": None, "obj": {"a": 2}, "uniq": 3},
    ]
    counts = column_counts(rows)
    assert counts["state"] == {"on": 2, "off": 1}
    assert counts["flag"] == {"true": 2, "false": 1}
    assert "tag" not in counts  # only nulls: says nothing
    assert "obj" not in counts  # not a short value
    assert "uniq" not in counts  # every row differs


def test_counts_skip_columns_with_many_values() -> None:
    rows = [{"zone": f"z{i % 11}"} for i in range(30)]
    assert column_counts(rows) == {}
    rows = [{"zone": f"z{i % 10}"} for i in range(30)]
    assert len(column_counts(rows)["zone"]) == 10


def test_counts_respect_the_byte_budget() -> None:
    rows = [{f"c{n}": f"v{i % 2}" for n in range(40)} for i in range(4)]
    counts = column_counts(rows, byte_budget=100)
    assert 0 < len(counts) < 40
    assert json_text_bytes(counts) <= 100


def test_counts_of_empty_and_single_row() -> None:
    assert column_counts([]) == {}
    assert column_counts([{"state": "on"}]) == {}


def test_pick_columns_prefers_identity_then_counted() -> None:
    rows = [{"id": "a", "name": "n1", "state": "on"}, {"id": "b", "name": "n2", "state": "on"}]
    assert pick_columns(rows, {"state": {"on": 2}}) == ["name"]  # one-value column left out
    assert pick_columns(rows, {"state": {"on": 1, "off": 1}}) == ["name", "state"]
    assert pick_columns(rows, {}, preferred=("state", "missing")) == ["state"]
    assert pick_columns([{"blob": "x" * 100}], {}) == []


def test_compact_items_is_all_or_nothing() -> None:
    rows = [{"name": "a"}, {"other": 1}]
    assert compact_items(rows, ["name"], byte_budget=100) == [{"name": "a"}, {"name": None}]
    assert compact_items(rows, ["name"], byte_budget=10) is None
    assert compact_items([], ["name"], byte_budget=100) is None
    assert compact_items(rows, [], byte_budget=100) is None


def test_fit_items_within_budget() -> None:
    rows = [{"name": f"n{i}", "state": "on"} for i in range(5)]
    items, columns = fit_items(rows, ["name", "state"], byte_budget=ITEMS_BYTE_BUDGET) or ([], [])
    assert columns == ["name", "state"]
    narrow = fit_items(rows, ["name", "state"], byte_budget=80)
    assert narrow is not None and narrow[1] == ["name"]
    assert json_text_bytes(narrow[0]) <= 80
    assert fit_items(rows, ["name"], byte_budget=5) is None
    assert len(items) == 5


def test_read_more_line_without_items_or_counts() -> None:
    line = read_more_line(
        handle_id="<handle-id>",
        total_rows=80,
        items_columns=(),
        sample_rows=5,
        has_counts=False,
        stored_rows=80,
        unavailable_reason=None,
    )
    assert line == (
        "Showing 5 of 80 rows this call returned. For other columns or a filter, "
        "call result_query with handle_id=<handle-id>."
    )
