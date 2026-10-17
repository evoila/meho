# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Old result records still read safely after the result-cache move (#3642).

The result-cache move is reader-first: new code reads records that older
code wrote, from the old key on the shared store, until they expire. These
tests read such records through the real store codec
(``msgspec.json.decode(..., type=_StoredPayload)``), not a hand-built fake
of the decode:

* A record with fields the current struct does not know decodes cleanly.
  The unknown fields are dropped and the missing new fields get their
  defaults.
* A record over the 16 MiB per-record cap (older code capped rows only,
  not bytes) is checked by length before any decode. The read is a typed
  "handle not readable" miss: no decode, no exception, no raw rows.
"""

from __future__ import annotations

import uuid
from typing import Any

import msgspec
import pytest
import structlog

import meho_backplane.connectors.result_handle_store as store_module
import meho_backplane.operations.result_query as rq
import meho_backplane.operations.result_read_admission as admission
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    _key,
    _StoredPayload,
)
from meho_backplane.operations.result_query import ResultHandleNotFoundError
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000003642")
_SUB = "legacy-operator"
_CAP = 16 * 1024 * 1024


class _Valkey:
    """Dict-backed async client with ``get`` / ``set`` only."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


def _vm_row(index: int) -> dict[str, Any]:
    """One row shaped like a vCenter VM list item with its summary fields."""
    return {
        "vm": f"vm-{index}",
        "name": f"app-{index:05d}-web",
        "power_state": "POWERED_ON" if index % 3 else "POWERED_OFF",
        "cpu_count": 2 + index % 6,
        "memory_size_MiB": 4096 * (1 + index % 4),
        "guest_os": "OTHER_LINUX_64",
        "annotation": "service owner team, change ticket, backup policy " * 32,
        "tags": [f"tag-{n}" for n in range(index % 5)],
        "disks": [{"label": f"Hard disk {n}", "capacity": 42949672960} for n in range(2)],
    }


def _legacy_record(rows: list[dict[str, Any]], **unknown: Any) -> bytes:
    """Encode a record the way the first spill code wrote it.

    That writer had only the six base fields and capped rows, not bytes.
    *unknown* adds fields the current struct does not declare.
    """
    return msgspec.json.encode(
        {
            "operator_sub": _SUB,
            "op_id": "vmware-rest.vm.list",
            "rows": rows,
            "total_rows": len(rows),
            "stored_rows": len(rows),
            "created_at": "2026-05-01T08:00:00+00:00",
            **unknown,
        }
    )


