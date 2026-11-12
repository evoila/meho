# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault-target:`` refs and the ``merge`` sink option of ``secret.move``.

The ``vault-target:<target>/<path>#<field>`` kind logs in to Vault under a
registered Vault target's role. Source and sink name their own target. The
plain ``vault:`` kind keeps using the operator's own role. ``merge: true``
keeps the other fields of the sink secret and guards the write with
check-and-set. The Vault client is faked at the single ``_build_client``
seam (see ``_vault_fakes.install_fake_client``); no real HTTP.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac
import pytest
from sqlalchemy import select

import meho_backplane.connectors.secret  # noqa: F401 -- registers the secret kinds
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.endpoints import SECRET_ENDPOINT_REGISTRY
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.connectors.secret.vault_endpoint import VaultSecretRefError
from meho_backplane.connectors.secret.vault_target_endpoint import VaultTargetKvSecretEndpoint
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# Made-up value for the tests only.
_SECRET_VALUE = "hunter2"
_TENANT_ID = UUID(int=0)
_OPERATOR = Operator(
    sub="test-operator",
    name=None,
    email=None,
    raw_jwt="fake.jwt.value",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "operator-role")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _seed_target(name: str, *, product: str = "vault", role: str | None = None) -> None:
    extras: dict[str, Any] = {"vault_role": role} if role else {}
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=name,
                aliases=[],
                product=product,
                host="vault.test",
                port=8200,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras=extras,
                fingerprint={},
                notes="seeded by test_secret_broker_vault_target",
            )
        )
        await session.commit()


