# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""phpIPAM connector through MEHO's dispatcher: registration, approval, audit, handles, secrets.

* The four operations are registered with their risk levels.
* ``phpipam.address.reserve`` and ``phpipam.address.release`` wait for a
  second person's approval; after the approval the change runs once and the
  audit table has its rows.
* A long list of free addresses comes back as a link to the stored result.
* Planted secret values (app code, user, password, phpIPAM token) never show
  up in a result, an audit row, an approval row or an error text.
* A wrong user password fails the call; after the secret is corrected in
  Vault, the next call works without a restart.

The vendor is :class:`tests._phpipam_fake.FakePhpipam` behind respx; the
secret comes from the fake Vault client. Documentation addresses only.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.phpipam  # noqa: F401 -- registers the connector
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.phpipam import (
    PHPIPAM_CONNECTOR_ID,
    PhpipamConnector,
    register_phpipam_typed_operations,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatcher as dispatcher_module
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

from ._phpipam_fake import APP_ID, HOST, FakePhpipam
from ._result_store_helpers import MemoryResultStore
from ._vault_fakes import install_fake_client

_TENANT = UUID("00000000-0000-0000-0000-000000005071")
_TARGET_NAME = "ipam-governed"

#: Planted values. None of them may leave the connector.
_PLANTED_APP_TOKEN = "planted-app-code-5070-zz"  # gitleaks:allow NOSONAR -- planted test value
_PLANTED_USER = "planted-user-5070"
_PLANTED_PASSWORD = "planted-pass-5070-zz"  # gitleaks:allow NOSONAR -- planted test value
_PLANTED_SESSION = "planted-session-5070-zz"  # gitleaks:allow NOSONAR -- planted test value
_PLANTED_WRONG_PASSWORD = "planted-wrong-5070-zz"  # gitleaks:allow NOSONAR -- planted test value
_PLANTED = (_PLANTED_APP_TOKEN, _PLANTED_PASSWORD, _PLANTED_SESSION)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product="phpipam", version="1.x", impl_id="phpipam-rest", cls=PhpipamConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return service


def _operator(sub: str = "op-requester") -> Operator:
    return Operator(
        sub=sub,
        name="IPAM governed test",
        email=None,
        raw_jwt=f"{sub}.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    def __init__(self) -> None:
        self.product = "phpipam"
        self.fingerprint = type("_FP", (), {"version": None})()
        self.preferred_impl_id: str | None = "phpipam-rest"
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = _TARGET_NAME
        self.host = HOST
        self.port = 443
        self.secret_ref = "tenants/test/ipam-governed"
        self.auth_model = "shared_service_account"
        self.extras = {"app_id": APP_ID}


async def _seed_target() -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name=_TARGET_NAME,
                aliases=[],
                product="phpipam",
                host=HOST,
                port=443,
                fqdn=None,
                secret_ref="tenants/test/ipam-governed",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"app_id": APP_ID},
                fingerprint={},
                preferred_impl_id="phpipam-rest",
                notes="seeded by test_connectors_phpipam_governed",
            )
        )
        await s.commit()


def _fake(*, user_login: bool = False) -> FakePhpipam:
    fake = FakePhpipam(app_token=None if user_login else _PLANTED_APP_TOKEN)
    if user_login:
        fake.username = _PLANTED_USER
        fake.password = _PLANTED_PASSWORD
        fake.user_token = _PLANTED_SESSION
    fake.add_subnet(7, "192.0.2.0/24")
    fake.add_address(7, "192.0.2.1", "gw.example.test")
    return fake


def _install_secret(monkeypatch: pytest.MonkeyPatch, fake: FakePhpipam) -> Any:
    if fake.app_token is not None:
        return install_fake_client(monkeypatch, secret={"token": fake.app_token})
    return install_fake_client(
        monkeypatch, secret={"username": fake.username, "password": fake.password}
    )


async def _db_text() -> str:
    """Every audit and approval row as text, for the planted-secret check."""
    async with get_sessionmaker()() as s:
        rows: list[Any] = list((await s.execute(select(AuditLog))).scalars())
        rows += list((await s.execute(select(ApprovalRequest))).scalars())
    return json.dumps(
        [{k: v for k, v in vars(row).items() if not k.startswith("_")} for row in rows],
        default=str,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_four_operations_registered_with_their_risk(_stub_embedding: AsyncMock) -> None:
    await register_phpipam_typed_operations(embedding_service=_stub_embedding)
    async with get_sessionmaker()() as s:
        rows = {
            row.op_id: row
            for row in (
                await s.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "phpipam")
                )
            ).scalars()
        }
    assert {op: (r.safety_level, r.requires_approval, r.source_kind) for op, r in rows.items()} == {
        "phpipam.subnet.list": ("safe", False, "typed"),
        "phpipam.subnet.free_addresses": ("safe", False, "typed"),
        "phpipam.address.reserve": ("caution", True, "typed"),
        "phpipam.address.release": ("dangerous", True, "typed"),
    }
    assert all(row.response_schema for row in rows.values())


# ---------------------------------------------------------------------------
# Long lists come back behind a result handle
# ---------------------------------------------------------------------------


