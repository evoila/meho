# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the per-target ``secret_field_map`` (#3711).

The map is ``{connector_field: source_field}``. It lets a secret whose keys
differ from the ones a connector reads back the target. These tests cover:

* (a) an SSH target backed by a secret keyed ``private_key`` -- via the map
  and via the built-in alias (the alias test sits with the other
  ``_auth_config`` tests in ``test_connectors_ssh_adapter.py``);
* (b) a guest-ops target backed by a secret keyed ``username`` /
  ``password`` mapped to ``guest_username`` / ``guest_password``;
* (c) the probe fails with a clear 422 when a mapped source field is
  missing from the secret;
* (d) a target with no map reads every field by its own name, as before;
* the create / update round trip and the PATCH merge rules;
* no secret value in a log event or an error.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncssh
import pytest
import respx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_basic_credentials,
    load_vault_secret_data,
    missing_secret_field_map_sources,
)
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.connectors.vmware_rest.composites import _guest
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import (
    SECRET_FIELD_MAP_MAX_ENTRIES,
    TargetCreate,
    TargetUpdate,
)
from meho_backplane.targets.secret_field_map import merge_secret_field_map

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._vault_fakes import install_fake_client

# Canary values: never real credentials. The no-leak tests search for them.
_CANARY_USER = "guest-canary-user"
_CANARY_PASSWORD = "guest-canary-pw-must-not-leak"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars Settings, the JWT check and the Vault login read."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    clear_registry()
    yield
    clear_registry()
    clear_jwks_cache()
    get_settings.cache_clear()


@dataclass
class _Target:
    """A target-like object carrying an optional ``secret_field_map``."""

    name: str = "srv-01"
    host: str = "srv-01.example.test"
    secret_ref: str | None = "targets/srv-01"
    secret_field_map: dict[str, str] = field(default_factory=dict)


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


class _SshConnector(SshConnector):
    """Concrete SSH connector; only ``_auth_config`` is exercised."""

    product = "test-ssh"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


def _private_key_pem() -> str:
    return asyncssh.generate_private_key("ssh-ed25519").export_private_key("pkcs8-pem").decode()


# ---------------------------------------------------------------------------
# (a) SSH target backed by a secret keyed ``private_key`` -- via the map
# ---------------------------------------------------------------------------


async def test_ssh_key_resolves_through_the_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ssh_private_key`` mapped to ``private_key`` picks key auth."""
    install_fake_client(
        monkeypatch,
        secret={
            "private_key": _private_key_pem(),
            "login": "ops",
            "known_hosts_insecure": True,
        },
    )
    target = _Target(secret_field_map={"ssh_private_key": "private_key", "username": "login"})

    cfg = await _SshConnector()._auth_config(target, _operator())

    assert cfg["username"] == "ops"
    assert len(cfg["client_keys"]) == 1
    assert "password" not in cfg


# ---------------------------------------------------------------------------
# (b) guest-ops target backed by a plain ``username`` / ``password`` secret
# ---------------------------------------------------------------------------


async def test_guest_credentials_resolve_through_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``guest_username`` / ``guest_password`` read the plain fields."""
    install_fake_client(
        monkeypatch, secret={"username": _CANARY_USER, "password": _CANARY_PASSWORD}
    )
    target = _Target(secret_field_map={"guest_username": "username", "guest_password": "password"})

    auth = await _guest._guest_auth(
        None,  # type: ignore[arg-type]  # credentials resolve off the target
        target,
        _operator(),
        guest_secret_ref=None,
    )

    assert auth["username"] == _CANARY_USER
    assert auth["password"] == _CANARY_PASSWORD


async def test_guest_credentials_missing_without_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the map the same secret still fails, naming the guest field."""
    install_fake_client(
        monkeypatch, secret={"username": _CANARY_USER, "password": _CANARY_PASSWORD}
    )

    with pytest.raises(VaultCredentialsReadError, match="guest_username"):
        await _guest._guest_auth(
            None,  # type: ignore[arg-type]
            _Target(),
            _operator(),
            guest_secret_ref=None,
        )


async def test_mapped_missing_source_error_names_both_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mapped field whose source is absent names both, and no value."""
    install_fake_client(monkeypatch, secret={"password": _CANARY_PASSWORD})
    target = _Target(secret_field_map={"username": "login"})

    with pytest.raises(VaultCredentialsReadError) as excinfo:
        await load_basic_credentials(target, _operator())

    message = str(excinfo.value)
    assert "'username'" in message
    assert "'login'" in message
    assert _CANARY_PASSWORD not in message


