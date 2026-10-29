# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The parts next to reduced rows are kept with the rows (#5139).

A part is a top-level object or list next to the rows of a large answer:
``assumptions``, ``inputs``, ``totals``, ``unknowns``. The reducer stores
every part with the rows, copies whole parts inline while the summary stays
under 4,096 bytes, and names the rest. These tests use the default reducer
limits and the real result store over an in-memory Valkey double.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.connectors.result_handle_store import ResultHandleStore, ResultRecordKindError
from meho_backplane.jsonflux.query.json_pointer import parse_json_pointer, resolve_json_pointer
from meho_backplane.operations import jsonflux_reducer as reducer_module
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_document import json_text_bytes
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")
_SUB = "op-parts"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The reducer reads its default limits from settings."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Valkey:
    """Minimal async Valkey double so the real store can spill."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


def _context(op_id: str = "example.assessment.run", **extra: Any) -> dict[str, Any]:
    return {"op_id": op_id, "operator_sub": _SUB, "tenant_id": str(_TENANT), **extra}


def _assessment_payload() -> dict[str, Any]:
    return {
        "items": [{"server": f"srv-{i}", "cores": 4, "fit": "ok"} for i in range(60)],
        "schema_version": 1,
        "calculation_version": "2026-10",
        "assumptions": {"cpu_headroom": 0.2, "region": "example-region"},
        "inputs": {"source": "inventory", "servers": 60},
        "totals": {"cores": 240, "memory_gb": 960},
        "counts": {"ok": 58, "too_big": 2},
        "unknowns": [{"server": "srv-7", "field": "disk"}],
        "omissions": ["srv-9 has no memory data"],
    }


_PART_KEYS = ("assumptions", "inputs", "totals", "counts", "unknowns", "omissions")


async def _path_read(store: ResultHandleStore, handle_id: uuid.UUID, pointer: str) -> Any:
    stored = await store.fetch_document(tenant_id=_TENANT, operator_sub=_SUB, handle_id=handle_id)
    assert stored is not None, "the handle keeps no fields next to the rows"
    return resolve_json_pointer(stored.document, parse_json_pointer(pointer))


async def test_parts_are_inline_or_named_and_a_path_read_returns_each_whole() -> None:
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = _assessment_payload()

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, _context())

    assert handle is not None
    assert reduced["source_key"] == "items"
    named = set(reduced.get("parts_not_inline", []))
    for key in _PART_KEYS:
        assert reduced.get(key) == payload[key] or key in named, f"{key} was lost"
        assert await _path_read(store, handle.handle_id, f"/{key}") == payload[key]
    window = await store.fetch_window(
        tenant_id=_TENANT, operator_sub=_SUB, handle_id=handle.handle_id, offset=0, limit=100
    )
    assert window is not None
    assert len(window.rows) == 60


async def test_parts_that_do_not_fit_are_named_with_the_read_that_returns_them() -> None:
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = _assessment_payload()
    payload["inputs"] = {"servers": [f"srv-{i}" for i in range(500)]}

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, _context())

    assert handle is not None
    assert "inputs" not in reduced
    assert reduced["parts_not_inline"] == ["inputs"]
    assert '"path": "/<name>"' in reduced["parts_note"]
    assert reduced["assumptions"] == payload["assumptions"]
    assert reduced["parts_note"] in handle.fetch_more.drill_in.rationale
    assert await _path_read(store, handle.handle_id, "/inputs") == payload["inputs"]


async def test_credential_class_op_keeps_the_old_rule() -> None:
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = {
        "keys": [f"secret-{i}" for i in range(60)],
        "metadata": {"mount": "kv", "token_hint": "s.example"},
        "warnings": ["lease is short"],
    }

    reduced, handle = await JsonFluxReducer(store=store).reduce(
        payload, None, _context("vault.kv.list")
    )

    assert handle is not None
    assert "metadata" not in reduced
    assert "warnings" not in reduced
    assert "parts_not_inline" not in reduced
    with pytest.raises(ResultRecordKindError):
        await store.fetch_document(tenant_id=_TENANT, operator_sub=_SUB, handle_id=handle.handle_id)


async def test_parts_too_large_for_the_store_keep_the_rows_and_say_they_cannot_be_read() -> None:
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = _assessment_payload()
    payload["omissions"] = ["x" * 1000 for _ in range(50)]
    reducer = JsonFluxReducer(store=store)

    original_spill = store.spill

    async def small_cap_spill(**kwargs: Any) -> Any:
        # The rows fit the cap; the rows plus the large part do not.
        return await original_spill(**kwargs, max_record_bytes=20_000)

    store.spill = small_cap_spill  # type: ignore[method-assign]
    reduced, handle = await reducer.reduce(payload, None, _context())

    assert handle is not None
    assert handle.fetch_more.drill_in.available is True
    assert "omissions" in reduced["parts_not_inline"]
    assert "cannot be read" in reduced["parts_note"]
    assert reduced["parts_note"] in handle.fetch_more.drill_in.rationale
    window = await store.fetch_window(
        tenant_id=_TENANT, operator_sub=_SUB, handle_id=handle.handle_id, offset=0, limit=100
    )
    assert window is not None
    assert len(window.rows) == 60


async def test_summary_stays_under_4096_bytes_with_many_large_parts() -> None:
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload: dict[str, Any] = {"items": [{"i": i} for i in range(60)]}
    for i in range(40):
        payload[f"part_with_a_long_name_{i:02d}"] = {"text": "y" * (300 + i)}
        payload[f"list_part_{i:02d}"] = ["z" * 50] * (i + 1)
    for i in range(20):
        payload[f"scalar_{i:02d}"] = "s" * 40

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, _context())

    assert handle is not None
    assert json_text_bytes(reduced) < 4096
    parts = [key for key in payload if key != "items" and not key.startswith("scalar_")]
    inline = [key for key in parts if reduced.get(key) == payload[key]]
    assert inline, "small parts should still be inline"
    assert reduced["parts_not_inline_count"] == len(parts) - len(inline)
    assert set(reduced["parts_not_inline"]) <= set(parts) - set(inline)
    for key in parts:
        assert await _path_read(store, handle.handle_id, f"/{key}") == payload[key]


async def test_a_part_is_never_cut_and_never_shadows_a_reducer_key() -> None:
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = {
        "items": [{"i": i} for i in range(60)],
        "total": {"vendor": "value"},
        "big": ["b" * 100 for _ in range(100)],
    }

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, _context())

    assert handle is not None
    assert reduced["total"] == 60
    assert "big" not in reduced
    assert sorted(reduced["parts_not_inline"]) == ["big", "total"]
    assert await _path_read(store, handle.handle_id, "/total") == {"vendor": "value"}


async def test_a_small_answer_does_not_change() -> None:
    payload = {"items": [{"i": i} for i in range(3)], "totals": {"n": 3}, "unknowns": []}

    reduced, handle = await JsonFluxReducer().reduce(payload, None, _context())

    assert handle is None
    assert reduced is payload


def test_parts_keys_are_reserved() -> None:
    assert {"parts_not_inline", "parts_not_inline_count", "parts_note"} <= (
        reducer_module._RESERVED_SUMMARY_KEYS
    )


async def test_the_names_and_the_note_never_push_the_summary_over_the_limit() -> None:
    """A hinted scalar fills the summary; the parts rule must not grow it past 4,096 bytes.

    Before the shared budget, the names and the note were added after the
    size check, so a summary of 3,953 bytes became 4,145 bytes. Now the
    largest plain value moves out to the store, the summary fits, and both
    the value and the part stay readable with a ``path``.
    """
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = {
        "items": [{"server": f"srv-{i}", "cores": 4, "fit": "ok"} for i in range(60)],
        "report": "r" * 3850,
        "assumptions": {"text": "a" * 500},
    }
    context = _context(result_scalars={"keys": ["report"]})

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, context)

    assert handle is not None
    assert json_text_bytes(reduced) < 4096
    assert "report" not in reduced
    assert reduced["omitted_scalar_count"] == 1
    assert reduced["assumptions"] == payload["assumptions"]
    assert "parts_not_inline" not in reduced
    assert "omitted_scalar_count" in handle.fetch_more.drill_in.rationale
    assert await _path_read(store, handle.handle_id, "/report") == payload["report"]
    assert await _path_read(store, handle.handle_id, "/assumptions") == payload["assumptions"]


async def test_a_moved_out_value_still_leaves_the_parts_named_when_they_do_not_fit() -> None:
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = {
        "items": [{"server": f"srv-{i}", "cores": 4, "fit": "ok"} for i in range(60)],
        "report": "r" * 3850,
        "inputs": {"servers": [f"srv-{i}" for i in range(500)]},
    }
    context = _context(result_scalars={"keys": ["report"]})

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, context)

    assert handle is not None
    assert json_text_bytes(reduced) < 4096
    assert "report" not in reduced
    assert reduced["omitted_scalar_count"] == 1
    assert reduced["parts_not_inline"] == ["inputs"]
    assert reduced["parts_note"] in handle.fetch_more.drill_in.rationale
    assert await _path_read(store, handle.handle_id, "/report") == payload["report"]
    assert await _path_read(store, handle.handle_id, "/inputs") == payload["inputs"]


async def test_the_limit_holds_without_parts_too() -> None:
    """A hinted scalar over the limit is moved out and stays readable."""
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    payload = {
        "items": [{"i": i} for i in range(60)],
        "id": "task-1",
        "log": "l" * 5000,
    }
    context = _context(result_scalars={"keys": ["id", "log"]})

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, context)

    assert handle is not None
    assert json_text_bytes(reduced) < 4096
    assert reduced["id"] == "task-1"
    assert "log" not in reduced
    assert reduced["omitted_scalar_count"] == 1
    assert await _path_read(store, handle.handle_id, "/log") == payload["log"]


def test_fit_inline_takes_the_largest_plain_value_first_and_places_parts_again() -> None:
    summary = {"small": "s", "big": "b" * 3900, "row_count": 60, "total": 60}
    parts = {"totals": {"n": 60}}

    fitted, displaced, not_inline = reducer_module._fit_inline(
        summary, parts, movable=frozenset({"small", "big"})
    )

    assert displaced == {"big": "b" * 3900}
    assert not_inline == []
    assert fitted["small"] == "s"
    assert fitted["totals"] == {"n": 60}
    assert fitted["omitted_scalar_count"] == 1
    assert json_text_bytes(fitted) < reducer_module._INLINE_SUMMARY_BYTE_LIMIT


async def test_a_digest_list_moves_out_and_is_named_when_it_fills_the_summary() -> None:
    """The review's example: a task digest of 60 rows with one 3,800-character failure.

    The ``failed`` digest list alone made a 4,034-byte summary; naming the
    ``milestones`` part took it to 4,225 bytes. Now the list moves out, is
    named under ``parts_not_inline`` and stored with the rows, and the part
    fits inline.
    """
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    rows = [{"name": f"task-{i}", "status": "COMPLETED"} for i in range(59)]
    rows.append({"name": "task-59", "status": "FAILED", "errors": [{"message": "e" * 3800}]})
    payload = {"subTasks": rows, "milestones": {"current": "deploy", "done": 3}}
    context = _context(
        result_digest={
            "collection": "subTasks",
            "status_field": "status",
            "failed_states": ["FAILED"],
        }
    )

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, context)

    assert handle is not None
    assert json_text_bytes(reduced) < 4096
    assert reduced["status_counts"] == {"COMPLETED": 59, "FAILED": 1}
    assert "failed" not in reduced
    assert "omitted_scalar_count" not in reduced
    assert reduced["milestones"] == payload["milestones"]
    assert reduced["parts_not_inline"] == ["failed"]
    assert reduced["parts_note"] in handle.fetch_more.drill_in.rationale
    assert await _path_read(store, handle.handle_id, "/failed") == [rows[59]]
    assert await _path_read(store, handle.handle_id, "/milestones") == payload["milestones"]


async def test_a_hinted_object_projection_moves_out_and_the_whole_object_stays_readable() -> None:
    """A ``result_objects`` projection near its own 4,096-byte budget fills the summary.

    The projection moves out whole, is named under ``parts_not_inline``, and
    the ``path`` read returns the complete object, which the spill stores
    anyway. The part that was left out before now fits.
    """
    store = ResultHandleStore(_Valkey())  # type: ignore[arg-type]
    # Four of these fields fill the projection's 4,096-byte budget exactly.
    whole = {f"field_{i}": "v" * 997 for i in range(8)}
    payload = {"items": [{"i": i} for i in range(60)], "config": whole, "totals": {"n": 60}}
    context = _context(result_objects={"objects": {"config": list(whole)}})

    reduced, handle = await JsonFluxReducer(store=store).reduce(payload, None, context)

    assert handle is not None
    assert json_text_bytes(reduced) < 4096
    assert "config" not in reduced
    assert "result_object_truncations" not in reduced
    assert "result_object_note" not in reduced
    assert reduced["totals"] == {"n": 60}
    assert reduced["parts_not_inline"] == ["config"]
    assert await _path_read(store, handle.handle_id, "/config") == whole


def test_fit_inline_names_a_moved_object_and_drops_its_truncation_note() -> None:
    summary = {
        "config": {"text": "c" * 3900},
        "result_object_truncations": {"config": ["text"]},
        "row_count": 60,
        "total": 60,
    }

    fitted, displaced, not_inline = reducer_module._fit_inline(
        summary, {"totals": {"n": 60}}, movable=frozenset({"config"})
    )

    assert displaced == {"config": summary["config"]}
    assert not_inline == ["config"]
    assert fitted["parts_not_inline"] == ["config"]
    assert fitted["totals"] == {"n": 60}
    assert "result_object_truncations" not in fitted
    assert "omitted_scalar_count" not in fitted
    assert json_text_bytes(fitted) < reducer_module._INLINE_SUMMARY_BYTE_LIMIT


def test_fit_inline_names_a_moved_list_before_the_parts_that_do_not_fit() -> None:
    summary = {"failed": [{"e": "x" * 3900}], "note": "n", "row_count": 60, "total": 60}
    parts = {"inputs": {"servers": [f"srv-{i}" for i in range(500)]}}

    fitted, displaced, not_inline = reducer_module._fit_inline(
        summary, parts, movable=frozenset({"failed", "note"})
    )

    assert displaced == {"failed": summary["failed"]}
    assert not_inline == ["failed", "inputs"]
    assert fitted["note"] == "n"
    assert "omitted_scalar_count" not in fitted
    assert json_text_bytes(fitted) < reducer_module._INLINE_SUMMARY_BYTE_LIMIT


def test_fit_inline_stops_when_nothing_can_move() -> None:
    """With nothing movable the summary is sent as is; a part that does not fit is still named."""
    summary = {"failed": [{"e": "x" * 4000}], "row_count": 60, "total": 60}

    fitted, displaced, not_inline = reducer_module._fit_inline(
        summary, {"totals": {"n": 60}}, movable=frozenset()
    )

    assert displaced == {}
    assert not_inline == ["totals"]
    assert fitted["parts_not_inline"] == ["totals"]
    assert fitted["failed"] == summary["failed"]
