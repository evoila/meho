# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingested operations run only on a connector class that accepts them.

MEHO picks the connector class from the target. An ingested operation
sends its method and path with that class's login and runs none of the
checks of the class's hand-written operations. So:

* the ingest refuses an API file under the name of a class that does not
  accept ingested operations (``keycloak/26.x/keycloak-admin``), before it
  reads the file; REST answers 422, MCP ``-32602``;
* a call of such a row is refused before it waits for approval, and an
  approved call of such a row is refused when it runs; nothing reaches the
  vendor, and the refusal has one ``denied`` audit row;
* ``preview_operation`` gives the same refusal;
* the generic and profiled classes, and the hand-written classes that serve
  an ingested catalog, still accept ingested operations.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from meho_backplane.api.v1.connectors_ingest import _run_ingest_with_http_mapping
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.argocd.connector import ArgoCdConnector
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.github.connector import GitHubRestConnector
from meho_backplane.connectors.harbor.connector import HarborConnector
from meho_backplane.connectors.hetzner_robot.connector import HetznerRobotConnector
from meho_backplane.connectors.keycloak.connector import KeycloakConnector
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.connector_ingest import _run_inline_ingest
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._ingested_owner import (
    INGESTED_OPS_NOT_ACCEPTED,
    IngestedOperationsNotAccepted,
    assert_ingest_target_accepts,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    approve_request,
    create_pending_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.ingest import IngestionPipelineService, SpecSource
from meho_backplane.operations.ingest.connector_registration import (
    GenericRestConnector,
    ensure_connector_class_registered,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000c1a5")
_PRODUCT = KeycloakConnector.product
_VERSION = KeycloakConnector.version
_IMPL_ID = KeycloakConnector.impl_id
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_OP_ID = "DELETE:/admin/realms/{realm}"
_TARGET_NAME = "kc-main"
#: The realm of MEHO's own login in these tests.
_PARAMS = {"realm": "meho"}


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
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def vendor_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Register the real Keycloak class and record every request it would send."""
    calls: list[tuple[str, str]] = []

    async def _request_json(
        self: KeycloakConnector, target: Any, method: str, path: str, **_: Any
    ) -> dict[str, Any]:
        calls.append((method, path))
        return {}

    async def _post_json(
        self: KeycloakConnector, target: Any, path: str, *, verb: str = "POST", **_: Any
    ) -> dict[str, Any]:
        calls.append((verb, path))
        return {}

    monkeypatch.setattr(KeycloakConnector, "_request_json", _request_json)
    monkeypatch.setattr(KeycloakConnector, "_post_json", _post_json)
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=KeycloakConnector
    )
    return calls


def _operator(sub: str = "op-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Ingested Owner Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = _FakeFingerprint(version="26.0")
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = _TARGET_NAME
        self.host = "keycloak.example.test"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None
        self.fqdn: str | None = None


async def _seed_target() -> TargetORM:
    async with get_sessionmaker()() as s:
        target = TargetORM(
            tenant_id=_TENANT,
            name=_TARGET_NAME,
            aliases=[],
            product=_PRODUCT,
            host="keycloak.example.test",
            port=443,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "26.0"},
            preferred_impl_id=None,
            notes="seeded by test_operations_ingested_ops_owner",
        )
        s.add(target)
        await s.commit()
        await s.refresh(target)
        s.expunge(target)
        return target


async def _seed_ingested_realm_delete(impl_id: str = _IMPL_ID) -> None:
    """The row an ingest of the Keycloak admin API file would leave, turned on."""
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=impl_id,
                op_id=_OP_ID,
                source_kind="ingested",
                method="DELETE",
                path="/admin/realms/{realm}",
                handler_ref=None,
                summary="Delete the realm",
                description="Ingested realm delete.",
                tags=[],
                parameter_schema={
                    "type": "object",
                    "properties": {"realm": {"type": "string", "x-meho-param-loc": "path"}},
                    "required": ["realm"],
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
        )
        await s.commit()


async def _approval_row_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _refusal_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars().all()
    return [
        r
        for r in rows
        if (r.payload or {}).get("error", {}).get("error_code") == INGESTED_OPS_NOT_ACCEPTED
    ]


# ---------------------------------------------------------------------------
# Which classes accept ingested operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        GenericRestConnector,
        ProfiledRestConnector,
        GitHubRestConnector,
        HetznerRobotConnector,
        HarborConnector,
        ArgoCdConnector,
    ],
)
def test_classes_that_serve_an_ingested_catalog_accept_ingested_ops(
    cls: type[Connector],
) -> None:
    assert cls.accepts_ingested_operations is True


def test_hand_written_classes_refuse_ingested_ops_by_default() -> None:
    assert Connector.accepts_ingested_operations is False
    assert KeycloakConnector.accepts_ingested_operations is False


# ---------------------------------------------------------------------------
# At ingest
# ---------------------------------------------------------------------------


def test_ingest_under_the_keycloak_name_is_refused(vendor_calls: list[tuple[str, str]]) -> None:
    with pytest.raises(IngestedOperationsNotAccepted) as info:
        assert_ingest_target_accepts(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID)
    assert info.value.class_name == "KeycloakConnector"
    assert str(info.value).startswith(f"{INGESTED_OPS_NOT_ACCEPTED}:")


@pytest.mark.asyncio
async def test_rows_under_another_name_are_refused_when_the_target_picks_keycloak(
    vendor_calls: list[tuple[str, str]],
) -> None:
    """A new impl id passes the ingest (it gets a shim), but a Keycloak target
    still resolves to the Keycloak class, so the call is refused at run time."""
    assert_ingest_target_accepts(product=_PRODUCT, version=_VERSION, impl_id="keycloak-custom")
    assert ensure_connector_class_registered(
        product=_PRODUCT, version=_VERSION, impl_id="keycloak-custom", base_url=None
    )
    await _seed_target()
    await _seed_ingested_realm_delete(impl_id="keycloak-custom")

    result = await call_operation(
        _operator(),
        {
            "connector_id": f"keycloak-custom-{_VERSION}",
            "op_id": _OP_ID,
            "target": _TARGET_NAME,
            "params": _PARAMS,
        },
    )

    assert result["status"] == "error", result
    assert result["extras"]["error_code"] == INGESTED_OPS_NOT_ACCEPTED
    assert await _approval_row_count() == 0
    assert vendor_calls == []


@pytest.mark.parametrize("cls", [GitHubRestConnector, HetznerRobotConnector, HarborConnector])
def test_ingest_under_a_class_that_accepts_ingested_ops_goes_on(cls: type[Connector]) -> None:
    register_connector_v2(product=cls.product, version=cls.version, impl_id=cls.impl_id, cls=cls)
    assert_ingest_target_accepts(product=cls.product, version=cls.version, impl_id=cls.impl_id)


def test_ingest_under_a_new_name_goes_on() -> None:
    """No class serves the name yet: the ingest registers a shim, which accepts."""
    assert_ingest_target_accepts(product="newapi", version="1.0", impl_id="newapi-rest")


@pytest.mark.asyncio
async def test_pipeline_refuses_before_it_reads_the_file(
    vendor_calls: list[tuple[str, str]],
) -> None:
    service = IngestionPipelineService(operator=_operator())
    # The URI cannot be read: a fetch would raise another error.
    spec = SpecSource(uri="file:///nonexistent/keycloak-admin.yaml")
    with pytest.raises(IngestedOperationsNotAccepted):
        await service.ingest(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            specs=[spec],
            tenant_id=_TENANT,
        )
    with pytest.raises(IngestedOperationsNotAccepted):
        await service.ingest(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            specs=[spec],
            tenant_id=_TENANT,
            dry_run=True,
        )


def _ingest_body() -> SimpleNamespace:
    """The fields the REST route and the MCP tool read off an ingest request."""
    return SimpleNamespace(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        specs=[SpecSource(uri="file:///nonexistent/keycloak-admin.yaml")],
        base_url=None,
        tenant_id=_TENANT,
        dry_run=False,
        auth_scheme=None,
        auth_secret_fields=None,
    )


@pytest.mark.asyncio
async def test_rest_answers_422_with_the_refusal_detail(
    vendor_calls: list[tuple[str, str]],
) -> None:
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(HTTPException) as info:
        await _run_ingest_with_http_mapping(
            service=service,
            body=_ingest_body(),  # type: ignore[arg-type]
        )
    assert info.value.status_code == 422
    detail = info.value.detail
    assert isinstance(detail, dict)
    assert detail["kind"] == INGESTED_OPS_NOT_ACCEPTED
    assert detail["connector_class"] == "KeycloakConnector"
    assert (detail["product"], detail["version"], detail["impl_id"]) == (
        _PRODUCT,
        _VERSION,
        _IMPL_ID,
    )


@pytest.mark.asyncio
async def test_mcp_answers_invalid_params_with_the_refusal_detail(
    vendor_calls: list[tuple[str, str]],
) -> None:
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(McpInvalidParamsError) as info:
        await _run_inline_ingest(
            service=service,
            request=_ingest_body(),  # type: ignore[arg-type]
            tenant_id=_TENANT,
        )
    assert info.value.data is not None
    assert info.value.data["kind"] == INGESTED_OPS_NOT_ACCEPTED
    assert info.value.data["connector_class"] == "KeycloakConnector"


# ---------------------------------------------------------------------------
# At run time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_is_refused_before_it_waits_for_approval(
    vendor_calls: list[tuple[str, str]],
) -> None:
    await _seed_target()
    await _seed_ingested_realm_delete()

    result = await call_operation(
        _operator(),
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": _OP_ID,
            "target": _TARGET_NAME,
            "params": _PARAMS,
        },
    )

    assert result["status"] == "error", result
    assert result["extras"]["error_code"] == INGESTED_OPS_NOT_ACCEPTED
    assert "KeycloakConnector" in result["error"]
    assert await _approval_row_count() == 0
    assert vendor_calls == []
    (row,) = await _refusal_rows()
    assert row.payload["result_status"] == "denied"


@pytest.mark.asyncio
async def test_approved_realm_delete_never_reaches_keycloak(
    vendor_calls: list[tuple[str, str]],
) -> None:
    """A row approved earlier is refused when the approved call runs."""
    target = await _seed_target()
    await _seed_ingested_realm_delete()
    requester = _operator("op-requester")
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=requester,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={"op_id": _OP_ID, "safety_level": "dangerous"},
        )
        await s.commit()
        request_id = request.id

    approver = _operator("op-approver").model_copy(update={"self_approver": True})
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        result = await resume_dispatch_after_approval(operator=approver, request=row, params=None)

    assert result.status == "error"
    assert result.extras["error_code"] == INGESTED_OPS_NOT_ACCEPTED
    assert vendor_calls == []
    (audit_row,) = await _refusal_rows()
    assert audit_row.payload["result_status"] == "denied"
    assert result.audit_id == audit_row.id


@pytest.mark.asyncio
async def test_preview_gives_the_same_refusal(vendor_calls: list[tuple[str, str]]) -> None:
    await _seed_ingested_realm_delete()

    envelope = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeTarget(),
        params=_PARAMS,
    )

    assert envelope["status"] == "error", envelope
    assert envelope["extras"]["error_code"] == INGESTED_OPS_NOT_ACCEPTED
    assert vendor_calls == []
    assert await _refusal_rows() == []
