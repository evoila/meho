# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row of ``vault.kv.patch`` names the login of the read inside the patch.

hvac's ``secrets.kv.v2.patch`` reads the current version first and then
writes. The Vault stand-in here runs hvac's own ``patch`` code against the
in-process fake, so the read really happens before the write.

* A patch of a secret that exists names the caller.
* A patch of a secret that does not exist names no login.
* A read that passes, then a write the store refuses, still names the caller.
* The made-up secret value is not in the row, the logs or the answer.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac
import pytest
import structlog
from hvac.api.secrets_engines.kv_v2 import KvV2
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.ops import register_vault_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_vault

_TENANT = UUID("00000000-0000-0000-0000-0000000000d7")
_MOUNT = "markermount0zzz"
_PATH = "markerenv0zzz/markersvc0zzz"
_STORED_VALUE = "made-up-stored-value-0zzz"
_NEW_VALUE = "made-up-new-value-0zzz"
_OP = "vault.kv.patch"


def _operator(sub: str) -> Operator:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=f"e30.{payload}.tokentail0zzz",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    def __init__(self) -> None:
        self.product = "vault"
        self.fingerprint = type("F", (), {"version": "1.18.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "vault-prod"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
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


def _install_reading_patch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install the Vault fake with hvac's own ``patch``: read first, then write."""
    fake = install_fake_vault(monkeypatch)
    kv_v2 = fake.secrets.kv.v2
    kv_v2.secret = {"password": _STORED_VALUE}

    def hvac_patch(path: str, secret: dict[str, Any], mount_point: str = "secret") -> Any:
        kv_v2.patch_calls.append({"path": path, "secret": secret, "mount_point": mount_point})
        return KvV2.patch(kv_v2, path=path, secret=secret, mount_point=mount_point)

    kv_v2.patch = hvac_patch
    return fake


async def _patch(embedding: AsyncMock) -> OperationResult:
    await register_vault_typed_operations(embedding_service=embedding)
    return await dispatch(
        operator=_operator("person-sub"),
        connector_id="vault-1.x",
        op_id=_OP,
        target=_Target(),
        params={"mount": _MOUNT, "path": _PATH, "data": {"api_key": _NEW_VALUE}},
        # The approval-resume path: the call runs as it does after approval.
        _approved=True,
    )


async def _row() -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.path == _OP))).scalars().all()
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
async def test_patch_of_an_existing_secret_names_the_caller(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = _install_reading_patch(monkeypatch)
    result = await _patch(embedding)
    assert result.status == "ok", result.error
    kv_v2 = fake.secrets.kv.v2
    assert len(kv_v2.read_calls) == 1
    (write,) = kv_v2.put_calls
    assert write["cas"] == kv_v2.version
    assert (await _row()).secret_read_as == "person-sub"


@pytest.mark.asyncio
async def test_patch_of_a_missing_secret_names_no_login(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = _install_reading_patch(monkeypatch)
    fake.secrets.kv.v2.read_exc = hvac.exceptions.InvalidPath("missing")
    result = await _patch(embedding)
    assert result.status != "ok"
    assert fake.secrets.kv.v2.put_calls == []
    assert (await _row()).secret_read_as is None


@pytest.mark.asyncio
async def test_refused_write_after_a_passed_read_names_the_caller(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = _install_reading_patch(monkeypatch)
    fake.secrets.kv.v2.put_exc = hvac.exceptions.Forbidden("permission denied")
    result = await _patch(embedding)
    assert result.status != "ok"
    assert len(fake.secrets.kv.v2.read_calls) == 1
    assert len(fake.secrets.kv.v2.put_calls) == 1
    assert (await _row()).secret_read_as == "person-sub"


@pytest.mark.asyncio
async def test_secret_values_stay_out_of_the_row_the_logs_and_the_answer(
    monkeypatch: pytest.MonkeyPatch,
    embedding: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_reading_patch(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        result = await _patch(embedding)
    assert result.status == "ok", result.error
    row = await _row()
    assert row.secret_read_as == "person-sub"
    texts = [
        repr(result.model_dump()),
        repr(logs),
        caplog.text,
        row.secret_read_as or "",
    ]
    for text in texts:
        assert _STORED_VALUE not in text
    assert _NEW_VALUE not in repr(result.model_dump())
    for marker in (_MOUNT, "markerenv0zzz", "tokentail0zzz"):
        assert marker not in (row.secret_read_as or "")
