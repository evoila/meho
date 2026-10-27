# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Document records and the RFC 6901 ``path`` reader (#3633).

An oversized result with no single paginable collection (a detail object
with several lists, a dict of scalars, a scalar root) used to ship whole
inline with no handle. These tests pin the new contract end to end:

* the reducer keeps such a payload whole as a document record under a
  handle with ``drill_in.available`` and a ``path`` example;
* retention is bounded: the per-record byte cap and the admission guard
  turn an oversized document into an unavailable handle, never a partial
  or unbounded copy; credential-class ops keep passing through unchanged;
* ``result_query`` with ``query={"path": ...}`` resolves RFC 6901 against
  the retained document: an array pages with ``offset`` / ``limit``, any
  other value comes back in ``value``;
* every failure is a typed, recoverable error: a ``ValidationError`` for a
  malformed pointer or a ``path`` mixed with relational fields, and a
  ``QueryContractError`` for a pointer that does not resolve or a read that
  does not match the handle's record kind.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import msgspec
import pytest
from pydantic import ValidationError

import meho_backplane.operations.result_query as rq
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    ResultRecordKindError,
    SpillResult,
    _StoredPayload,
)
from meho_backplane.operations.result_document import document_outline, json_text_bytes
from meho_backplane.operations.result_query import (
    QueryContractError,
    ResultHandleNotFoundError,
    ResultQueryOutputTooLargeError,
    ResultQuerySpec,
    read_result_window,
    run_result_query,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
_CONTEXT: dict[str, Any] = {
    "op_id": "k8s.pod.info",
    "operator_sub": "op-a",
    "tenant_id": str(_TENANT),
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()


class _FakeRedis:
    """In-memory async Valkey double (``set`` / ``get`` only)."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


@pytest.fixture
def redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def store(redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    """A real store over the fake client, also used by the read core."""
    real = ResultHandleStore(redis)  # type: ignore[arg-type]
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: real)
    return real


def _operator(sub: str = "op-a") -> Operator:
    return Operator(
        sub=sub,
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _detail_object() -> dict[str, Any]:
    """A multi-list detail object well over the 4 KB byte threshold."""
    return {
        "name": "payments-api",
        "phase": "Running",
        "restart_count": 2,
        "containers": [
            {"name": f"c{i}", "image": "registry/app:1." + "x" * 300} for i in range(20)
        ],
        "conditions": [{"type": f"T{i}", "status": "True"} for i in range(20)],
        "labels": {"app": "payments", "a/b~c": "escaped"},
        "empty": [],
    }


async def _document_handle(
    reducer_cls: type[Any], store: ResultHandleStore, payload: Any
) -> tuple[Any, Any]:
    reducer = reducer_cls(store=store)
    return await reducer.reduce(payload, None, dict(_CONTEXT))


# ---------------------------------------------------------------------------
# Reducer: oversized non-collection roots become document handles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "root_type"),
    [
        (_detail_object(), "object"),
        ({f"key_{i}": "v" * 100 for i in range(80)}, "object"),
        ("z" * 10_000, "string"),
    ],
    ids=["multi-list-detail", "dict-of-scalars", "scalar-root"],
)
async def test_oversized_non_collection_root_gets_a_drillable_handle(
    result_reducer_cls: type[Any], store: ResultHandleStore, payload: Any, root_type: str
) -> None:
    reduced, handle = await _document_handle(result_reducer_cls, store, payload)

    assert handle is not None, "was handle=None before #3633"
    assert handle.fetch_more is not None
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is True
    assert drill_in.mcp_tool == "result_query"
    assert drill_in.example_call is not None
    assert drill_in.example_call["args"]["handle_id"] == str(handle.handle_id)
    assert "path" in drill_in.example_call["args"]["query"]
    assert handle.sample_rows is None
    assert reduced["result_record"] == "document"
    assert reduced["root_type"] == root_type
    assert reduced["document_bytes"] > 4096
    assert handle.schema_["x-result-record"] == "document"
    stored = await store.fetch_document(
        tenant_id=_TENANT, operator_sub="op-a", handle_id=handle.handle_id
    )
    assert stored is not None
    assert stored.document == payload


async def test_keep_whole_hint_stores_a_collection_payload_as_a_document(
    result_reducer_cls: type[Any], store: ResultHandleStore
) -> None:
    """An op may ask for its whole payload, even when a list sits under an envelope key."""
    payload = {
        "items": [{"name": f"server-{i}", "why": "cheapest " * 20} for i in range(60)],
        "totals": {"monthly": 2655.0},
        "unknowns": [{"what": "size", "reason": "too big"}],
        "version": "1",
    }
    reducer = result_reducer_cls(store=store)
    context = {**_CONTEXT, "result_document": {"keep_whole": True}}

    reduced, handle = await reducer.reduce(payload, None, context)

    assert handle is not None
    assert reduced["result_record"] == "document"
    assert reduced["version"] == "1"
    stored = await store.fetch_document(
        tenant_id=_TENANT, operator_sub="op-a", handle_id=handle.handle_id
    )
    assert stored is not None
    assert stored.document == payload

    # Without the hint the same payload reduces on ``items`` and keeps rows.
    _rows_reduced, rows_handle = await reducer.reduce(payload, None, dict(_CONTEXT))
    assert rows_handle is not None
    assert rows_handle.total_rows == 60

    # A hint of the wrong shape is ignored, not an error.
    _ignored, ignored_handle = await reducer.reduce(
        payload, None, {**_CONTEXT, "result_document": {"keep_whole": "yes"}}
    )
    assert ignored_handle is not None
    assert ignored_handle.total_rows == 60


async def test_detail_summary_keeps_scalars_and_outlines_fields(
    result_reducer_cls: type[Any], store: ResultHandleStore
) -> None:
    reduced, handle = await _document_handle(result_reducer_cls, store, _detail_object())

    assert reduced["name"] == "payments-api"
    assert reduced["phase"] == "Running"
    assert reduced["restart_count"] == 2
    assert "containers" not in reduced, "lists are outlined, not copied inline"
    assert reduced["top_level_fields"]["containers"] == "array (20 items)"
    assert reduced["top_level_fields"]["labels"] == "object (2 keys)"
    assert reduced["top_level_field_count"] == 7
    assert handle.fetch_more.drill_in.example_call["args"]["query"] == {"path": "/name"}


@pytest.mark.parametrize(
    "payload",
    [
        {f"key_{i:02d}": "v" * 200 for i in range(30)},
        {
            "name": "payments-api",
            "phase": "Running",
            "log": "L" * 5_000,
            "containers": [1],
            "volumes": [2],
        },
    ],
    ids=["many-medium-strings", "one-large-string"],
)
async def test_document_summary_stays_under_the_threshold(
    result_reducer_cls: type[Any], store: ResultHandleStore, payload: dict[str, Any]
) -> None:
    reduced, handle = await _document_handle(result_reducer_cls, store, payload)

    assert handle is not None
    inline_bytes = len(msgspec.json.encode(reduced))
    assert inline_bytes < 4096
    assert inline_bytes < reduced["document_bytes"]
    assert reduced["omitted_scalar_count"] >= 1
    if "log" in payload:
        assert "log" not in reduced, "a 5,000-char string must not ride inline"
        assert reduced["name"] == "payments-api"


def _escaped_name(i: int) -> str:
    """A valid field name full of control characters: one byte raw, six as JSON."""
    return "\x01" * 100 + f"{i:03d}"


def test_outline_budget_counts_escaped_json_bytes() -> None:
    """The outline measures JSON text, so escaped names cannot blow its budget."""
    document = {_escaped_name(i): "v" * 50 for i in range(64)}

    outline = document_outline(document)

    assert outline, "names under the length limit are still outlined"
    assert len(outline) < 64, "the byte budget stops the outline before the field cap"
    assert json_text_bytes(outline) <= 2048
    assert len(json.dumps(outline)) <= 2048 + 2 * len(outline)  # default separators add spaces


async def test_document_summary_with_escaped_field_names_stays_under_the_threshold(
    result_reducer_cls: type[Any], store: ResultHandleStore
) -> None:
    """Escaped field names and values do not push the inline summary past 4 KB."""
    payload = {_escaped_name(i): "\x02" * 60 for i in range(64)}

    reduced, handle = await _document_handle(result_reducer_cls, store, payload)

    assert handle is not None
    assert reduced["result_record"] == "document"
    assert reduced["top_level_field_count"] == 64
    assert len(json.dumps(reduced)) < 4096, "measured as the MCP transport sends it"
    assert len(json.dumps(reduced)) < reduced["document_bytes"]


async def test_small_non_collection_root_still_passes_through(
    result_reducer_cls: type[Any], store: ResultHandleStore, redis: _FakeRedis
) -> None:
    payload = {"name": "small", "containers": [1], "volumes": [2]}

    reduced, handle = await _document_handle(result_reducer_cls, store, payload)

    assert handle is None
    assert reduced is payload
    assert redis.values == {}


async def test_credential_class_document_passes_through_unchanged(
    result_reducer_cls: type[Any], store: ResultHandleStore, redis: _FakeRedis
) -> None:
    payload = {f"key_{i}": "s" * 100 for i in range(80)}
    reducer = result_reducer_cls(store=store)

    reduced, handle = await reducer.reduce(payload, None, {**_CONTEXT, "op_id": "vault.kv.read"})

    assert handle is None
    assert reduced is payload
    assert redis.values == {}, "a credential-class payload must not be copied to the store"


async def test_document_over_the_record_cap_is_not_stored(
    result_reducer_cls: type[Any],
    store: ResultHandleStore,
    redis: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "5000")
    get_settings.cache_clear()
    payload = {"blob": "q" * 20_000, "name": "big"}

    reduced, handle = await _document_handle(result_reducer_cls, store, payload)

    assert handle is not None
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is False
    assert drill_in.reason == "result_store_unavailable"
    assert redis.values == {}, "an over-cap document is kept whole or not at all"
    assert reduced["name"] == "big"


async def test_document_over_the_admission_limit_is_not_stored(
    result_reducer_cls: type[Any], store: ResultHandleStore, redis: _FakeRedis
) -> None:
    # The guard class, taken from a default reducer so this suite needs no
    # extra import path.
    guard_cls = type(result_reducer_cls()._resolve_admission_guard())
    reducer = result_reducer_cls(store=store, admission_guard=guard_cls(max_nodes=5))
    payload = {"a": [1, 2], "b": [3, 4], "c": "x" * 5000}

    reduced, handle = await reducer.reduce(payload, None, dict(_CONTEXT))

    assert handle is not None
    assert handle.fetch_more.drill_in.available is False
    assert handle.fetch_more.drill_in.reason == "admission_limit_exceeded"
    assert reduced["status"] == "unprofiled"
    assert "c" not in reduced
    assert redis.values == {}


async def test_document_without_tenant_context_is_unavailable(
    result_reducer_cls: type[Any], store: ResultHandleStore, redis: _FakeRedis
) -> None:
    reducer = result_reducer_cls(store=store)

    _reduced, handle = await reducer.reduce(_detail_object(), None, {"op_id": "k8s.pod.info"})

    assert handle is not None
    assert handle.fetch_more.drill_in.reason == "no_tenant_context"
    assert redis.values == {}


# ---------------------------------------------------------------------------
# Store: the document record is additive
# ---------------------------------------------------------------------------


def test_record_written_before_documents_decodes_as_rows() -> None:
    raw = msgspec.json.encode(
        {
            "operator_sub": "op",
            "op_id": None,
            "rows": [{"i": 1}],
            "total_rows": 1,
            "stored_rows": 1,
            "created_at": "x",
        }
    )

    decoded = msgspec.json.decode(raw, type=_StoredPayload)

    assert decoded.record_kind == "rows"
    assert decoded.document is None


async def test_document_record_rejects_row_reads(
    store: ResultHandleStore, redis: _FakeRedis
) -> None:
    handle_id = uuid.uuid4()
    assert await store.spill_document(
        tenant_id=_TENANT,
        operator_sub="op-a",
        handle_id=handle_id,
        op_id="x.info",
        document={"a": 1},
        ttl_seconds=60,
    )

    with pytest.raises(ResultRecordKindError, match="path"):
        await store.fetch_rows(tenant_id=_TENANT, operator_sub="op-a", handle_id=handle_id)
    with pytest.raises(ResultRecordKindError):
        await store.fetch_window(
            tenant_id=_TENANT, operator_sub="op-a", handle_id=handle_id, offset=0, limit=5
        )
    # Another operator gets the same miss as an unknown handle.
    assert (
        await store.fetch_document(tenant_id=_TENANT, operator_sub="op-b", handle_id=handle_id)
        is None
    )


# ---------------------------------------------------------------------------
# Spec: path syntax and mutual exclusion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relational",
    [
        {"filter": [{"field": "a", "op": "=", "value": 1}]},
        {"select": ["a"]},
        {"group_by": ["a"]},
        {"aggregate": [{"func": "COUNT"}]},
        {"order_by": [{"field": "a"}]},
        {"limit": 5},
    ],
    ids=["filter", "select", "group_by", "aggregate", "order_by", "limit"],
)
def test_path_with_any_relational_field_is_a_validation_error(relational: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="cannot be combined"):
        ResultQuerySpec.model_validate({"path": "/a", **relational})


@pytest.mark.parametrize("pointer", ["a/b", "/a~2b", "/a~", "/~x"])
def test_malformed_pointer_is_a_validation_error(pointer: str) -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(path=pointer)


def test_over_long_pointer_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        ResultQuerySpec(path="/" + "a" * 2000)


@pytest.mark.parametrize("pointer", ["", "/", "/a/0", "/a~0b/c~1d"])
def test_well_formed_pointer_builds_a_spec(pointer: str) -> None:
    assert ResultQuerySpec(path=pointer).path == pointer


# ---------------------------------------------------------------------------
# Core: run_result_query with ``path``
# ---------------------------------------------------------------------------

_DOCUMENT: dict[str, Any] = {
    "name": "pod-1",
    "spec": {"containers": [{"name": f"c{i}"} for i in range(7)]},
    "lines": ["a", "b", "c"],
    "a/b": {"m~n": "escaped"},
    "": "empty key",
    "flag": False,
}


async def _seed_document(store: ResultHandleStore, document: Any = _DOCUMENT) -> uuid.UUID:
    handle_id = uuid.uuid4()
    assert await store.spill_document(
        tenant_id=_TENANT,
        operator_sub="op-a",
        handle_id=handle_id,
        op_id="k8s.pod.info",
        document=document,
        ttl_seconds=60,
    )
    return handle_id


async def test_path_to_an_array_pages_with_offset_and_limit(store: ResultHandleStore) -> None:
    handle_id = await _seed_document(store)

    page = await run_result_query(
        _operator(), handle_id, ResultQuerySpec(path="/spec/containers"), offset=2, limit=3
    )

    assert page["value_type"] == "array"
    assert page["rows"] == [{"name": "c2"}, {"name": "c3"}, {"name": "c4"}]
    assert page["returned_rows"] == 3
    assert page["total_rows"] == page["stored_rows"] == 7
    assert page["offset"] == 2
    assert page["limit"] == 3
    assert page["coverage"] == "complete"
    assert page["truncated"] is False
    assert "value" not in page


async def test_array_of_scalars_pages_as_value_rows(store: ResultHandleStore) -> None:
    handle_id = await _seed_document(store)

    page = await run_result_query(_operator(), handle_id, ResultQuerySpec(path="/lines"))

    assert page["rows"] == [{"value": "a"}, {"value": "b"}, {"value": "c"}]


async def test_paging_past_the_end_returns_no_rows(store: ResultHandleStore) -> None:
    handle_id = await _seed_document(store)

    page = await run_result_query(
        _operator(), handle_id, ResultQuerySpec(path="/lines"), offset=10, limit=5
    )

    assert page["rows"] == []
    assert page["total_rows"] == 3


@pytest.mark.parametrize(
    ("pointer", "expected", "value_type"),
    [
        ("/name", "pod-1", "string"),
        ("/spec/containers/6", {"name": "c6"}, "object"),
        ("/spec/containers/0/name", "c0", "string"),
        ("/a~1b/m~0n", "escaped", "string"),
        ("/", "empty key", "string"),
        ("/flag", False, "boolean"),
    ],
)
async def test_path_to_a_non_array_returns_the_single_value(
    store: ResultHandleStore, pointer: str, expected: Any, value_type: str
) -> None:
    handle_id = await _seed_document(store)

    result = await run_result_query(_operator(), handle_id, ResultQuerySpec(path=pointer))

    assert result["value"] == expected
    assert result["value_type"] == value_type
    assert result["rows"] == []
    assert result["path"] == pointer


async def test_empty_path_is_the_whole_document(store: ResultHandleStore) -> None:
    handle_id = await _seed_document(store, {"k": [1, 2]})

    result = await run_result_query(_operator(), handle_id, ResultQuerySpec(path=""))

    assert result["value"] == {"k": [1, 2]}


async def test_scalar_root_document_reads_with_empty_path(store: ResultHandleStore) -> None:
    handle_id = await _seed_document(store, "plain text")

    result = await run_result_query(_operator(), handle_id, ResultQuerySpec(path=""))

    assert result["value"] == "plain text"


@pytest.mark.parametrize(
    ("pointer", "message"),
    [
        ("/missing", "no such member"),
        ("/spec/containers/7", "7 element"),
        ("/spec/containers/01", "leading zeros"),
        ("/spec/containers/-", '"-"'),
        ("/spec/containers/x", "not a valid array index"),
        ("/name/first", "not an object or array"),
    ],
)
async def test_pointer_that_does_not_resolve_is_a_contract_error(
    store: ResultHandleStore, pointer: str, message: str
) -> None:
    handle_id = await _seed_document(store)

    with pytest.raises(QueryContractError, match=message):
        await run_result_query(_operator(), handle_id, ResultQuerySpec(path=pointer))


async def test_path_on_a_rows_handle_is_a_contract_error(store: ResultHandleStore) -> None:
    handle_id = uuid.uuid4()
    stored = (
        await store.spill(
            tenant_id=_TENANT,
            operator_sub="op-a",
            handle_id=handle_id,
            op_id="x.list",
            rows=[{"i": 1}],
            total_rows=1,
            ttl_seconds=60,
            max_rows=10,
        )
    ).stored_rows
    assert stored == 1

    with pytest.raises(QueryContractError, match="holds rows"):
        await run_result_query(_operator(), handle_id, ResultQuerySpec(path="/0"))


async def test_path_reads_a_field_the_rows_summary_left_out(
    result_reducer_cls: type[Any], store: ResultHandleStore
) -> None:
    """An omitted scalar beside a row list is read back with ``path`` (#3633).

    ``stderr`` is over the per-value cap of the inline copy, so the summary
    only counts it. The same ``result_query`` call that reads a document
    handle returns it from the rows handle; the rows themselves still page.
    """
    reducer = result_reducer_cls(store=store)
    stderr = "e" * 600
    payload = {"stdout": [{"line": f"l{i}"} for i in range(60)], "stderr": stderr, "exit_code": 1}

    reduced, handle = await reducer.reduce(payload, None, dict(_CONTEXT, op_id="k8s.exec"))

    assert handle is not None
    assert "stderr" not in reduced
    assert reduced["omitted_scalar_count"] == 1
    assert '"path": "/<field name>"' in handle.fetch_more.drill_in.rationale

    one = await run_result_query(_operator(), handle.handle_id, ResultQuerySpec(path="/stderr"))
    assert one["value"] == stderr
    assert one["value_type"] == "string"

    everything = await run_result_query(_operator(), handle.handle_id, ResultQuerySpec(path=""))
    assert everything["value"] == {"stderr": stderr}

    window = await read_result_window(_operator(), handle.handle_id, offset=0, limit=5)
    assert window["returned_rows"] == 5
    assert window["total_rows"] == 60

    with pytest.raises(QueryContractError, match="does not resolve"):
        await run_result_query(_operator(), handle.handle_id, ResultQuerySpec(path="/exit_code"))


async def test_omitted_fields_over_the_record_cap_are_dropped_but_rows_stay(
    store: ResultHandleStore,
) -> None:
    """Omitted fields never cost the caller its rows: too big for the cap, they go."""
    handle_id = uuid.uuid4()

    spilled = await store.spill(
        tenant_id=_TENANT,
        operator_sub="op-a",
        handle_id=handle_id,
        op_id="x.list",
        rows=[{"i": 1}, {"i": 2}],
        total_rows=2,
        ttl_seconds=60,
        max_rows=10,
        max_record_bytes=2_000,
        document={"big": "b" * 5_000},
    )

    assert spilled == SpillResult(stored_rows=2, document_stored=False)
    window = await read_result_window(_operator(), handle_id, offset=0, limit=5)
    assert window["returned_rows"] == 2
    with pytest.raises(QueryContractError, match="holds rows"):
        await run_result_query(_operator(), handle_id, ResultQuerySpec(path="/big"))


async def test_omitted_fields_that_leave_no_room_for_a_row_are_dropped(
    store: ResultHandleStore,
) -> None:
    """Boundary: the omitted fields fit the cap alone but not beside the first row.

    The rows store fine on their own. Adding fields that fit the cap by
    themselves used to leave too little room for the first row, so nothing
    was stored at all. Now the store retries without the fields, keeps the
    rows, and reports the fields as not stored.
    """
    cap = 2_000
    rows = [{"v": "r" * 200} for _ in range(3)]
    fields = {"note": "n" * 1_600}
    created_at = "2026-01-01T00:00:00.000001+00:00"
    with_fields = _StoredPayload(
        "op-a", "x.list", [], 3, 0, created_at, 3, "partial", document=fields
    )
    assert len(msgspec.json.encode(with_fields)) <= cap
    first_row = msgspec.structs.replace(with_fields, rows=rows[:1], stored_rows=1)
    assert len(msgspec.json.encode(first_row)) > cap

    def _spill(handle_id: uuid.UUID, document: dict[str, Any] | None) -> Any:
        return store.spill(
            tenant_id=_TENANT,
            operator_sub="op-a",
            handle_id=handle_id,
            op_id="x.list",
            rows=rows,
            total_rows=3,
            ttl_seconds=60,
            max_rows=10,
            max_record_bytes=cap,
            document=document,
        )

    assert await _spill(uuid.uuid4(), None) == SpillResult(stored_rows=3, document_stored=False)

    handle_id = uuid.uuid4()
    assert await _spill(handle_id, fields) == SpillResult(stored_rows=3, document_stored=False)
    window = await read_result_window(_operator(), handle_id, offset=0, limit=5)
    assert window["returned_rows"] == 3
    with pytest.raises(QueryContractError, match="holds rows"):
        await run_result_query(_operator(), handle_id, ResultQuerySpec(path="/note"))


async def test_rationale_does_not_promise_a_path_read_the_store_dropped(
    result_reducer_cls: type[Any], store: ResultHandleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a sibling too large for the record cap is reported as lost.

    The rows are stored and the handle pages, but the omitted field was not
    kept beside them. The drill-in rationale must say so instead of naming a
    ``path`` read that fails.
    """
    monkeypatch.setenv("RESULT_HANDLE_MAX_RECORD_BYTES", "2000")
    get_settings.cache_clear()
    reducer = result_reducer_cls(store=store)
    payload = {
        "stdout": [{"line": f"l{i}"} for i in range(60)],
        "stderr": "e" * 5_000,
        "exit_code": 1,
    }

    reduced, handle = await reducer.reduce(payload, None, dict(_CONTEXT, op_id="k8s.exec"))

    assert handle is not None
    assert "stderr" not in reduced
    assert reduced["omitted_scalar_count"] == 1
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is True
    assert "1 top-level field(s) did not fit the inline summary" in drill_in.rationale
    assert "not retrievable from this handle" in drill_in.rationale
    assert '"path": "/<field name>"' not in drill_in.rationale

    window = await read_result_window(_operator(), handle.handle_id, offset=0, limit=5)
    assert window["returned_rows"] == 5
    assert window["total_rows"] == 60
    with pytest.raises(QueryContractError, match="holds rows"):
        await run_result_query(_operator(), handle.handle_id, ResultQuerySpec(path="/stderr"))


async def test_relational_query_on_a_document_is_a_contract_error(
    store: ResultHandleStore,
) -> None:
    handle_id = await _seed_document(store)

    with pytest.raises(QueryContractError, match="holds one document"):
        await run_result_query(_operator(), handle_id, ResultQuerySpec(select=["name"]))


async def test_paging_a_document_is_a_contract_error(store: ResultHandleStore) -> None:
    handle_id = await _seed_document(store)

    with pytest.raises(QueryContractError, match="holds one document"):
        await read_result_window(_operator(), handle_id, 0, 10)


async def test_path_read_of_another_operators_document_is_not_found(
    store: ResultHandleStore,
) -> None:
    handle_id = await _seed_document(store)

    with pytest.raises(ResultHandleNotFoundError):
        await run_result_query(_operator("op-b"), handle_id, ResultQuerySpec(path="/name"))


async def test_path_read_over_the_output_budget_is_recoverable(
    store: ResultHandleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESULT_QUERY_MAX_OUTPUT_BYTES", "64")
    get_settings.cache_clear()
    handle_id = await _seed_document(store, {"big": "w" * 500})

    with pytest.raises(ResultQueryOutputTooLargeError, match="Point `path`"):
        await run_result_query(_operator(), handle_id, ResultQuerySpec(path="/big"))