# ---------------------------------------------------------------------------
# (d) the unmapped path is unchanged
# ---------------------------------------------------------------------------


async def test_unmapped_target_reads_fields_by_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No map: the raw secret comes back exactly as stored."""
    secret = {"username": _CANARY_USER, "password": _CANARY_PASSWORD, "extra": "x"}
    install_fake_client(monkeypatch, secret=secret)

    assert await load_vault_secret_data(_Target(), _operator()) == secret
    assert await load_basic_credentials(_Target(), _operator()) == {
        "username": _CANARY_USER,
        "password": _CANARY_PASSWORD,
    }


async def test_unmapped_fields_still_resolve_by_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A map entry for one field leaves every other field as it was."""
    install_fake_client(monkeypatch, secret={"login": _CANARY_USER, "password": _CANARY_PASSWORD})
    target = _Target(secret_field_map={"username": "login"})

    creds = await load_basic_credentials(target, _operator())

    assert creds == {"username": _CANARY_USER, "password": _CANARY_PASSWORD}


async def test_map_wins_over_a_field_with_the_connector_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``secret[map.get(X, X)]``: a mapped field never falls back to ``X``."""
    install_fake_client(monkeypatch, secret={"username": "stale", "login": _CANARY_USER})
    target = _Target(secret_field_map={"username": "login"})

    data = await load_vault_secret_data(target, _operator())

    assert data["username"] == _CANARY_USER


async def test_param_stub_target_is_never_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub with no ``secret_field_map`` attribute reads fields as stored."""

    @dataclass
    class _Stub:
        name: str = "srv-01"
        host: str = "h"
        secret_ref: str | None = "targets/other"

    install_fake_client(monkeypatch, secret={"username": _CANARY_USER})

    assert await load_vault_secret_data(_Stub(), _operator()) == {"username": _CANARY_USER}


