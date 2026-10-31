# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``idb`` connectors through MEHO's dispatcher: risk, approval, audit, handles, login, secrets.

* Both implementations register the six operations with their risk levels.
* ``idb.directory.sync`` waits for a second person's approval; after the
  approval it runs once and the audit table has its row. A vendor error
  reaches the answer.
* A ``401`` on a call drops the token, MEHO logs in again and the call works.
* A long user list comes back as a link to the stored result.
* Planted secrets (client secret, tokens, a user's password field) never show
  up in a result, an audit row or an approval row.
* On Identity Broker 9.x the directory operations stop early with a message.

The vendor is :class:`tests._idb_fake.FakeIdb` behind respx; the secret
comes from the fake Vault client. Made-up names only.
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

import meho_backplane.connectors.idb  # noqa: F401 -- registers the connectors
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.idb import (
    IDB_REST_CONNECTOR_ID,
    IDB_WSA_CONNECTOR_ID,
    IdbRestConnector,
    IdbWsaConnector,
    register_idb_rest_typed_operations,
    register_idb_wsa_typed_operations,
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
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

from ._idb_fake import BASE_URL, HOST, FakeIdb, scim_user
from ._result_store_helpers import MemoryResultStore
from ._vault_fakes import install_fake_client

_TENANT = UUID("00000000-0000-0000-0000-000000004509")
_TARGET_NAME = "idb-governed"

_PLANTED_SECRET = "planted-client-secret-4508-zz"  # gitleaks:allow NOSONAR -- planted test value
_PLANTED_TOKEN = "planted-access-token-4508-zz"  # gitleaks:allow NOSONAR -- planted test value

_EXPECTED_RISK = {
    "idb.about": ("safe", False),
    "idb.identity_provider.list": ("safe", False),
    "idb.directory.list": ("safe", False),
    "idb.directory.sync": ("caution", True),
    "idb.user.find": ("safe", False),
    "idb.group.find": ("safe", False),
}


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
    register_connector_v2(product="idb", version="3.3", impl_id="idb-wsa", cls=IdbWsaConnector)
    register_connector_v2(product="idb", version="9.0", impl_id="idb-rest", cls=IdbRestConnector)
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
        name="IDB governed test",
        email=None,
        raw_jwt=f"{sub}.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    def __init__(self, impl_id: str = "idb-wsa", version: str | None = "3.3") -> None:
        self.product = "idb"
        self.fingerprint = type("_FP", (), {"version": version})()
        self.version = version
        self.preferred_impl_id: str | None = impl_id
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = _TARGET_NAME
        self.host = HOST
        self.port = 443
        self.secret_ref = "tenants/test/idb-governed"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


async def _register_all(embedding: AsyncMock) -> None:
    await register_idb_wsa_typed_operations(embedding_service=embedding)
    await register_idb_rest_typed_operations(embedding_service=embedding)


async def _seed_target(
    name: str = _TARGET_NAME, impl_id: str = "idb-wsa", version: str = "3.3"
) -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name=name,
                aliases=[],
                product="idb",
                host=HOST,
                port=443,
                fqdn=None,
                version=version,
                secret_ref="tenants/test/idb-governed",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={},
                preferred_impl_id=impl_id,
                notes="seeded by test_connectors_idb_governed",
            )
        )
        await s.commit()


def _fake(product: str = "wsa") -> FakeIdb:
    fake = FakeIdb(product=product, client_secret=_PLANTED_SECRET, token_prefix=_PLANTED_TOKEN)
    fake.directories = [{"directoryId": "dir-1", "name": "example.test", "type": "LDAP"}]
    fake.users = [scim_user("alice")]
    return fake


def _install_secret(monkeypatch: pytest.MonkeyPatch, fake: FakeIdb) -> Any:
    return install_fake_client(
        monkeypatch, secret={"client_id": fake.client_id, "client_secret": fake.client_secret}
    )


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


async def _db_text() -> str:
    async with get_sessionmaker()() as s:
        rows: list[Any] = list((await s.execute(select(AuditLog))).scalars())
        rows += list((await s.execute(select(ApprovalRequest))).scalars())
    return json.dumps(
        [{k: v for k, v in vars(row).items() if not k.startswith("_")} for row in rows],
        default=str,
    )


# ---------------------------------------------------------------------------
# Registration: risk level and approval flag of every operation
# ---------------------------------------------------------------------------


async def test_both_implementations_register_six_ops_with_their_risk(
    _stub_embedding: AsyncMock,
) -> None:
    await _register_all(_stub_embedding)
    async with get_sessionmaker()() as s:
        rows = list(
            (
                await s.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "idb")
                )
            ).scalars()
        )
    for impl_id in ("idb-wsa", "idb-rest"):
        found = {
            r.op_id: (r.safety_level, r.requires_approval) for r in rows if r.impl_id == impl_id
        }
        assert found == _EXPECTED_RISK, impl_id
    assert all(r.source_kind == "typed" and r.response_schema for r in rows)


# ---------------------------------------------------------------------------
# The change waits for approval, then runs once with an audit row
# ---------------------------------------------------------------------------


