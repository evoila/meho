# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login an audit row names, at the edges of a secret read.

* A check runner token without a ``sub`` names the fixed text
  ``check-runner``, never the Sensor's owner.
* A read of a deleted latest version returns no secret data, so it names
  no login. This holds for ``vault.kv.read``, ``vault.kv.compare``, the
  shared target login loader, the Vault side of the secret broker and the
  Vault read of the pfSense OpenVPN user operations.
* The read of the sink secret for the approval card of ``secret.move``
  names the caller on the row of the call that waits for approval. The
  ``approval.reuse`` row of a second call holds the value of its own call,
  for the same call and for a retry with the same key.
* A child call that reads names the caller on its own row; the parent
  that reads nothing stays empty.
* A service login sees the field only on the rows of its own calls.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.audit_query.owner_scope import own_rows_sub
from meho_backplane.audit_query.query import query_audit
from meho_backplane.audit_query.schemas import AuditQueryFilters
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.secret_read_as import (
    CHECK_RUNNER_IDENTITY,
    current_secret_read_as,
    record_caller_secret_read,
    token_subject,
    track_secret_read_as,
)
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors.pfsense import ops_openvpn_user
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.connectors.secret.vault_endpoint import VaultKvSecretEndpoint
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.kv_compare import vault_kv_compare
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.composite import composite_depth_var
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_client, install_fake_vault

_TENANT = UUID("00000000-0000-0000-0000-0000000000d6")
_MOUNT = "markermount0zzz"
_PATH = "markerenv0zzz/markersvc0zzz"
_PARENT_OP = "vault.demo.parent_reads_nothing"
_CHILD_OP = "vault.demo.child_reads"


