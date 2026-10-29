# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the two content-library find reads (#3550).

* ``vmware.content_library.find`` and ``vmware.content_library.item.find``
  against a recording fake connector: the vCenter path, the ``FindSpec`` at
  the top level of the body, only the filters the caller set, and the
  answer as a list of ids;
* the real connector over mocked HTTP: the find lands on ``/api``;
* the operation metadata: safe reads, the shared ``content_library`` group
  text, and the parameter schemas;
* through the dispatcher on ``vmware-rest-9.0`` (and ``8.0``): both ops are
  registered with their schemas and run; they never answer ``unknown_op``;
  a large answer becomes a result handle.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    VsphereTargetLike,
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.composites import _register
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_content_library_find import (
    CONTENT_LIBRARY_GROUP_KEY,
    CONTENT_LIBRARY_WHEN_TO_USE,
    VMWARE_CONTENT_LIBRARY_FIND_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP,
    content_library_find_impl,
    content_library_item_find_impl,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _FakeVmwareTarget,
    _seed_connector,
)
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _make_operator as _make_e2e_operator,
)

_FIND_OP = "vmware.content_library.find"
_ITEM_FIND_OP = "vmware.content_library.item.find"
_LIB_ID = "11111111-1111-4111-8111-111111111111"
_ITEM_ID = "22222222-2222-4222-8222-222222222222"