@pytest.fixture
async def _env(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    await register_secret_broker_operations(embedding_service=stub_embedding_service)
    await _seed_target("vault-src", role="role-src")
    await _seed_target("vault-dst", role="role-dst")
    await _seed_target("not-vault", product="keycloak")
    yield


async def _move(params: dict[str, Any], *, approved: bool = True) -> OperationResult:
    return await dispatch(
        operator=_OPERATOR,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
        _approved=approved,
    )


def _roles(fake: Any) -> list[str]:
    return [c["role"] for c in fake.auth.jwt.login_calls]


# -- ref grammar ---------------------------------------------------------


def test_kind_is_registered() -> None:
    assert SECRET_ENDPOINT_REGISTRY["vault-target"] is VaultTargetKvSecretEndpoint


@pytest.mark.parametrize("ref", ["/secret/db#pw", "t/#pw", "t/secret/db", "t/#"], ids=str)
def test_malformed_refs_are_rejected(ref: str) -> None:
    with pytest.raises(VaultSecretRefError):
        VaultTargetKvSecretEndpoint(ref)


# -- target resolution ---------------------------------------------------


async def test_source_and_sink_resolve_their_own_target(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _move(
        {
            "from": "vault-target:vault-src/secret/a#password",
            "to": "vault-target:vault-dst/secret/b#password",
        }
    )
    assert result.status == "ok", result.error
    assert _roles(fake) == ["role-src", "role-dst"]
    assert fake.secrets.kv.v2.read_calls[0]["path"] == "secret/a"
    assert fake.secrets.kv.v2.put_calls[0]["path"] == "secret/b"


async def test_target_sink_with_operator_source(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _move(
        {
            "from": "vault:secret/a#password",
            "to": "vault-target:vault-dst/secret/b#password",
        }
    )
    assert result.status == "ok", result.error
    assert _roles(fake) == ["operator-role", "role-dst"]


async def test_plain_vault_ref_still_uses_operator_role(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _move({"from": "vault:secret/a#password", "to": "vault:secret/b#password"})
    assert result.status == "ok", result.error
    assert _roles(fake) == ["operator-role", "operator-role"]
    assert fake.secrets.kv.v2.put_calls[0]["secret"] == {"password": _SECRET_VALUE}


async def test_unknown_target_is_an_error_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _move(
        {"from": "vault:secret/a#password", "to": "vault-target:nope/secret/b#password"}
    )
    assert result.status == "error"
    assert fake.secrets.kv.v2.put_calls == []


async def test_non_vault_target_is_refused(monkeypatch: pytest.MonkeyPatch, _env: None) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _move(
        {"from": "vault-target:not-vault/secret/a#password", "to": "vault:secret/b#password"}
    )
    assert result.status == "error"
    assert fake.auth.jwt.login_calls == []


# -- merge ---------------------------------------------------------------


async def test_merge_keeps_sibling_fields_with_cas(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(
        monkeypatch, secret={"username": "demo", "password": _SECRET_VALUE}, kv_version=4
    )
    result = await _move(
        {
            "from": "vault:secret/a#password",
            "to": "vault-target:vault-dst/secret/b#password",
            "merge": True,
        }
    )
    assert result.status == "ok", result.error
    put = fake.secrets.kv.v2.put_calls[0]
    assert put["secret"] == {"username": "demo", "password": _SECRET_VALUE}
    assert put["cas"] == 4
    assert _roles(fake) == ["operator-role", "role-dst"]


async def test_merge_creates_a_missing_secret_with_cas_zero(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    kv = fake.secrets.kv.v2
    real_read = kv.read_secret_version

    def read(path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "secret/new":
            raise hvac.exceptions.InvalidPath("missing")
        return real_read(path, **kwargs)

    monkeypatch.setattr(kv, "read_secret_version", read)
    result = await _move(
        {"from": "vault:secret/a#password", "to": "vault:secret/new#password", "merge": True}
    )
    assert result.status == "ok", result.error
    assert kv.put_calls[0]["secret"] == {"password": _SECRET_VALUE}
    assert kv.put_calls[0]["cas"] == 0


async def test_merge_cas_conflict_is_an_error_without_the_value(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(
        monkeypatch,
        secret={"password": _SECRET_VALUE},
        put_exc=hvac.exceptions.InvalidRequest("check-and-set parameter did not match"),
    )
    result = await _move(
        {"from": "vault:secret/a#password", "to": "vault:secret/b#password", "merge": True}
    )
    assert result.status == "error"
    assert result.error == "connector_error: VaultSecretRefError"
    assert _SECRET_VALUE not in json.dumps(result.model_dump(mode="json"), default=str)
    assert fake.secrets.kv.v2.put_calls  # the guarded write was tried once


async def test_merge_default_off_replaces_the_secret(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(monkeypatch, secret={"username": "demo", "password": _SECRET_VALUE})
    result = await _move({"from": "vault:secret/a#password", "to": "vault:secret/b#password"})
    assert result.status == "ok", result.error
    put = fake.secrets.kv.v2.put_calls[0]
    assert put["secret"] == {"password": _SECRET_VALUE}
    assert put["cas"] is None


async def test_merge_with_a_sink_that_cannot_merge_fails_before_reading(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _move(
        {
            "from": "vault:secret/a#password",
            "to": "keycloak:some-target/realm/user#password",
            "merge": True,
        }
    )
    assert result.status == "error"
    assert result.error == "connector_error: SecretMergeUnsupportedError"
    assert fake.secrets.kv.v2.read_calls == []


# -- preview and audit ---------------------------------------------------


async def test_preview_names_both_targets_and_merge_flag_without_value(
    monkeypatch: pytest.MonkeyPatch, _env: None
) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    from meho_backplane.connectors.secret.move_preview import build_secret_move_preview
    from meho_backplane.operations._preview import PreviewContext

    ctx = PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # builder ignores it
        connector_instance=None,
        operator=_OPERATOR,
        target=None,
        params={
            "from": "vault-target:vault-src/secret/a#password",
            "to": "vault-target:vault-dst/secret/b#password",
            "merge": True,
        },
    )
    preview = await build_secret_move_preview(ctx)
    assert preview is not None
    assert preview["source"]["target"] == "vault-src"
    assert preview["sink"]["target"] == "vault-dst"
    assert preview["merge"] is True
    assert _SECRET_VALUE not in json.dumps(preview)


async def test_audit_rows_carry_no_value(monkeypatch: pytest.MonkeyPatch, _env: None) -> None:
    install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    result = await _move(
        {
            "from": "vault-target:vault-src/secret/a#password",
            "to": "vault-target:vault-dst/secret/b#password",
            "merge": True,
        }
    )
    assert result.status == "ok", result.error
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert rows
    for row in rows:
        assert _SECRET_VALUE not in json.dumps(
            [row.payload, row.raw_payload, row.path], default=str
        )
