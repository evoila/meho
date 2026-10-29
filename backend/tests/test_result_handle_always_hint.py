# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``result_handle: {"always": true}`` hint (#5143).

An operation whose result other operations take by its handle can ask for a
handle even when the result is small. These tests pin the contract with the
default reducer limits and a real result store on an in-memory stand-in for
Valkey:

* with the hint, a small result comes back whole and a handle to a stored
  copy comes next to it; ``result_query`` with ``path: ""`` returns the same
  answer;
* without the hint, nothing changes;
* a credential-class operation gets no handle and nothing is stored;
* the dispatcher forwards the hint, and the reply paths (the result envelope,
  the wire size check, the audit row metadata) carry a whole answer with a
  handle.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.operations import jsonflux_reducer as reducer_module
from meho_backplane.operations import result_query as rq
from meho_backplane.operations._errors import wrap_ok_result
from meho_backplane.operations.delivery_budget import (
    bound_complete_response,
    handle_is_readable,
)
from meho_backplane.operations.dispatcher import (
    _build_reducer_context,
    _handle_metadata_for_audit,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.result_query import ResultQuerySpec, run_result_query
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c143")
_SUB = "operator:fixture"
_HINT = {"always": True}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()


class _MemoryValkey:
    """The two calls of the async Valkey client the store uses."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


@pytest.fixture
def valkey(monkeypatch: pytest.MonkeyPatch) -> _MemoryValkey:
    memory = _MemoryValkey()
    store = ResultHandleStore(memory)  # type: ignore[arg-type]
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(reducer_module, "get_result_handle_store", lambda: store)
    return memory


def _operator() -> Operator:
    return Operator(
        sub=_SUB,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _context(op_id: str = "test.inventory.read", **hints: Any) -> dict[str, Any]:
    return {"op_id": op_id, "tenant_id": str(_TENANT), "operator_sub": _SUB, **hints}


def _padded(payload: dict[str, Any], size: int) -> dict[str, Any]:
    """Grow *payload*'s ``note`` until its JSON text is *size* bytes."""
    payload["note"] = ""
    payload["note"] = "x" * (size - len(json.dumps(payload, separators=(",", ":"))))
    return payload


def _rows_answer() -> dict[str, Any]:
    """One list of two rows: the reducer sees a collection under its limits."""
    return _padded({"items": [{"name": "vm-a"}, {"name": "vm-b"}]}, 1700)


def _document_answer() -> dict[str, Any]:
    """Two lists, like an inventory: the reducer sees one document under its limits."""
    return _padded(
        {
            "schema_version": "1",
            "records": [{"source_id": "fake:vm-1", "name": "app-01"}],
            "omissions": [],
            "counts": {"server": 1},
        },
        1700,
    )


@pytest.mark.parametrize("make_answer", [_rows_answer, _document_answer])
async def test_hint_returns_a_small_answer_whole_with_a_handle(
    valkey: _MemoryValkey, make_answer: Any
) -> None:
    answer = make_answer()
    assert len(json.dumps(answer, separators=(",", ":"))) == 1700

    result, handle = await JsonFluxReducer().reduce(answer, None, _context(result_handle=_HINT))

    assert result == answer
    assert handle is not None
    assert handle.total_rows is None
    assert handle.sample_rows is None
    assert handle.fetch_more is not None
    drill_in = handle.fetch_more.drill_in
    assert drill_in.available is True
    assert drill_in.example_call is not None
    assert drill_in.example_call["args"]["query"] == {"path": ""}
    read = await run_result_query(_operator(), handle.handle_id, ResultQuerySpec(path=""))
    assert read["value"] == answer


@pytest.mark.parametrize("make_answer", [_rows_answer, _document_answer])
async def test_without_the_hint_a_small_answer_has_no_handle(
    valkey: _MemoryValkey, make_answer: Any
) -> None:
    answer = make_answer()

    result, handle = await JsonFluxReducer().reduce(answer, None, _context())

    assert (result, handle) == (answer, None)
    assert valkey.values == {}


async def test_a_credential_op_with_the_hint_gets_no_handle_and_nothing_is_stored(
    valkey: _MemoryValkey,
) -> None:
    answer = {"data": {"password": "made-up-secret"}, "metadata": {"version": 3}}

    result, handle = await JsonFluxReducer().reduce(
        answer, None, _context("vault.kv.read", result_handle=_HINT)
    )

    assert (result, handle) == (answer, None)
    assert valkey.values == {}


@pytest.mark.parametrize("hint", [{"always": False}, {"always": "yes"}, "always", {}])
async def test_an_off_or_malformed_hint_keeps_the_size_rule(
    valkey: _MemoryValkey, hint: Any
) -> None:
    answer = _document_answer()

    result, handle = await JsonFluxReducer().reduce(answer, None, _context(result_handle=hint))

    assert (result, handle) == (answer, None)
    assert valkey.values == {}


async def test_hint_without_a_tenant_returns_the_answer_whole_without_a_handle() -> None:
    answer = _document_answer()

    result, handle = await JsonFluxReducer().reduce(
        answer, None, {"op_id": "test.inventory.read", "result_handle": _HINT}
    )

    assert (result, handle) == (answer, None)


async def test_hint_changes_nothing_over_the_limits(valkey: _MemoryValkey) -> None:
    answer = {"items": [{"name": f"vm-{i}"} for i in range(60)]}

    with_hint, handle = await JsonFluxReducer().reduce(answer, None, _context(result_handle=_HINT))
    without_hint, other = await JsonFluxReducer().reduce(answer, None, _context())

    assert handle is not None
    assert other is not None
    assert with_hint == without_hint
    assert with_hint["row_count"] == 60
    assert handle.total_rows == 60


def _descriptor(llm_instructions: dict[str, Any] | None) -> Any:
    return SimpleNamespace(source_kind="typed", llm_instructions=llm_instructions)


def test_dispatcher_forwards_the_hint() -> None:
    operator = _operator()
    built = _build_reducer_context(
        op_id="test.inventory.read",
        descriptor=_descriptor({"result_handle": _HINT}),
        operator=operator,
        target=None,
        audit_id=uuid.uuid4(),
    )
    plain = _build_reducer_context(
        op_id="test.inventory.read",
        descriptor=_descriptor({"when_to_call": "never"}),
        operator=operator,
        target=None,
        audit_id=uuid.uuid4(),
    )

    assert built["result_handle"] == _HINT
    assert "result_handle" not in plain


async def test_reply_paths_carry_the_whole_answer_and_the_handle(
    valkey: _MemoryValkey,
) -> None:
    answer = _document_answer()
    result, handle = await JsonFluxReducer().reduce(answer, None, _context(result_handle=_HINT))
    assert handle is not None

    envelope = wrap_ok_result("test.inventory.read", result, 1.0, handle, audit_id=uuid.uuid4())
    delivered = bound_complete_response(envelope, 256 * 1024)

    assert delivered is envelope
    assert delivered.result == answer
    assert delivered.handle == handle
    assert handle_is_readable(delivered.handle)
    wire = delivered.model_dump(mode="json")
    assert wire["result"] == answer
    assert wire["handle"]["handle_id"] == str(handle.handle_id)
    assert _handle_metadata_for_audit(handle) == {
        "handle_id": str(handle.handle_id),
        "total_rows": None,
        "sample_rows_returned": 0,
    }