async def test_directory_sync_waits_for_approval_then_runs_once(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_all(_stub_embedding)
    await _seed_target()
    fake = _fake()
    _install_secret(monkeypatch, fake)
    args = {
        "connector_id": IDB_WSA_CONNECTOR_ID,
        "op_id": "idb.directory.sync",
        "target": _TARGET_NAME,
        "params": {"directory_id": "dir-1"},
    }
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        call = await call_operation(_operator(), args)
        assert call["status"] == "awaiting_approval", call
        assert [c for c in fake.calls_of("POST") if "sync" in c.url.path] == []
        request_id = UUID(call["extras"]["approval_request_id"])
        async with get_sessionmaker()() as s:
            parked = await s.get(ApprovalRequest, request_id)
            assert parked is not None
            card = dict(parked.proposed_effect)["preview"]
        resume = await _approve_and_resume(request_id)
    assert card == {"directory_id": "dir-1"}
    assert resume.status == "ok", resume.error
    assert resume.result["started"] is True
    assert len([c for c in fake.calls_of("POST") if "sync" in c.url.path]) == 1
    async with get_sessionmaker()() as s:
        audit = [
            row
            for row in (await s.execute(select(AuditLog))).scalars()
            if "idb.directory.sync" in json.dumps(row.payload, default=str)
        ]
    assert audit, "the change has no audit row"


async def test_directory_sync_vendor_error_reaches_the_answer(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_all(_stub_embedding)
    await _seed_target()
    fake = _fake()
    _install_secret(monkeypatch, fake)
    args = {
        "connector_id": IDB_WSA_CONNECTOR_ID,
        "op_id": "idb.directory.sync",
        "target": _TARGET_NAME,
        "params": {"directory_id": "dir-unknown"},
    }
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        call = await call_operation(_operator(), args)
        resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "error", resume
    assert resume.extras["http_status"] == 404, resume.extras
    assert "directory.not.found" in resume.extras["upstream_message"]


# ---------------------------------------------------------------------------
# A refused token: new login, then success
# ---------------------------------------------------------------------------


async def test_401_drops_the_token_logs_in_again_and_succeeds(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_all(_stub_embedding)
    fake = _fake()
    _install_secret(monkeypatch, fake)

    async def _find() -> Any:
        return await dispatch(
            operator=_operator(),
            connector_id=IDB_WSA_CONNECTOR_ID,
            op_id="idb.user.find",
            target=target,
            params={"name": "alice"},
        )

    target = _Target()
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        first = await _find()
        fake.expire_tokens()
        second = await _find()
    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert second.result["users"][0]["user_name"] == "alice"
    assert fake.logins == 2


# ---------------------------------------------------------------------------
# A long list comes back behind a result handle
# ---------------------------------------------------------------------------


async def test_long_user_list_comes_back_as_a_handle(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_all(_stub_embedding)
    fake = _fake("idb")
    fake.users = [scim_user(f"user{i:03d}") for i in range(120)]
    _install_secret(monkeypatch, fake)
    monkeypatch.setattr(
        dispatcher_module, "_DEFAULT_REDUCER", JsonFluxReducer(store=MemoryResultStore())
    )
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        result = await dispatch(
            operator=_operator(),
            connector_id=IDB_REST_CONNECTOR_ID,
            op_id="idb.user.find",
            target=_Target("idb-rest", "9.1"),
            params={"name": "user", "limit": 200},
        )
    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.handle.total_rows == 120


# ---------------------------------------------------------------------------
# Identity Broker 9.x: the directory operations stop early
# ---------------------------------------------------------------------------


async def test_directory_list_on_identity_broker_stops_with_a_message(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_all(_stub_embedding)
    fake = _fake("idb")
    _install_secret(monkeypatch, fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        result = await dispatch(
            operator=_operator(),
            connector_id=IDB_REST_CONNECTOR_ID,
            op_id="idb.directory.list",
            target=_Target("idb-rest", "9.1.1"),
            params={},
        )
    assert result.status == "error"
    assert "VCF Identity Broker 9.1.1 has no API call" in result.model_dump_json()
    assert fake.calls == []


async def test_directory_sync_on_identity_broker_shows_the_problem_on_the_card(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_all(_stub_embedding)
    await _seed_target("idb-governed-9", "idb-rest", "9.1")
    fake = _fake("idb")
    _install_secret(monkeypatch, fake)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        call = await call_operation(
            _operator(),
            {
                "connector_id": IDB_REST_CONNECTOR_ID,
                "op_id": "idb.directory.sync",
                "target": "idb-governed-9",
                "params": {"directory_id": "dir-1"},
            },
        )
    assert call["status"] == "awaiting_approval", call
    async with get_sessionmaker()() as s:
        parked = await s.get(ApprovalRequest, UUID(call["extras"]["approval_request_id"]))
        assert parked is not None
        card = dict(parked.proposed_effect)["preview"]
    assert "has no API call for a directory sync" in card["version_problem"]


# ---------------------------------------------------------------------------
# Planted secrets never leave the connector
# ---------------------------------------------------------------------------


async def test_planted_secrets_stay_out_of_results_and_audit(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register_all(_stub_embedding)
    await _seed_target()
    fake = _fake()
    _install_secret(monkeypatch, fake)
    texts: list[str] = []
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handler)
        for op_id, params in (
            ("idb.about", {}),
            ("idb.user.find", {"name": "alice"}),
            ("idb.directory.list", {}),
        ):
            result = await dispatch(
                operator=_operator(),
                connector_id=IDB_WSA_CONNECTOR_ID,
                op_id=op_id,
                target=_Target(),
                params=params,
            )
            assert result.status == "ok", result.error
            texts.append(result.model_dump_json())
        call = await call_operation(
            _operator(),
            {
                "connector_id": IDB_WSA_CONNECTOR_ID,
                "op_id": "idb.directory.sync",
                "target": _TARGET_NAME,
                "params": {"directory_id": "dir-1"},
            },
        )
        resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
        texts.append(json.dumps(call, default=str))
        texts.append(resume.model_dump_json())
    texts.append(await _db_text())
    blob = "\n".join(texts)
    for planted in (_PLANTED_SECRET, _PLANTED_TOKEN, "user-password-must-not-show"):
        assert planted not in blob