def _token(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.sig"


def _operator(
    sub: str,
    token_claims: dict[str, Any] | None = None,
    *,
    check_runner_dispatch: bool = False,
    principal_kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=_token({"sub": sub} if token_claims is None else token_claims),
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        check_runner_dispatch=check_runner_dispatch,
        principal_kind=principal_kind,
    )


class _Target:
    """A Vault target whose own login is in Vault."""

    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = type("F", (), {"version": "1.18.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "app-prod"
        self.host = "app.example.com"
        self.secret_ref = _PATH


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    monkeypatch.delenv("AUDIT_SECRET_READ_AS", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list(
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )


# ---------------------------------------------------------------------------
# The check runner
# ---------------------------------------------------------------------------


def _noted(operator: Operator) -> str | None:
    with track_secret_read_as():
        record_caller_secret_read(operator)
        return current_secret_read_as()


def test_check_runner_token_without_sub_names_the_check_runner() -> None:
    runner = _operator("sensor-owner-sub", {"azp": "runner"}, check_runner_dispatch=True)
    assert _noted(runner) == CHECK_RUNNER_IDENTITY == "check-runner"


def test_check_runner_token_with_sub_names_that_sub() -> None:
    runner = _operator("sensor-owner-sub", {"sub": "runner-sub"}, check_runner_dispatch=True)
    assert _noted(runner) == "runner-sub"


def test_ordinary_operator_is_unchanged() -> None:
    assert _noted(_operator("person-sub")) == "person-sub"
    assert _noted(_operator("person-sub", {"azp": "client"})) == "person-sub"


# ---------------------------------------------------------------------------
# A deleted latest version
# ---------------------------------------------------------------------------


def _deleted_latest_version(fake: Any) -> None:
    """The KV-v2 answer for a deleted latest version read with no raise: no data."""

    def read_secret_version(path: str, mount_point: str = "secret", **_kw: Any) -> dict[str, Any]:
        fake.secrets.kv.v2.read_calls.append({"path": path, "mount_point": mount_point})
        return {
            "data": {
                "data": None,
                "metadata": {"version": 3, "deletion_time": "2026-01-01T00:00:00Z"},
            }
        }

    fake.secrets.kv.v2.read_secret_version = read_secret_version


@pytest.mark.asyncio
async def test_kv_read_of_a_deleted_latest_version_names_no_login(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = install_fake_vault(monkeypatch)
    _deleted_latest_version(fake)
    await register_vault_typed_operations(embedding_service=embedding)
    result = await dispatch(
        operator=_operator("person-sub"),
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=_Target(),
        params={"mount": _MOUNT, "path": _PATH},
    )
    assert result.status == "ok", result.error
    assert len(fake.secrets.kv.v2.read_calls) == 1
    (row,) = await _rows("vault.kv.read")
    assert row.secret_read_as is None


@pytest.mark.asyncio
async def test_target_login_read_of_a_deleted_latest_version_names_no_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_vault(monkeypatch)
    _deleted_latest_version(fake)
    with track_secret_read_as():
        with pytest.raises(Exception):  # noqa: B017 - only the note matters here
            await load_basic_credentials(_Target(), _operator("person-sub"))
        assert current_secret_read_as() is None
    assert len(fake.secrets.kv.v2.read_calls) == 1


@pytest.mark.asyncio
async def test_broker_read_of_a_deleted_latest_version_names_no_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_vault(monkeypatch)
    _deleted_latest_version(fake)
    endpoint = VaultKvSecretEndpoint(f"{_PATH}#password")
    with track_secret_read_as():
        with pytest.raises(Exception):  # noqa: B017 - only the note matters here
            await endpoint.read_secret(_operator("person-sub"))
        assert current_secret_read_as() is None
    assert len(fake.secrets.kv.v2.read_calls) == 1


@pytest.mark.asyncio
async def test_compare_of_two_deleted_copies_names_no_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_vault(monkeypatch)
    _deleted_latest_version(fake)
    paths = [{"path": _PATH, "key": "password"}, {"path": f"{_PATH}-copy", "key": "password"}]
    with track_secret_read_as():
        result = await vault_kv_compare(_operator("person-sub"), None, {"paths": paths})
        assert current_secret_read_as() is None
    assert [copy["reason"] for copy in result["copies"]] == ["not_found", "not_found"]
    assert len(fake.secrets.kv.v2.read_calls) == 2


def _pfsense_vault(monkeypatch: pytest.MonkeyPatch, data: dict[str, Any] | None) -> None:
    """A Vault for the pfSense user operations whose KV-v2 answer holds *data*."""

    @asynccontextmanager
    async def _client(_operator: Operator, **_kw: Any) -> AsyncIterator[Any]:
        client = MagicMock()
        client.adapter.get = lambda _url: {"data": {"type": "kv", "options": {"version": "2"}}}
        client.secrets.kv.v2.read_secret_version = lambda **_kw: {
            "data": {"data": data, "metadata": {"version": 4}}
        }
        yield client

    monkeypatch.setattr(ops_openvpn_user, "vault_client_for_operator", _client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [(None, None), ({"password": "made-up-value-0zzz"}, "person-sub")],
    ids=["only-version-information", "secret-data"],
)
async def test_pfsense_vault_read_names_the_caller_only_for_secret_data(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any] | None, expected: str | None
) -> None:
    _pfsense_vault(monkeypatch, data)
    with track_secret_read_as():
        version, _ = await ops_openvpn_user._vault_state(_operator("person-sub"), _MOUNT, _PATH)
        assert current_secret_read_as() == expected
    assert version == 4


# ---------------------------------------------------------------------------
# The sink state read for the approval card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_that_waits_for_approval_names_the_caller_of_the_sink_read(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    install_fake_client(monkeypatch, secret={"password": "made-up-value-0zzz"})
    await register_secret_broker_operations(embedding_service=embedding)
    result = await dispatch(
        operator=_operator("person-sub"),
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params={
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
            "reason": "made-up reason",
        },
    )
    assert result.status == "awaiting_approval", result.error
    (row,) = await _rows("approval.request")
    assert row.secret_read_as == "person-sub"


#: How a second call gets the waiting request: the switch, and the key both calls send.
_REUSE_WAYS = [
    pytest.param("APPROVAL_SAME_CALL_REUSE", None, id="same-call"),
    pytest.param("APPROVAL_RETRY_OWN_AUDIT_ROW", "made-up-key", id="retry-with-the-same-key"),
]


async def _move_twice(
    monkeypatch: pytest.MonkeyPatch,
    embedding: AsyncMock,
    *,
    switch_on: str | None,
    key: str | None,
    second_reads_deleted: bool = False,
) -> str:
    """Send one ``secret.move`` twice with a login made up here, and return the login.

    Each call reads the sink secret for its card and waits for approval. The
    second call gets the request of the first. With *second_reads_deleted*,
    the second call's read answers a deleted latest version.
    """
    for switch in ("APPROVAL_SAME_CALL_REUSE", "APPROVAL_RETRY_OWN_AUDIT_ROW"):
        monkeypatch.setenv(switch, "true" if switch == switch_on else "false")
    get_settings.cache_clear()
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"password": "made-up-value-0zzz"}
    await register_secret_broker_operations(embedding_service=embedding)
    login = f"person-{uuid.uuid4().hex[:12]}"
    request_ids = []
    for is_second in (False, True):
        if is_second and second_reads_deleted:
            _deleted_latest_version(fake)
        reads_before = len(fake.secrets.kv.v2.read_calls)
        result = await dispatch(
            operator=_operator(login),
            connector_id="secret-broker-1.x",
            op_id="secret.move",
            target=None,
            params={
                "from": "vault:secret/db/prod#password",
                "to": "vault:secret/db/replica#password",
                "reason": "made-up reason",
            },
            idempotency_key=key,
        )
        assert result.status == "awaiting_approval", result.error
        assert len(fake.secrets.kv.v2.read_calls) == reads_before + 1
        request_ids.append(result.extras["approval_request_id"])
    assert request_ids[0] == request_ids[1]
    assert {token_subject(call["jwt"], "") for call in fake.auth.jwt.login_calls} == {login}
    return login


@pytest.mark.asyncio
@pytest.mark.parametrize(("switch", "key"), _REUSE_WAYS)
async def test_second_move_names_the_caller_on_its_reuse_row(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock, switch: str, key: str | None
) -> None:
    login = await _move_twice(monkeypatch, embedding, switch_on=switch, key=key)
    (request_row,) = await _rows("approval.request")
    (reuse_row,) = await _rows("approval.reuse")
    assert request_row.secret_read_as == login
    assert reuse_row.secret_read_as == login


@pytest.mark.asyncio
@pytest.mark.parametrize(("switch", "key"), _REUSE_WAYS)
async def test_second_move_that_reads_a_deleted_secret_leaves_its_reuse_row_empty(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock, switch: str, key: str | None
) -> None:
    login = await _move_twice(
        monkeypatch, embedding, switch_on=switch, key=key, second_reads_deleted=True
    )
    (request_row,) = await _rows("approval.request")
    (reuse_row,) = await _rows("approval.reuse")
    assert request_row.secret_read_as == login
    assert reuse_row.secret_read_as is None


@pytest.mark.asyncio
async def test_retry_of_a_move_with_the_same_key_and_the_switch_off_writes_no_reuse_row(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    login = await _move_twice(monkeypatch, embedding, switch_on=None, key="made-up-key")
    (request_row,) = await _rows("approval.request")
    assert request_row.secret_read_as == login
    assert await _rows("approval.reuse") == []


@pytest.mark.asyncio
async def test_sink_state_read_of_a_deleted_secret_names_no_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_vault(monkeypatch)
    _deleted_latest_version(fake)
    endpoint = VaultKvSecretEndpoint(f"{_PATH}#password")
    with track_secret_read_as():
        state = await endpoint.sink_state(_operator("person-sub"), merge=True)
        assert current_secret_read_as() is None
    assert state["secret_exists"] == "yes"


# ---------------------------------------------------------------------------
# A child call reads, the parent reads nothing
# ---------------------------------------------------------------------------


async def _register(op_id: str, handler: Any, embedding: AsyncMock) -> None:
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=handler,
        summary="Demo op for the secret-read-as edge tests.",
        description="Demo op used by the secret-read-as edge tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )


async def _parent_calls_child(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    depth_token = composite_depth_var.set(1)
    try:
        child = await dispatch(
            operator=operator, connector_id="vault-1.x", op_id=_CHILD_OP, target=target, params={}
        )
    finally:
        composite_depth_var.reset(depth_token)
    return {"child_status": child.status}


async def _child_reads(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    creds = await load_basic_credentials(target, operator)
    return {"user": creds["username"]}


@pytest.mark.asyncio
async def test_child_that_reads_names_the_caller_and_the_parent_stays_empty(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"username": "svc", "password": "made-up-value-0zzz"}
    await _register(_PARENT_OP, _parent_calls_child, embedding)
    await _register(_CHILD_OP, _child_reads, embedding)
    result = await dispatch(
        operator=_operator("person-sub"),
        connector_id="vault-1.x",
        op_id=_PARENT_OP,
        target=_Target(),
        params={},
    )
    assert result.status == "ok", result.error
    assert result.result["child_status"] == "ok"
    (child_row,) = await _rows(_CHILD_OP)
    (parent_row,) = await _rows(_PARENT_OP)
    assert child_row.secret_read_as == "person-sub"
    assert parent_row.secret_read_as is None


# ---------------------------------------------------------------------------
# Who sees the field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_login_sees_the_field_only_on_its_own_rows(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"password": "made-up-value-0zzz"}
    await register_vault_typed_operations(embedding_service=embedding)
    service = _operator("svc-login", principal_kind=PrincipalKind.SERVICE)
    for operator in (service, _operator("person-sub"), service):
        result = await dispatch(
            operator=operator,
            connector_id="vault-1.x",
            op_id="vault.kv.read",
            target=_Target(),
            params={"mount": _MOUNT, "path": _PATH},
        )
        assert result.status == "ok", result.error

    async def answer() -> list[dict[str, Any]]:
        async with get_sessionmaker()() as session:
            found = await query_audit(
                AuditQueryFilters(),
                tenant_id=_TENANT,
                session=session,
                owner_sub=own_rows_sub(service),
            )
        return [json.loads(row.model_dump_json()) for row in found.rows]

    off = await answer()
    monkeypatch.setenv("AUDIT_SECRET_READ_AS", "true")
    get_settings.cache_clear()
    on = await answer()

    assert [row["id"] for row in on] == [row["id"] for row in off]
    assert len(on) == 2
    assert all(row["principal_sub"] == "svc-login" for row in on)
    assert [row["secret_read_as"] for row in on] == ["svc-login", "svc-login"]
    assert all("secret_read_as" not in row for row in off)