def _operator() -> Operator:
    return Operator(
        sub=_SUB,
        name="Legacy Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    admission.shutdown_result_read_admission()
    yield
    admission.shutdown_result_read_admission()
    get_settings.cache_clear()


def _legacy_store(handle_id: uuid.UUID, record: bytes) -> ResultHandleStore:
    """A store whose record sits only under the old key on the shared client."""
    shared, dedicated = _Valkey(), _Valkey()
    shared.values[_key(_TENANT, handle_id)] = record
    return ResultHandleStore(shared, dedicated_client=dedicated)  # type: ignore[arg-type]


def test_unknown_legacy_fields_are_dropped_by_the_store_codec() -> None:
    rows = [_vm_row(i) for i in range(3)]
    raw = _legacy_record(
        rows,
        schema_version=1,
        spill_format="rows-v1",
        row_schema={"vm": "string", "cpu_count": "integer"},
    )

    payload = msgspec.json.decode(raw, type=_StoredPayload)

    assert payload.rows == rows
    assert payload.stored_rows == payload.total_rows == 3
    for name in ("schema_version", "spill_format", "row_schema"):
        assert not hasattr(payload, name)
        assert name not in msgspec.structs.asdict(payload)
    assert payload.record_kind == "rows"
    assert payload.document is None
    assert payload.storage_coverage == payload.source_coverage == "unknown"
    assert payload.captured_rows == 0
    assert payload.metadata == {}


async def test_legacy_record_with_unknown_fields_pages_through_the_fallback_read() -> None:
    handle_id = uuid.uuid4()
    rows = [_vm_row(i) for i in range(5)]
    store = _legacy_store(handle_id, _legacy_record(rows, schema_version=1, spill_format="v1"))

    window = await store.fetch_window(
        tenant_id=_TENANT, operator_sub=_SUB, handle_id=handle_id, offset=1, limit=2
    )

    assert window is not None
    assert window.rows == rows[1:3]
    assert window.total_rows == window.stored_rows == 5
    assert window.truncated is False


@pytest.fixture(scope="module")
def oversized_record() -> bytes:
    """A real record of 10000 rows, as the row-capped writer stored it."""
    raw = _legacy_record([_vm_row(i) for i in range(10_000)], schema_version=1)
    assert len(raw) > _CAP
    return raw


async def test_oversized_legacy_record_is_length_checked_before_decode(
    monkeypatch: pytest.MonkeyPatch, oversized_record: bytes
) -> None:
    handle_id = uuid.uuid4()
    store = _legacy_store(handle_id, oversized_record)
    decode_calls: list[int] = []
    real_decode = msgspec.json.decode

    def _spy(*args: Any, **kwargs: Any) -> Any:
        decode_calls.append(1)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(msgspec.json, "decode", _spy)
    # The module logger is cached after its first use. Give the store a
    # fresh logger so capture_logs sees its events in any test order.
    monkeypatch.setattr(store_module, "_log", structlog.get_logger(store_module.__name__))

    with structlog.testing.capture_logs() as logs:
        raw = await store.fetch_record(tenant_id=_TENANT, handle_id=handle_id)
        window = await store.fetch_window(
            tenant_id=_TENANT, operator_sub=_SUB, handle_id=handle_id, offset=0, limit=50
        )

    assert raw is None
    assert window is None
    assert decode_calls == []
    over_cap = [e for e in logs if e["event"] == "result_handle_record_over_cap"]
    assert len(over_cap) == 2
    assert over_cap[0]["record_bytes"] == len(oversized_record)
    assert over_cap[0]["max_record_bytes"] == _CAP
    assert "rows" not in over_cap[0]


@pytest.mark.parametrize("read", ["paging", "query", "path"])
async def test_oversized_legacy_record_surfaces_the_typed_not_readable_result(
    monkeypatch: pytest.MonkeyPatch, oversized_record: bytes, read: str
) -> None:
    handle_id = uuid.uuid4()
    store = _legacy_store(handle_id, oversized_record)
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)

    with pytest.raises(ResultHandleNotFoundError) as caught:
        if read == "paging":
            await rq.read_result_window(_operator(), handle_id, 0, 50)
        elif read == "query":
            await rq.run_result_query(_operator(), handle_id, rq.ResultQuerySpec())
        else:
            await rq.run_result_query(_operator(), handle_id, rq.ResultQuerySpec(path="/name"))

    assert caught.value.handle_id == handle_id
    assert "app-00000-web" not in str(caught.value)


async def test_production_store_reads_with_the_configured_record_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meho_backplane.broadcast.client as broadcast_client

    shared = _Valkey()
    handle_id = uuid.uuid4()
    raw = _legacy_record([_vm_row(i) for i in range(2)])
    shared.values[_key(_TENANT, handle_id)] = raw
    monkeypatch.delenv("RESULT_CACHE_REDIS_URL", raising=False)
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", str(len(raw) - 1))
    monkeypatch.setattr(broadcast_client, "get_broadcast_client", lambda: shared)
    get_settings.cache_clear()
    store_module.reset_result_handle_store_for_testing()
    try:
        store = store_module.get_result_handle_store()
        assert await store.fetch_record(tenant_id=_TENANT, handle_id=handle_id) is None
    finally:
        store_module.reset_result_handle_store_for_testing()