def _operator() -> Operator:
    return Operator(
        sub="op-content-library-find",
        name="Content Library Find Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


class _FakeVcenter(VmwareRestConnector):
    """A real connector subclass that answers the two finds from memory.

    The dispatcher binds the typed handlers to it. Only the mount and the
    POST seam are replaced; every call is recorded.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        super().__init__(session_loader=AsyncMock())
        self.answers = answers or {
            "/api/content/library?action=find": [_LIB_ID],
            "/api/content/library/item?action=find": [_ITEM_ID],
        }
        self.calls: list[tuple[str, str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def _post_json(  # type: ignore[override]
        self, target: Any, path: str, *, operator: Operator, verb: str = "POST", json: Any = None
    ) -> Any:
        self.calls.append((verb, path, copy.deepcopy(json)))
        return copy.deepcopy(self.answers[path])


# ---------------------------------------------------------------------------
# The two implementations
# ---------------------------------------------------------------------------


async def test_library_find_sends_the_find_spec_at_the_top_level() -> None:
    conn = _FakeVcenter()
    out = await content_library_find_impl(
        conn, _operator(), _Target(), {"name": "templates", "type": "LOCAL"}
    )
    assert conn.calls == [
        ("POST", "/api/content/library?action=find", {"name": "templates", "type": "LOCAL"})
    ]
    assert out == {"library_ids": [_LIB_ID]}
    Draft202012Validator(VMWARE_CONTENT_LIBRARY_FIND_OP.response_schema).validate(out)


async def test_item_find_sends_only_the_filters_that_are_set() -> None:
    conn = _FakeVcenter()
    params = {"name": "ubuntu", "library_id": _LIB_ID, "type": "ovf", "cached": False}
    out = await content_library_item_find_impl(conn, _operator(), _Target(), params)
    [(verb, path, body)] = conn.calls
    assert (verb, path) == ("POST", "/api/content/library/item?action=find")
    assert body == params
    assert "spec" not in body
    assert out == {"item_ids": [_ITEM_ID]}
    Draft202012Validator(VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP.response_schema).validate(out)


async def test_no_match_is_an_empty_list() -> None:
    conn = _FakeVcenter({"/api/content/library?action=find": []})
    out = await content_library_find_impl(conn, _operator(), _Target(), {"name": "nothing"})
    assert out == {"library_ids": []}


async def test_the_older_value_wrapper_is_read() -> None:
    conn = _FakeVcenter({"/api/content/library/item?action=find": {"value": [_ITEM_ID]}})
    out = await content_library_item_find_impl(conn, _operator(), _Target(), {"name": "x"})
    assert out == {"item_ids": [_ITEM_ID]}


async def test_an_answer_that_is_not_a_list_is_an_error() -> None:
    conn = _FakeVcenter({"/api/content/library?action=find": {"unexpected": True}})
    with pytest.raises(RuntimeError, match="expected id list"):
        await content_library_find_impl(conn, _operator(), _Target(), {"name": "x"})


# ---------------------------------------------------------------------------
# The real connector over mocked HTTP
# ---------------------------------------------------------------------------


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


@dataclass
class _StubTarget:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


async def test_the_real_connector_posts_the_find_on_api() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test", assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "9.0.0"})
            library = mock.post("/api/content/library", params={"action": "find"}).respond(
                200, json=[_LIB_ID]
            )
            item = mock.post("/api/content/library/item", params={"action": "find"}).respond(
                200, json=[_ITEM_ID]
            )
            found = await connector.content_library_find(
                _operator(), _StubTarget(), {"name": "templates"}
            )
            items = await connector.content_library_item_find(
                _operator(), _StubTarget(), {"library_id": _LIB_ID}
            )
        assert found == {"library_ids": [_LIB_ID]}
        assert items == {"item_ids": [_ITEM_ID]}
        assert library.call_count == 1
        assert library.calls.last.request.content == b'{"name":"templates"}'
        assert item.call_count == 1
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op", [VMWARE_CONTENT_LIBRARY_FIND_OP, VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP]
)
def test_op_is_a_safe_typed_read_in_the_content_library_group(op: Any) -> None:
    assert op in VMWARE_TYPED_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == CONTENT_LIBRARY_GROUP_KEY
    assert callable(getattr(VmwareRestConnector, op.handler_attr))
    for example in op.parameter_schema["examples"]:
        assert validate_params(op.parameter_schema, example) == []
    # vCenter answers 400 to an empty FindSpec, so MEHO refuses it first.
    assert validate_params(op.parameter_schema, {}) != []
    assert validate_params(op.parameter_schema, {"spec": {"name": "x"}}) != []


def test_typed_ops_and_composites_share_one_group_text() -> None:
    """The group text is written by whichever registrar runs first, so both use the same one."""
    assert (
        VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[CONTENT_LIBRARY_GROUP_KEY] == CONTENT_LIBRARY_WHEN_TO_USE
    )
    assert _register._WHEN_TO_USE_BY_GROUP[CONTENT_LIBRARY_GROUP_KEY] == CONTENT_LIBRARY_WHEN_TO_USE


def test_library_type_is_local_or_subscribed() -> None:
    schema = VMWARE_CONTENT_LIBRARY_FIND_OP.parameter_schema
    assert validate_params(schema, {"type": "SUBSCRIBED"}) == []
    assert validate_params(schema, {"type": "REMOTE"}) != []


# ---------------------------------------------------------------------------
# Through the dispatcher: registered, with schemas, and never unknown_op
# ---------------------------------------------------------------------------


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for the database tests."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service(_settings_env: None) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def _clean_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


async def test_both_finds_are_registered_with_their_schemas_on_both_catalogs(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    await register_vmware_typed_operations_8_0(embedding_service=stub_embedding_service)
    expected = {
        _FIND_OP: VMWARE_CONTENT_LIBRARY_FIND_OP,
        _ITEM_FIND_OP: VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP,
    }
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(list(expected)))
                )
            )
            .scalars()
            .all()
        )
        groups = {
            group.id: group for group in (await session.execute(select(OperationGroup))).scalars()
        }
    assert {(row.version, row.op_id) for row in rows} == {
        (version, op_id) for version in ("8.0", "9.0") for op_id in expected
    }
    for row in rows:
        op = expected[row.op_id]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == ("safe", False)
        assert row.parameter_schema == op.parameter_schema
        assert row.response_schema == op.response_schema
        assert groups[row.group_id].group_key == CONTENT_LIBRARY_GROUP_KEY


async def test_both_finds_run_through_call_operation_on_9_0(
    _clean_registry: None,
    stub_embedding_service: AsyncMock,
) -> None:
    fake = _FakeVcenter()
    _seed_connector(fake)  # type: ignore[arg-type]
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    operator = _make_e2e_operator(sub="content-library-finder")
    target = _FakeVmwareTarget()

    found = await dispatch(
        operator=operator,
        connector_id="vmware-rest-9.0",
        op_id=_FIND_OP,
        target=target,
        params={"name": "templates"},
    )
    assert found.status == "ok", found.error
    assert found.result == {"library_ids": [_LIB_ID]}

    items = await dispatch(
        operator=operator,
        connector_id="vmware-rest-9.0",
        op_id=_ITEM_FIND_OP,
        target=target,
        params={"name": "ubuntu", "library_id": _LIB_ID},
    )
    assert items.status == "ok", items.error
    assert items.result == {"item_ids": [_ITEM_ID]}
    assert [path for _verb, path, _body in fake.calls] == [
        "/api/content/library?action=find",
        "/api/content/library/item?action=find",
    ]

    # An empty FindSpec is refused by MEHO before any vCenter call.
    refused = await dispatch(
        operator=operator,
        connector_id="vmware-rest-9.0",
        op_id=_FIND_OP,
        target=target,
        params={},
    )
    assert refused.status == "error"
    assert refused.extras["error_code"] == "invalid_params"
    assert refused.extras["validation_errors"][0]["validator"] == "minProperties"
    assert len(fake.calls) == 2


async def test_a_large_answer_becomes_a_result_handle(
    _clean_registry: None,
    stub_embedding_service: AsyncMock,
) -> None:
    """The id list is the one list in the answer, so JSONFlux spills a big one to a handle."""
    many = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(300)]
    fake = _FakeVcenter({"/api/content/library/item?action=find": many})
    _seed_connector(fake)  # type: ignore[arg-type]
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    # The tests run with a pass-through reducer; the app installs this one at start.
    # The conftest safety net puts the pass-through reducer back after the test.
    set_default_reducer(JsonFluxReducer())

    result = await dispatch(
        operator=_make_e2e_operator(sub="content-library-finder"),
        connector_id="vmware-rest-9.0",
        op_id=_ITEM_FIND_OP,
        target=_FakeVmwareTarget(),
        params={"library_id": _LIB_ID},
    )
    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 300