async def test_long_free_address_list_comes_back_as_a_handle(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register_phpipam_typed_operations(embedding_service=_stub_embedding)
    fake = _fake()
    _install_secret(monkeypatch, fake)
    # The app start swaps in the JSONFlux reducer; do the same here.
    monkeypatch.setattr(
        dispatcher_module, "_DEFAULT_REDUCER", JsonFluxReducer(store=MemoryResultStore())
    )
    async with respx.mock(base_url=f"https://{HOST}", assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        result = await dispatch(
            operator=_operator(),
            connector_id=PHPIPAM_CONNECTOR_ID,
            op_id="phpipam.subnet.free_addresses",
            target=_Target(),
            params={"subnet_id": 7, "limit": 200},
        )
    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.handle.total_rows == 200
    assert result.handle.fetch_more.drill_in.available is True


# ---------------------------------------------------------------------------
# Changes wait for approval, then run once with an audit row
# ---------------------------------------------------------------------------


async def _approve_and_resume(request_id: UUID) -> Any:
    approver = _operator("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


@pytest.mark.parametrize(
    ("op_id", "params", "status"),
    [
        (
            "phpipam.address.reserve",
            {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "web-01.example.test"},
            "reserved",
        ),
        ("phpipam.address.release", {"subnet_id": 7, "ip": "192.0.2.1"}, "released"),
    ],
)
async def test_change_waits_for_approval_then_runs_once_with_audit(
    _stub_embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    status: str,
) -> None:
    await register_phpipam_typed_operations(embedding_service=_stub_embedding)
    await _seed_target()
    fake = _fake()
    _install_secret(monkeypatch, fake)
    args = {
        "connector_id": PHPIPAM_CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }
    async with respx.mock(base_url=f"https://{HOST}", assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        preview = await preview_operation(_operator(), args)
        call = await call_operation(_operator(), args)
        assert call["status"] == "awaiting_approval", call
        assert fake.calls_of("POST") == [] and fake.calls_of("DELETE") == []
        request_id = UUID(call["extras"]["approval_request_id"])
        async with get_sessionmaker()() as s:
            parked = await s.get(ApprovalRequest, request_id)
            assert parked is not None
            card = dict(parked.proposed_effect)["preview"]
        resume = await _approve_and_resume(request_id)

    assert preview["status"] == "ok", preview
    assert card["cidr"] == "192.0.2.0/24"
    assert card["ip"] == params["ip"]
    assert card["address_free"] is (op_id == "phpipam.address.reserve")
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == status
    assert resume.result["verification"] == "verified"
    assert len(fake.calls_of("POST") + fake.calls_of("DELETE")) == 1
    async with get_sessionmaker()() as s:
        audit = [
            row
            for row in (await s.execute(select(AuditLog))).scalars()
            if op_id in json.dumps(row.payload, default=str)
        ]
    assert audit, "the change has no audit row"


# ---------------------------------------------------------------------------
# Planted secrets never leave the connector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_login", [False, True])
async def test_planted_secrets_stay_out_of_results_audit_and_errors(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, user_login: bool
) -> None:
    await register_phpipam_typed_operations(embedding_service=_stub_embedding)
    await _seed_target()
    fake = _fake(user_login=user_login)
    _install_secret(monkeypatch, fake)
    args = {
        "connector_id": PHPIPAM_CONNECTOR_ID,
        "op_id": "phpipam.address.reserve",
        "target": _TARGET_NAME,
        "params": {"subnet_id": 7, "ip": "192.0.2.10", "hostname": "web-01.example.test"},
    }
    texts: list[str] = []
    async with respx.mock(base_url=f"https://{HOST}", assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        listed = await dispatch(
            operator=_operator(),
            connector_id=PHPIPAM_CONNECTOR_ID,
            op_id="phpipam.subnet.list",
            target=_Target(),
            params={},
        )
        texts.append(listed.model_dump_json())
        call = await call_operation(_operator(), args)
        resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
        texts.append(json.dumps(call, default=str))
        texts.append(resume.model_dump_json())
        # A refused call: the error text must not carry the secret either.
        fake.refuse_tokens = 2
        refused = await dispatch(
            operator=_operator(),
            connector_id=PHPIPAM_CONNECTOR_ID,
            op_id="phpipam.subnet.list",
            target=_Target(),
            params={},
        )
        texts.append(refused.model_dump_json())
    assert listed.status == "ok", listed.error
    assert resume.status == "ok", resume.error
    assert refused.status == "error"
    texts.append(await _db_text())
    blob = "\n".join(texts)
    for planted in _PLANTED:
        assert planted not in blob


# ---------------------------------------------------------------------------
# A corrected secret works on the next call, without a restart
# ---------------------------------------------------------------------------


async def test_corrected_password_works_on_the_next_call(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register_phpipam_typed_operations(embedding_service=_stub_embedding)
    await _seed_target()
    fake = _fake(user_login=True)
    vault = _install_secret(monkeypatch, fake)
    vault.secrets.kv.v2.secret = {"username": fake.username, "password": _PLANTED_WRONG_PASSWORD}

    async def _list() -> Any:
        return await dispatch(
            operator=_operator(),
            connector_id=PHPIPAM_CONNECTOR_ID,
            op_id="phpipam.subnet.list",
            target=_Target(),
            params={},
        )

    async with respx.mock(base_url=f"https://{HOST}", assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        wrong = await _list()
        vault.secrets.kv.v2.secret = {"username": fake.username, "password": fake.password}
        fixed = await _list()
    assert wrong.status == "error", wrong
    assert fixed.status == "ok", fixed.error
    assert fixed.result["total"] == 1
    assert len(fake.calls_of("POST", f"/api/{APP_ID}/user/")) == 2
    assert len(vault.secrets.kv.v2.read_calls) == 2
