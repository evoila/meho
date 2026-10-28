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
