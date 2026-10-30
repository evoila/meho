# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for moving a secret to another MEHO instance in two approved steps.

``secret.handoff.export`` runs on instance A: it reads a value and puts it
into a one-time Vault handoff (response wrapping) on A's store.
``secret.handoff.import`` runs on instance B: it uses the handoff once at
A's store and writes the value into B's store.

Each test uses in-process fake stores (:class:`_FakeStore`), no real Vault.
A fake store keeps KV-v2 fields and handoffs; a handoff works once. Two
separate fake stores stand for the two instances' stores. "This instance's
store" is whichever fake the ``auth.vault._build_client`` seam returns at
the moment; the other instance's store is reached over HTTP through the
``handoff._build_store_client`` seam: a mock transport routes each request
to a fake store by URL, so the real request, header and status handling
runs.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import hvac.exceptions
import pytest
import structlog
from cryptography.fernet import Fernet
from sqlalchemy import select

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors._shared.pinned_transport import _PinnedAsyncBackend
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret import handoff as handoff_module
from meho_backplane.connectors.secret.handoff import (
    SecretHandoffDestinationBlockedError,
    SecretHandoffDigestMismatchError,
    SecretHandoffInvalidError,
    SecretHandoffRefusedError,
    SecretHandoffSinkWriteError,
    SecretHandoffStoreError,
    SecretHandoffUnreachableError,
    secret_handoff_export,
    secret_handoff_import,
)
from meho_backplane.connectors.secret.handoff_ops import register_secret_handoff_operations
from meho_backplane.connectors.secret.handoff_preview import (
    build_secret_handoff_export_preview,
    build_secret_handoff_import_preview,
)
from meho_backplane.connectors.vault.tenant_scope import VaultTenantScopeError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations._vendor_text import VENDOR_TEXT_WITHHELD
from meho_backplane.settings import get_settings
from meho_backplane.targets.ssrf_guard import TARGET_SSRF_ALLOWLIST_ENV

#: The moved value. Generated so no literal secret sits in the source.
_VALUE = f"handoff-value-{secrets.token_hex(6)}"
_VALUE_SHA256 = hashlib.sha256(_VALUE.encode()).hexdigest()

_TENANT_A = UUID("aaaaaaaa-0000-0000-0000-00000000000a")
_TENANT_B = UUID("bbbbbbbb-0000-0000-0000-00000000000b")

_STORE_A_HOST = "store-a.test.invalid"
_STORE_A_URL = f"https://{_STORE_A_HOST}:8200"
_STORE_TARGET = "instance-a-vault"

_SOURCE_PATH = f"tenants/{_TENANT_A}/targets/db"
_SINK_PATH = f"tenants/{_TENANT_B}/targets/db"
_FROM = f"vault:{_SOURCE_PATH}#password"
_TO = f"vault:{_SINK_PATH}#password"


# ---------------------------------------------------------------------------
# Fake stores
# ---------------------------------------------------------------------------


class _FakeStore:
    """One Vault-API store: KV-v2 fields plus one-time handoffs."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.kv: dict[str, dict[str, str]] = {}
        self.handoffs: dict[str, dict[str, Any]] = {}
        self.login_exc: Exception | None = None
        self.wrap_exc: Exception | None = None
        #: When set, every unwrap answers this HTTP status instead of working.
        self.unwrap_status: int | None = None
        self.unwrap_calls = 0
        self.writes: list[tuple[str, dict[str, str]]] = []

    def client(self, token: str | None = None) -> _FakeStoreClient:
        return _FakeStoreClient(self, token)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one HTTP request the way Vault's unwrap endpoint does."""
        if request.method != "POST" or request.url.path != "/v1/sys/wrapping/unwrap":
            return httpx.Response(404, json={"errors": []})
        self.unwrap_calls += 1
        if self.unwrap_status is not None:
            headers = {"location": "https://elsewhere.test.invalid/v1/sys/wrapping/unwrap"}
            return httpx.Response(self.unwrap_status, headers=headers, json={"errors": []})
        payload = self.handoffs.pop(request.headers.get("X-Vault-Token", ""), None)
        if payload is None:
            return httpx.Response(
                400, json={"errors": ["wrapping token is not valid or does not exist"]}
            )
        return httpx.Response(200, json={"data": payload})


