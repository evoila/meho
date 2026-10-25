# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Seeded property tests for result reduction over admitted JSON.

Each seed builds one set of captured JSON rows with ``random.Random(seed)``
and checks a property across several row orders of that same set. The rows
mix every case the field catalog cares about: missing keys, nulls, mixed
JSON kinds, whole and decimal numbers, integers past 64 bits, objects,
lists, non-ASCII text, and two keys that differ only by case.

Two properties are checked:

* The field catalog does not depend on row order: the same field names,
  JSON kinds, queryability, and schema order (by field name) come back.
* Reading the stored rows back returns the captured values byte for byte,
  whatever the row order: paging, a flat query with no arguments, and a
  ``select`` of fields the catalog cannot query.

The generator is the standard library only, so a failing seed replays
exactly with no extra dependency.
"""

from __future__ import annotations

import random
import types
import uuid
from collections.abc import Iterator
from typing import Any

import msgspec
import pytest

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.jsonflux.query.result_catalog import (
    AdmissionGuard,
    ResultFieldCatalog,
    build_catalog,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryResultStore

_SEEDS = tuple(range(16))
_PERMUTATIONS_PER_SEED = 4
_PAGE_LIMIT = 7
_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a7")
_SUB = "op-properties"

#: Fields the catalog can never query, whatever the seed draws.
_RAW_ONLY_FIELDS = ("flex", "huge", "nested", "tags")

_TEXT = ("alpha", "Beta", "gamma delta", "żółw", "ключ", "名前", 'a"b\\c', "tab\tnew\nline", "")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires; the reducer reads settings."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _maybe(rng: random.Random, value: Any) -> Any:
    """Return *value*, or ``None`` one time in six."""
    return None if rng.random() < 1 / 6 else value


def _row(rng: random.Random, index: int) -> dict[str, Any]:
    """One captured row. ``id`` is always present; the rest may be missing."""
    candidates: dict[str, Any] = {
        "name": rng.choice(_TEXT),
        "Name": rng.choice(_TEXT).upper(),
        "count": _maybe(rng, rng.randint(-1000, 1000)),
        "ratio": rng.choice([0, 3, -7, 0.5, 12.25, -1e-3, 1.5e10]),
        "enabled": _maybe(rng, rng.random() < 0.5),
        "flex": rng.choice([7, "seven", True, 7.5]),
        "huge": rng.choice([2**64 + index, -(2**63) - 1 - index]),
        "nested": {"level": index, "labels": {"k": rng.choice(_TEXT)}, "list": [index, None]},
        "tags": [rng.choice(_TEXT) for _ in range(rng.randint(0, 3))],
        "nothing": None,
    }
    row: dict[str, Any] = {"id": f"row-{index:03d}"}
    for key, value in candidates.items():
        if rng.random() < 0.8:
            row[key] = value
    # The raw-only fields must each appear at least once in the set.
    if index == 0:
        for key in _RAW_ONLY_FIELDS:
            row.setdefault(key, candidates[key])
        row["flex"] = 7
    if index == 1:
        row["flex"] = "seven"
    return row


def _admitted_rows(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = [_row(rng, index) for index in range(rng.randint(12, 60))]
    AdmissionGuard().require(rows)
    return rows


def _permutations(seed: int, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rng = random.Random(seed * 7919 + 1)
    orders = [list(rows), list(reversed(rows))]
    while len(orders) < _PERMUTATIONS_PER_SEED:
        shuffled = list(rows)
        rng.shuffle(shuffled)
        orders.append(shuffled)
    return orders


def _profile(catalog: ResultFieldCatalog) -> list[tuple[Any, ...]]:
    return [
        (
            field.name,
            field.column,
            field.kinds,
            field.nullable,
            field.queryable,
            field.reason,
            field.scalar_family,
        )
        for field in catalog.fields
    ]


def _operator() -> Operator:
    return Operator(
        sub=_SUB,
        name="Property Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _context() -> dict[str, Any]:
    return {"op_id": "example.list", "operator_sub": _SUB, "tenant_id": str(_TENANT)}


def _read_settings() -> Any:
    return types.SimpleNamespace(
        result_query_max_output_rows=500,
        result_query_max_output_bytes=262144,
        result_query_timeout_seconds=10,
        result_query_response_wire_max_bytes=2097152,
    )


def _encode(value: Any) -> bytes:
    return msgspec.json.encode(value)


@pytest.mark.parametrize("seed", _SEEDS)
async def test_catalog_is_invariant_under_row_permutation(seed: int) -> None:
    """Row order never changes the catalog or the handle schema it drives."""
    rows = _admitted_rows(seed)
    guard = AdmissionGuard()
    expected = _profile(build_catalog(rows, guard=guard))
    expected_names = [entry[0] for entry in expected]
    assert expected_names == sorted({key for row in rows for key in row})
    raw_only = {entry[0] for entry in expected if not entry[4]}
    assert set(_RAW_ONLY_FIELDS) <= raw_only

    reducer = JsonFluxReducer(row_threshold=0, store=MemoryResultStore(), max_spill_rows=10000)
    schemas: list[dict[str, Any]] = []
    for order in _permutations(seed, rows):
        assert _profile(build_catalog(order, guard=guard)) == expected
        _summary, handle = await reducer.reduce(order, None, _context())
        assert handle is not None
        schema = handle.model_dump(mode="json")["schema_"]
        assert list(schema["items"]["properties"]) == expected_names
        schemas.append(schema)

    assert all(_encode(schema) == _encode(schemas[0]) for schema in schemas)


@pytest.mark.parametrize("seed", _SEEDS)
async def test_raw_read_back_is_byte_equal_under_row_permutation(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paging, a flat query, and a raw-only ``select`` return the captured bytes."""
    rows = _admitted_rows(seed)
    operator = _operator()
    monkeypatch.setattr(rq, "get_settings", _read_settings)

    for order in _permutations(seed, rows):
        store = MemoryResultStore()
        monkeypatch.setattr(rq, "get_result_handle_store", lambda store=store: store)
        reducer = JsonFluxReducer(row_threshold=0, store=store, max_spill_rows=10000)
        _summary, handle = await reducer.reduce(order, None, _context())
        assert handle is not None
        assert handle.fetch_more.drill_in.available is True
        handle_id = handle.handle_id

        paged: list[dict[str, Any]] = []
        offset = 0
        while offset < len(order):
            window = await rq.read_result_window(operator, handle_id, offset, _PAGE_LIMIT)
            assert window["total_rows"] == window["stored_rows"] == len(order)
            paged.extend(window["rows"])
            offset += _PAGE_LIMIT
        assert _encode(paged) == _encode(order)

        flat = await rq.run_result_query(operator, handle_id, ResultQuerySpec())
        assert flat["coverage"] == "complete"
        assert flat["truncated"] is False
        assert _encode(flat["rows"]) == _encode(order)

        select = list(_RAW_ONLY_FIELDS)
        projected = await rq.run_result_query(operator, handle_id, ResultQuerySpec(select=select))
        expected = [{key: row[key] for key in select if key in row} for row in order]
        assert _encode(projected["rows"]) == _encode(expected)
