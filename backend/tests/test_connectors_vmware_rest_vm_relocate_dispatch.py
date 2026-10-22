# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person's generic VM relocate waits for approval with the copy size (#4749).

The floor stores the generic relocate as ``dangerous`` with
``requires_approval=True``. A human operator seat that dispatches it waits for
a second person; the approval row carries the copy-size preview from
``vm_relocate_preview.py``, so the approver sees how much data the move copies
and how much room the target datastore has. Nothing reaches vCenter but the
two read-only property reads.

Mirrors the fixtures of ``test_connectors_vmware_rest_tagging_delete_dispatch.py``
(recording HTTP connector, in-memory SQLite via the autouse-migrated engine).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest import vm_relocate_preview as previews
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import VimFake, moref

_TENANT: UUID = UUID("00000000-0000-0000-0000-000000004749")
_CONNECTOR_ID = "vmware-rest-9.0"
_RELOCATE = "POST:/vcenter/vm/{vm}?action=relocate"
_GIB = 1024**3
_VM = "vm-101"
_SOURCE = "datastore-11"
_TARGET = "datastore-22"
_RELOCATE_PARAMS: dict[str, Any] = {"vm": _VM, "body": {"placement": {"datastore": _TARGET}}}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry; keep the preview registry."""
    builders_snapshot = dict(_PREVIEW_BUILDERS)
    reset_dispatcher_caches()
    clear_registry()
    previews.register_vm_relocate_previews()
    yield
    reset_dispatcher_caches()
    clear_registry()
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(builders_snapshot)


def _operator() -> Operator:
    return Operator(
        sub="op-requester",
        name="VM Relocate Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """In-memory target shape the resolver / dispatcher / connectors read."""

    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint(version="9.0")
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = uuid.uuid4()
        self.name = "vcenter-lab"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None
        self.fqdn: str | None = None


class _FakeVcenter(HttpConnector):
    """vCenter stand-in: answers the VI-JSON property reads, records every write."""

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[dict[str, Any]] = []
        self.vim = VimFake()
        self.vim.add(
            "VirtualMachine",
            _VM,
            name="nested-esxi-01",
            **{
                "runtime.powerState": "poweredOff",
                "datastore": [moref("Datastore", _SOURCE)],
                "summary.storage": {"committed": 365 * _GIB, "uncommitted": 738 * _GIB},
            },
        )
        self.vim.add("Datastore", _SOURCE, summary={"name": "local-a"})
        self.vim.add(
            "Datastore",
            _TARGET,
            summary={"name": "local-b", "capacity": 4000 * _GIB, "freeSpace": 2000 * _GIB},
        )

    async def _post_vmomi_json(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return await self.vim._post_vmomi_json(*args, **kwargs)

    async def _request_json(  # type: ignore[override]
        self, target: Any, method: str, path: str, *, operator: Operator, **kw: Any
    ) -> Any:
        self.writes.append({"method": method, "path": path, "json": kw.get("json")})
        return {}

    async def _post_json(  # type: ignore[override]
        self, target: Any, path: str, *, operator: Operator, verb: str = "POST", **kw: Any
    ) -> Any:
        self.writes.append({"method": verb, "path": path, "json": kw.get("json")})
        return {}

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


def _register_fake_vcenter() -> _FakeVcenter:
    register_connector_v2(product="vmware", version="9.0", impl_id="vmware-rest", cls=_FakeVcenter)
    connector = get_or_create_connector_instance(_FakeVcenter)
    assert isinstance(connector, _FakeVcenter)
    return connector


async def _insert_relocate_descriptor() -> None:
    """Seed the generic relocate the way the floor stores it: dangerous, with approval."""
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=_RELOCATE,
        source_kind="ingested",
        method="POST",
        path=_RELOCATE.split(":", 1)[1],
        handler_ref=None,
        summary="Relocate a VM.",
        description=None,
        tags=[],
        parameter_schema={
            "type": "object",
            "properties": {
                "vm": {"type": "string", "x-meho-param-loc": "path"},
                "body": {"type": "object", "x-meho-param-loc": "body"},
            },
        },
        response_schema=None,
        llm_instructions=None,
        safety_level="dangerous",
        requires_approval=True,
        is_enabled=True,
        embedding=[0.1] * 384,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    async with get_sessionmaker()() as s:
        s.add(descriptor)
        await s.commit()


async def _approval_row_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def test_operator_relocate_waits_for_approval_with_the_copy_size() -> None:
    """A person's relocate parks; the approval row shows the size and the target's room."""
    connector = _register_fake_vcenter()
    await _insert_relocate_descriptor()

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_RELOCATE,
        target=_FakeTarget(),
        params=_RELOCATE_PARAMS,
    )

    assert result.status == "awaiting_approval", result
    assert connector.writes == []
    assert connector.vim.methods() == ["RetrievePropertiesEx", "RetrievePropertiesEx"]
    assert await _approval_row_count() == 1
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "dangerous"
    preview = effect["preview"]
    assert preview["copied_now_bytes"] == 365 * _GIB
    assert preview["copy_max_bytes"] == 1103 * _GIB
    assert preview["source_datastores"] == [{"id": _SOURCE, "name": "local-a"}]
    assert preview["target_datastores"][0]["free_space_after_copy_bytes"] == 1635 * _GIB
    assert preview["summary"].startswith("Copies 365.0 GiB now, up to 1103.0 GiB")


async def test_operator_relocate_still_waits_when_the_vm_read_fails() -> None:
    """A failed size read never blocks the park; the row says why the size is missing."""
    connector = _register_fake_vcenter()
    connector.vim.objects.pop(("VirtualMachine", _VM))
    await _insert_relocate_descriptor()

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_RELOCATE,
        target=_FakeTarget(),
        params=_RELOCATE_PARAMS,
    )

    assert result.status == "awaiting_approval", result
    assert connector.writes == []
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
        assert row is not None
        effect = dict(row.proposed_effect)
    assert effect["preview"]["copied_now_bytes"] is None
    assert effect["preview"]["size_unavailable_reason"].startswith("the VM read failed (")
    assert effect["preview"]["target_datastores"][0]["free_space_bytes"] == 2000 * _GIB