class _NS:
    """A tiny attribute bag for hvac's nested client namespaces."""


class _FakeStoreClient:
    """The slice of ``hvac.Client`` the broker and the handoff steps call."""

    def __init__(self, store: _FakeStore, token: str | None) -> None:
        self.store = store
        self.token = token
        self.auth = _NS()
        self.auth.jwt = _NS()
        self.auth.jwt.jwt_login = self._jwt_login
        self.auth.token = _NS()
        self.auth.token.revoke_self = lambda: None
        self.secrets = _NS()
        self.secrets.kv = _NS()
        self.secrets.kv.v2 = _NS()
        self.secrets.kv.v2.read_secret_version = self._read
        self.secrets.kv.v2.create_or_update_secret = self._write
        self.sys = _NS()
        self.sys.wrap = self._wrap

    def _jwt_login(self, role: str, jwt: str, path: str | None = None) -> dict[str, Any]:
        del role, jwt, path
        if self.store.login_exc is not None:
            raise self.store.login_exc
        self.token = f"login-{secrets.token_hex(4)}"
        return {"auth": {"client_token": self.token}}

    def _read(self, path: str, mount_point: str, raise_on_deleted_version: bool) -> dict[str, Any]:
        del mount_point, raise_on_deleted_version
        return {"data": {"data": dict(self.store.kv.get(path, {}))}}

    def _write(self, path: str, secret: dict[str, str], cas: Any, mount_point: str) -> None:
        del cas, mount_point
        self.store.kv[path] = dict(secret)
        self.store.writes.append((path, dict(secret)))

    def _wrap(self, payload: dict[str, Any], ttl: int) -> dict[str, Any]:
        if self.store.wrap_exc is not None:
            raise self.store.wrap_exc
        token = f"wrap-{secrets.token_hex(8)}"
        self.store.handoffs[token] = dict(payload)
        return {"wrap_info": {"token": token, "ttl": ttl, "creation_path": "sys/wrapping/wrap"}}


class _Instances:
    """Two instances' stores, and a switch for which one is 'this instance'."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store_a = _FakeStore("a")
        self.store_b = _FakeStore("b")
        self.local = self.store_a
        self.remote_by_url: dict[str, _FakeStore] = {_STORE_A_URL: self.store_a}
        self.remote_calls: list[dict[str, Any]] = []
        self.remote_requests: list[httpx.Request] = []

        def _build_local(_settings: Any, *, token: str | None = None) -> _FakeStoreClient:
            return self.local.client(token)

        def _handle(request: httpx.Request) -> httpx.Response:
            self.remote_requests.append(request)
            store = self.remote_by_url.get(f"{request.url.scheme}://{request.url.netloc.decode()}")
            if store is None:
                raise httpx.ConnectError("name or service not known", request=request)
            return store.handle(request)

        def _build_remote(store: handoff_module._RemoteStore) -> httpx.AsyncClient:
            self.remote_calls.append({"url": store.url, "verify": store.verify})
            return httpx.AsyncClient(
                base_url=store.url, transport=httpx.MockTransport(_handle), follow_redirects=False
            )

        monkeypatch.setattr(vault_module, "_build_client", _build_local)
        monkeypatch.setattr(handoff_module, "_build_store_client", _build_remote)

    def on_a(self) -> None:
        self.local = self.store_a

    def on_b(self) -> None:
        self.local = self.store_b


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "secret/tenants/{tenant_id}/")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def instances(monkeypatch: pytest.MonkeyPatch) -> _Instances:
    pair = _Instances(monkeypatch)
    pair.store_a.kv[_SOURCE_PATH] = {"password": _VALUE, "username": "svc"}
    return pair


@pytest.fixture
async def _registered_ops() -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_secret_handoff_operations(embedding_service=service)
    yield


@pytest.fixture
async def store_target() -> None:
    """On instance B: a target whose host is instance A's store."""
    await _seed_target(_STORE_TARGET, product="vault")