async def test_no_secret_value_in_log_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mapped reads log field names only."""
    install_fake_client(monkeypatch, secret={"login": _CANARY_USER, "pw": _CANARY_PASSWORD})
    target = _Target(secret_field_map={"username": "login", "password": "pw"})

    with capture_logs() as events:
        await load_basic_credentials(target, _operator())
        await load_vault_secret_data(target, _operator())

    rendered = repr(events)
    assert _CANARY_USER not in rendered
    assert _CANARY_PASSWORD not in rendered


# ---------------------------------------------------------------------------
# Probe-time source check -- helper level
# ---------------------------------------------------------------------------


async def test_missing_sources_lists_only_absent_source_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"private_key": "k", "password": _CANARY_PASSWORD})
    target = _Target(
        secret_field_map={"ssh_private_key": "private_key", "username": "login", "x": "zeta"}
    )

    assert await missing_secret_field_map_sources(target, _operator()) == ["login", "zeta"]


async def test_missing_sources_skips_the_read_without_a_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={})

    assert await missing_secret_field_map_sources(_Target(), _operator()) == []
    assert fake.secrets.kv.v2.read_calls == []


# ---------------------------------------------------------------------------
# Schemas and the PATCH merge rules
# ---------------------------------------------------------------------------


def test_create_rejects_empty_names_and_too_many_entries() -> None:
    base = {"name": "t", "product": "ssh", "host": "t.example.test"}
    for bad in ({"": "x"}, {"x": ""}, {"x": "   "}):
        with pytest.raises(ValidationError):
            TargetCreate(**base, secret_field_map=bad)
    too_many = {f"f{i}": "s" for i in range(SECRET_FIELD_MAP_MAX_ENTRIES + 1)}
    with pytest.raises(ValidationError):
        TargetCreate(**base, secret_field_map=too_many)


def test_create_strips_whitespace_around_names() -> None:
    body = TargetCreate(
        name="t", product="ssh", host="t.example.test", secret_field_map={" a ": " b "}
    )
    assert body.secret_field_map == {"a": "b"}


def test_update_accepts_null_entry_values() -> None:
    assert TargetUpdate(secret_field_map={"a": None}).secret_field_map == {"a": None}


def test_merge_adds_replaces_and_removes_entries() -> None:
    current = {"a": "1", "b": "2"}
    assert merge_secret_field_map(current, {"b": "20", "c": "3", "a": None}) == {
        "b": "20",
        "c": "3",
    }
    assert current == {"a": "1", "b": "2"}


def test_merge_null_clears_and_empty_result_is_none() -> None:
    assert merge_secret_field_map({"a": "1"}, None) is None
    assert merge_secret_field_map({"a": "1"}, {"a": None}) is None
    assert merge_secret_field_map(None, {"a": "1"}) == {"a": "1"}


def test_merge_rejects_a_result_over_the_limit() -> None:
    current = {f"f{i}": "s" for i in range(SECRET_FIELD_MAP_MAX_ENTRIES)}
    with pytest.raises(HTTPException) as excinfo:
        merge_secret_field_map(current, {"one-more": "s"})
    assert excinfo.value.status_code == 422


# ---------------------------------------------------------------------------
# REST round trip + (c) probe-time rejection
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _token(key: Any, role: TenantRole) -> str:
    return mint_token(key, sub="admin-1", tenant_role=role.value, tenant_id=DEFAULT_TENANT_ID)


async def _insert_target(**kwargs: Any) -> None:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.UUID(DEFAULT_TENANT_ID),
        "name": "ssh-01",
        "product": "linux-ssh-test",
        "host": "ssh-01.example.test",
        "aliases": [],
        "vpn_required": False,
        "auth_model": "shared_service_account",
        "secret_ref": "targets/ssh-01",
        "extras": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(kwargs)
    async with get_sessionmaker()() as session:
        session.add(TargetORM(**values))
        await session.commit()


def test_create_and_patch_round_trip(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_token(key, TenantRole.TENANT_ADMIN)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        created = client.post(
            "/api/v1/targets",
            json={
                "name": "ssh-01",
                "product": "ssh",
                "host": "ssh-01.example.test",
                "secret_field_map": {"ssh_private_key": "private_key"},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["secret_field_map"] == {"ssh_private_key": "private_key"}

        patched = client.patch(
            "/api/v1/targets/ssh-01",
            json={"secret_field_map": {"username": "login"}},
            headers=headers,
        )
        assert patched.json()["secret_field_map"] == {
            "ssh_private_key": "private_key",
            "username": "login",
        }

        removed = client.patch(
            "/api/v1/targets/ssh-01",
            json={"secret_field_map": {"ssh_private_key": None}},
            headers=headers,
        )
        assert removed.json()["secret_field_map"] == {"username": "login"}

        listed = client.get("/api/v1/targets", headers=headers)
        assert listed.json()["items"][0]["secret_field_map"] == {"username": "login"}

        cleared = client.patch(
            "/api/v1/targets/ssh-01", json={"secret_field_map": None}, headers=headers
        )
        assert cleared.json()["secret_field_map"] == {}


def test_create_without_map_reads_back_empty(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        created = client.post(
            "/api/v1/targets",
            json={"name": "plain", "product": "ssh", "host": "plain.example.test"},
            headers={"Authorization": f"Bearer {_token(key, TenantRole.TENANT_ADMIN)}"},
        )
    assert created.status_code == 201
    assert created.json()["secret_field_map"] == {}


class _ProbeConnector(Connector):
    """Fingerprints without touching the secret, so only the map check reads it."""

    product = "linux-ssh-test"

    async def probe(self, target: Any) -> ProbeResult:  # pragma: no cover
        raise NotImplementedError

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        return FingerprintResult(
            vendor="test",
            product="linux-ssh-test",
            version="1",
            build=None,
            reachable=True,
            probed_at=datetime(2026, 10, 28, tzinfo=UTC),
            probe_method="test",
            extras={},
        )

    async def execute(  # pragma: no cover  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


def _probe(client: TestClient) -> Any:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.post(
            "/api/v1/targets/ssh-01/probe",
            headers={"Authorization": f"Bearer {_token(key, TenantRole.OPERATOR)}"},
        )


async def test_probe_rejects_a_missing_mapped_source_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_connector("linux-ssh-test", _ProbeConnector)
    install_fake_client(monkeypatch, secret={"private_key": "k", "password": _CANARY_PASSWORD})
    await _insert_target(secret_field_map={"ssh_private_key": "private_key", "username": "login"})

    response = _probe(client)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "secret_field_map_source_missing"
    assert detail["missing_source_fields"] == ["login"]
    assert _CANARY_PASSWORD not in response.text


async def test_probe_passes_when_every_mapped_source_exists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_connector("linux-ssh-test", _ProbeConnector)
    install_fake_client(monkeypatch, secret={"private_key": "k", "login": "ops"})
    await _insert_target(secret_field_map={"ssh_private_key": "private_key", "username": "login"})

    response = _probe(client)

    assert response.status_code == 200, response.text


async def test_probe_without_a_map_does_not_read_the_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_connector("linux-ssh-test", _ProbeConnector)
    fake = install_fake_client(monkeypatch, secret={})
    await _insert_target()

    response = _probe(client)

    assert response.status_code == 200, response.text
    assert fake.secrets.kv.v2.read_calls == []