async def _seed_target(name: str, *, product: str, host: str = _STORE_A_HOST) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_B,
                name=name,
                aliases=[],
                product=product,
                host=host,
                port=8200,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes="seeded by test_secret_broker_handoff",
            )
        )
        await session.commit()


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub=f"operator-{tenant_id.hex[:4]}",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _import_params(token: str, **extra: Any) -> dict[str, Any]:
    return {"handoff_token": token, "store": _STORE_TARGET, "to": _TO, **extra}


async def _export_on_a(instances: _Instances) -> dict[str, Any]:
    instances.on_a()
    return await secret_handoff_export(_operator(_TENANT_A), None, {"from": _FROM})


async def _dispatch(
    tenant_id: UUID, op_id: str, params: dict[str, Any], *, approved: bool = True
) -> OperationResult:
    return await dispatch(
        operator=_operator(tenant_id),
        connector_id="secret-broker-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=approved,
    )


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Registration + classification
# ---------------------------------------------------------------------------


async def test_both_steps_are_dangerous_and_need_approval(_registered_ops: None) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.impl_id == "secret-broker",
                    EndpointDescriptor.op_id.in_(
                        ["secret.handoff.export", "secret.handoff.import"]
                    ),
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    assert set(by_op) == {"secret.handoff.export", "secret.handoff.import"}
    for row in by_op.values():
        assert row.safety_level == "dangerous"
        assert row.requires_approval is True


def test_steps_are_pinned_as_credential_classes() -> None:
    # Export returns the handoff; import carries it in params.
    assert classify_op("secret.handoff.export") == "credential_mint"
    assert classify_op("secret.handoff.import") == "credential_write"


# ---------------------------------------------------------------------------
# Export on A
# ---------------------------------------------------------------------------


async def test_export_returns_handoff_hash_and_length_never_the_value(
    instances: _Instances,
) -> None:
    with structlog.testing.capture_logs() as logs:
        result = await _export_on_a(instances)

    assert result["status"] == "exported"
    assert result["value_sha256"] == _VALUE_SHA256
    assert result["length"] == len(_VALUE.encode())
    assert result["handoff_ttl_seconds"] == 3600
    # The handoff is a reference into store A, not the value.
    assert instances.store_a.handoffs[result["handoff_token"]] == {"value": _VALUE}
    assert _VALUE not in json.dumps(result)
    assert _VALUE not in repr(logs)


async def test_export_keeps_the_source_tenant_guard(instances: _Instances) -> None:
    instances.on_a()
    foreign = f"vault:tenants/{_TENANT_B}/targets/db#password"
    with pytest.raises(VaultTenantScopeError):
        await secret_handoff_export(_operator(_TENANT_A), None, {"from": foreign})
    assert instances.store_a.handoffs == {}


async def test_export_refused_wrap_names_store_and_path(instances: _Instances) -> None:
    """The store refuses the wrap call (no right on the path): a plain error."""
    instances.on_a()
    instances.store_a.wrap_exc = hvac.exceptions.Forbidden("permission denied")
    with pytest.raises(SecretHandoffRefusedError) as excinfo:
        await secret_handoff_export(_operator(_TENANT_A), None, {"from": _FROM})
    message = str(excinfo.value)
    assert "sys/wrapping/wrap" in message
    assert "this instance's secret store" in message
    assert _VALUE not in message


async def test_export_refused_jwt_login_maps_to_refused(
    instances: _Instances, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused Vault login on the wrap step is a plain 'refused' error."""
    instances.on_a()
    calls = {"n": 0}
    original = instances.store_a.client

    def _second_login_refused(token: str | None = None) -> _FakeStoreClient:
        calls["n"] += 1
        client = original(token)
        if calls["n"] == 2:
            instances.store_a.login_exc = hvac.exceptions.Forbidden("role denied")
        return client

    monkeypatch.setattr(instances.store_a, "client", _second_login_refused)
    with pytest.raises(SecretHandoffRefusedError) as excinfo:
        await secret_handoff_export(_operator(_TENANT_A), None, {"from": _FROM})
    assert "sys/wrapping/wrap" in str(excinfo.value)
    assert instances.store_a.handoffs == {}


# ---------------------------------------------------------------------------
# Import on B
# ---------------------------------------------------------------------------


async def test_import_writes_value_into_the_other_store(
    instances: _Instances, store_target: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()

    with structlog.testing.capture_logs() as logs:
        result = await secret_handoff_import(
            _operator(_TENANT_B),
            None,
            _import_params(exported["handoff_token"], expect_sha256=exported["value_sha256"]),
        )

    assert result == {
        "status": "imported",
        "value_sha256": _VALUE_SHA256,
        "length": len(_VALUE.encode()),
    }
    assert instances.store_b.kv[_SINK_PATH] == {"password": _VALUE}
    # Store A was only unwrapped, never written by the import.
    assert instances.store_a.writes == []
    assert instances.remote_calls == [{"url": _STORE_A_URL, "verify": True}]
    assert _VALUE not in repr(logs)


async def test_import_handoff_used_twice_fails_plainly(
    instances: _Instances, store_target: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()
    params = _import_params(exported["handoff_token"])
    await secret_handoff_import(_operator(_TENANT_B), None, params)

    with pytest.raises(SecretHandoffInvalidError) as excinfo:
        await secret_handoff_import(_operator(_TENANT_B), None, params)

    message = str(excinfo.value)
    assert _STORE_TARGET in message
    assert "sys/wrapping/unwrap" in message
    assert "already used" in message
    assert _VALUE not in message
    assert exported["handoff_token"] not in message
    assert len(instances.store_b.writes) == 1


async def test_import_wrong_address_fails_plainly(
    instances: _Instances,
) -> None:
    exported = await _export_on_a(instances)
    await _seed_target(_STORE_TARGET, product="vault", host="wrong-host.test.invalid")
    instances.on_b()

    with pytest.raises(SecretHandoffUnreachableError) as excinfo:
        await secret_handoff_import(
            _operator(_TENANT_B), None, _import_params(exported["handoff_token"])
        )

    message = str(excinfo.value)
    assert "wrong-host.test.invalid" in message
    assert _STORE_TARGET in message
    assert "sys/wrapping/unwrap" in message
    assert _VALUE not in message
    assert instances.store_b.writes == []


async def test_import_refused_by_the_other_store_fails_plainly(
    instances: _Instances, store_target: None
) -> None:
    """A store that answers 403 (token unknown there) gives the same plain error."""
    instances.on_b()
    instances.store_a.unwrap_status = 403

    with pytest.raises(SecretHandoffInvalidError) as excinfo:
        await secret_handoff_import(_operator(_TENANT_B), None, _import_params("not-a-handoff"))
    assert "sys/wrapping/unwrap" in str(excinfo.value)
    assert instances.store_b.writes == []


async def test_import_refused_sink_login_says_handoff_is_used(
    instances: _Instances, store_target: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()
    instances.store_b.login_exc = hvac.exceptions.Forbidden("role denied")

    with pytest.raises(SecretHandoffSinkWriteError) as excinfo:
        await secret_handoff_import(
            _operator(_TENANT_B), None, _import_params(exported["handoff_token"])
        )

    message = str(excinfo.value)
    assert f"vault:{_SINK_PATH}#password" in message
    assert "make a new handoff" in message
    assert "role denied" not in message
    assert _VALUE not in message


async def test_import_checks_sink_tenant_before_using_the_handoff(
    instances: _Instances, store_target: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()
    foreign_sink = f"vault:tenants/{_TENANT_A}/targets/db#password"

    with pytest.raises(VaultTenantScopeError):
        await secret_handoff_import(
            _operator(_TENANT_B),
            None,
            {
                "handoff_token": exported["handoff_token"],
                "store": _STORE_TARGET,
                "to": foreign_sink,
            },
        )

    # The handoff was not used, so a corrected import still works.
    assert instances.store_a.unwrap_calls == 0
    assert exported["handoff_token"] in instances.store_a.handoffs


async def test_import_digest_mismatch_writes_nothing(
    instances: _Instances, store_target: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()

    with pytest.raises(SecretHandoffDigestMismatchError) as excinfo:
        await secret_handoff_import(
            _operator(_TENANT_B),
            None,
            _import_params(exported["handoff_token"], expect_sha256="f" * 64),
        )

    assert _VALUE not in str(excinfo.value)
    assert instances.store_b.writes == []


async def test_import_refuses_a_store_target_that_is_not_vault(instances: _Instances) -> None:
    exported = await _export_on_a(instances)
    await _seed_target(_STORE_TARGET, product="linux")
    instances.on_b()

    with pytest.raises(SecretHandoffStoreError, match="product 'linux'"):
        await secret_handoff_import(
            _operator(_TENANT_B), None, _import_params(exported["handoff_token"])
        )
    assert instances.remote_calls == []


# ---------------------------------------------------------------------------
# The other store's address: screened, pinned, no redirects
# ---------------------------------------------------------------------------


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *ips: str) -> None:
    """Point the destination guard's DNS seam at a fixed answer (no real DNS)."""
    import ipaddress

    addrs = [ipaddress.ip_address(ip) for ip in ips]
    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", lambda host: addrs)


async def test_store_client_pins_the_screened_address_and_follows_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real client dials only an address the guard passed, and never follows a redirect."""
    monkeypatch.delenv(TARGET_SSRF_ALLOWLIST_ENV, raising=False)
    store = handoff_module._RemoteStore(
        label=f"store target {_STORE_TARGET!r}", host=_STORE_A_HOST, url=_STORE_A_URL, verify=True
    )
    client = handoff_module._build_store_client(store)
    try:
        assert client.follow_redirects is False
        backend = client._transport._pool._network_backend  # type: ignore[attr-defined]
        assert isinstance(backend, _PinnedAsyncBackend)

        _resolve_to(monkeypatch, "93.184.216.34")
        assert backend._resolver(_STORE_A_HOST) == ["93.184.216.34"]

        # The resolver flips to a private address when the socket opens.
        _resolve_to(monkeypatch, "10.0.0.5")
        with pytest.raises(SecretHandoffDestinationBlockedError) as excinfo:
            backend._resolver(_STORE_A_HOST)
        assert "10.0.0.5" not in str(excinfo.value)
    finally:
        await client.aclose()


async def test_import_refuses_a_store_whose_address_is_now_blocked(
    instances: _Instances,
    store_target: None,
    _registered_ops: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After registration the store's name resolves to a private address: nothing is sent."""
    exported = await _export_on_a(instances)
    instances.on_b()
    monkeypatch.delenv(TARGET_SSRF_ALLOWLIST_ENV, raising=False)
    _resolve_to(monkeypatch, "10.0.0.5")

    result = await _dispatch(
        _TENANT_B, "secret.handoff.import", _import_params(exported["handoff_token"])
    )

    assert result.status == "error"
    assert result.error == "connector_error: SecretHandoffDestinationBlockedError"
    remediation = result.extras["remediation"]
    assert _STORE_TARGET in remediation
    assert TARGET_SSRF_ALLOWLIST_ENV in remediation
    assert "still usable" in remediation
    assert "10.0.0.5" not in remediation
    assert instances.remote_requests == []
    assert exported["handoff_token"] in instances.store_a.handoffs
    assert instances.store_b.writes == []
    assert exported["handoff_token"] not in result.model_dump_json()


async def test_import_does_not_follow_a_redirect(instances: _Instances, store_target: None) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()
    instances.store_a.unwrap_status = 302

    with pytest.raises(SecretHandoffStoreError) as excinfo:
        await secret_handoff_import(
            _operator(_TENANT_B), None, _import_params(exported["handoff_token"])
        )

    message = str(excinfo.value)
    assert "redirect" in message
    assert "HTTP 302" in message
    assert _STORE_TARGET in message
    assert "elsewhere.test.invalid" not in message
    # One request to the store, none to the address it pointed at.
    assert [str(req.url) for req in instances.remote_requests] == [
        f"{_STORE_A_URL}/v1/sys/wrapping/unwrap"
    ]
    assert instances.store_b.writes == []


async def test_import_sends_the_handoff_as_the_vault_token(
    instances: _Instances, store_target: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()
    await secret_handoff_import(
        _operator(_TENANT_B), None, _import_params(exported["handoff_token"])
    )

    (request,) = instances.remote_requests
    assert request.method == "POST"
    assert str(request.url) == f"{_STORE_A_URL}/v1/sys/wrapping/unwrap"
    assert request.headers["X-Vault-Token"] == exported["handoff_token"]
    assert json.loads(request.content) == {}


# ---------------------------------------------------------------------------
# End to end through the dispatcher: two instances, two stores, two audits
# ---------------------------------------------------------------------------


async def test_move_between_two_stores_with_matching_audit_hashes(
    instances: _Instances, store_target: None, _registered_ops: None
) -> None:
    instances.on_a()
    exported = await _dispatch(
        _TENANT_A, "secret.handoff.export", {"from": _FROM, "reason": "move target db"}
    )
    assert exported.status == "ok", exported.error
    assert exported.result is not None
    token = exported.result["handoff_token"]

    instances.on_b()
    imported = await _dispatch(
        _TENANT_B,
        "secret.handoff.import",
        _import_params(token, expect_sha256=exported.result["value_sha256"], reason="move db"),
    )
    assert imported.status == "ok", imported.error
    assert imported.result == {
        "status": "imported",
        "value_sha256": _VALUE_SHA256,
        "length": len(_VALUE.encode()),
    }

    # The value moved from store A into store B; the two stores are separate.
    assert instances.store_b.kv[_SINK_PATH] == {"password": _VALUE}
    assert _SINK_PATH not in instances.store_a.kv
    assert instances.store_a.handoffs == {}

    # Each side wrote its own audit record; both carry the same SHA-256.
    export_rows = await _audit_rows("secret.handoff.export")
    import_rows = await _audit_rows("secret.handoff.import")
    assert len(export_rows) == 1
    assert len(import_rows) == 1
    assert export_rows[0].tenant_id == _TENANT_A
    assert import_rows[0].tenant_id == _TENANT_B
    assert export_rows[0].raw_payload["value_sha256"] == _VALUE_SHA256
    assert import_rows[0].raw_payload["value_sha256"] == _VALUE_SHA256

    # The value is in no result and no audit column on either side.
    for result in (exported, imported):
        assert _VALUE not in result.model_dump_json()
    for row in (*export_rows, *import_rows):
        assert _VALUE not in json.dumps(row.payload, default=str)
        assert _VALUE not in json.dumps(row.raw_payload, default=str)


async def test_both_steps_wait_for_approval(
    instances: _Instances,
    store_target: None,
    _registered_ops: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    instances.on_a()
    export = await _dispatch(_TENANT_A, "secret.handoff.export", {"from": _FROM}, approved=False)
    assert export.status == "awaiting_approval", export.error
    assert instances.store_a.handoffs == {}

    instances.on_b()
    imported = await _dispatch(
        _TENANT_B, "secret.handoff.import", _import_params("some-handoff"), approved=False
    )
    assert imported.status == "awaiting_approval", imported.error
    assert instances.store_a.unwrap_calls == 0
    assert instances.store_b.writes == []


# ---------------------------------------------------------------------------
# Failures through the dispatcher: the message is withheld, the remediation stays
# ---------------------------------------------------------------------------


def _assert_message_withheld(result: OperationResult, exception_class: str) -> str:
    """Both ops send a secret, so the raised message is withheld. Return the remediation."""
    assert result.status == "error"
    assert result.error == f"connector_error: {exception_class}"
    assert result.extras["exception_class"] == exception_class
    assert result.extras["exception_message"] == VENDOR_TEXT_WITHHELD
    remediation = result.extras["remediation"]
    assert isinstance(remediation, str) and remediation
    assert _VALUE not in result.model_dump_json()
    return remediation


async def test_dispatch_import_wrong_address_names_store_path_and_next_step(
    instances: _Instances, _registered_ops: None
) -> None:
    exported = await _export_on_a(instances)
    await _seed_target(_STORE_TARGET, product="vault", host="wrong-host.test.invalid")
    instances.on_b()

    result = await _dispatch(
        _TENANT_B, "secret.handoff.import", _import_params(exported["handoff_token"])
    )

    remediation = _assert_message_withheld(result, "SecretHandoffUnreachableError")
    assert _STORE_TARGET in remediation
    assert "wrong-host.test.invalid" in remediation
    assert "sys/wrapping/unwrap" in remediation
    assert "check the address" in remediation
    assert "same handoff" in remediation
    assert exported["handoff_token"] not in result.model_dump_json()
    assert instances.store_b.writes == []


async def test_dispatch_export_refused_by_the_store_names_store_and_path(
    instances: _Instances, _registered_ops: None
) -> None:
    instances.on_a()
    instances.store_a.wrap_exc = hvac.exceptions.Forbidden("permission denied")

    result = await _dispatch(_TENANT_A, "secret.handoff.export", {"from": _FROM})

    remediation = _assert_message_withheld(result, "SecretHandoffRefusedError")
    assert "this instance's secret store" in remediation
    assert "sys/wrapping/wrap" in remediation
    assert "secret.handoff.export again" in remediation
    assert "permission denied" not in remediation


async def test_dispatch_import_used_handoff_says_make_a_new_one(
    instances: _Instances, store_target: None, _registered_ops: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()
    params = _import_params(exported["handoff_token"])
    first = await _dispatch(_TENANT_B, "secret.handoff.import", params)
    assert first.status == "ok", first.error

    second = await _dispatch(_TENANT_B, "secret.handoff.import", params)

    remediation = _assert_message_withheld(second, "SecretHandoffInvalidError")
    assert _STORE_TARGET in remediation
    assert "sys/wrapping/unwrap" in remediation
    assert "already used" in remediation
    assert "secret.handoff.export" in remediation
    assert exported["handoff_token"] not in second.model_dump_json()
    assert len(instances.store_b.writes) == 1


async def test_dispatch_import_refused_sink_login_keeps_no_sink_text(
    instances: _Instances, store_target: None, _registered_ops: None
) -> None:
    exported = await _export_on_a(instances)
    instances.on_b()
    instances.store_b.login_exc = hvac.exceptions.Forbidden("role denied")

    result = await _dispatch(
        _TENANT_B, "secret.handoff.import", _import_params(exported["handoff_token"])
    )

    remediation = _assert_message_withheld(result, "SecretHandoffSinkWriteError")
    assert f"vault:{_SINK_PATH}#password" in remediation
    assert "handoff was used" in remediation
    assert "new handoff" in remediation
    assert "role denied" not in remediation


# ---------------------------------------------------------------------------
# Approval previews: addresses only, never the handoff
# ---------------------------------------------------------------------------


def _preview_ctx(params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # the builders read params only
        connector_instance=None,
        operator=_operator(_TENANT_B),
        target=None,
        params=params,
    )


async def test_import_preview_never_shows_the_handoff() -> None:
    token = f"wrap-{secrets.token_hex(8)}"
    preview = await build_secret_handoff_import_preview(
        _preview_ctx(_import_params(token, expect_sha256="A" * 64, reason="move db"))
    )
    assert preview == {
        "action": "secret.handoff.import",
        "store_target": _STORE_TARGET,
        "sink": {"kind": "vault", "ref": f"{_SINK_PATH}#password"},
        "expect_sha256": "a" * 64,
        "reason": "move db",
    }
    assert token not in json.dumps(preview)


async def test_export_preview_shows_source_and_life() -> None:
    preview = await build_secret_handoff_export_preview(
        _preview_ctx({"from": _FROM, "ttl_seconds": 600})
    )
    assert preview == {
        "action": "secret.handoff.export",
        "source": {"kind": "vault", "ref": f"{_SOURCE_PATH}#password"},
        "handoff_ttl_seconds": 600,
    }
