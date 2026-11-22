# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which secret reads a call's audit row names, at the edges of a call.

* The read of the existing sink secret before a ``secret.move`` with
  ``merge: true`` writes it notes the caller's login, also when the write
  after it is refused. The second read that tells why the write failed
  notes it too. A sink secret that does not exist notes nothing.
* Each call has its own scope from its first check on: a child call that
  the policy refuses, and a step that the step gate of a multi-step call
  refuses, write a refusal row with no value of the parent call.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.secret_read_as import current_secret_read_as, track_secret_read_as
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.secret.endpoints import SecretMaterial
from meho_backplane.connectors.secret.vault_endpoint import (
    SecretMergeChangedError,
    SecretMergeRefusedError,
    VaultKvSecretEndpoint,
)
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PERMISSION_PREFLIGHTS,
    PreviewContext,
    register_permission_preflight,
)
from meho_backplane.operations.composite import composite_depth_var, enforce_subop_policy
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_vault

_TENANT = UUID("00000000-0000-0000-0000-0000000000d5")
_LOGIN_PATH = "markerenv0zzz/markersvc0zzz"
_CHILD_OP = "vault.demo.refused_child"
_STEP_OP = "vault.demo.refused_step"


def _operator(sub: str) -> Operator:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=f"e30.{payload}.sig",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
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
        self.secret_ref = _LOGIN_PATH


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    for op_id in (_CHILD_OP, _STEP_OP):
        _PERMISSION_PREFLIGHTS.pop(op_id, None)
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


async def _row(op_id: str) -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# The merge read of ``secret.move``
# ---------------------------------------------------------------------------


async def _merge_in_scope(fake: Any) -> str | None:
    endpoint = VaultKvSecretEndpoint(f"{_LOGIN_PATH}#password")
    with track_secret_read_as():
        await endpoint.write_secret_merged(_operator("person-sub"), _material("made-up-value-0zzz"))
        assert len(fake.secrets.kv.v2.read_calls) == 1
        return current_secret_read_as()


def _material(value: str) -> SecretMaterial:
    return SecretMaterial(value.encode())


@pytest.mark.asyncio
async def test_merge_read_of_the_sink_secret_names_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"username": "svc"}
    assert await _merge_in_scope(fake) == "person-sub"
    assert len(fake.secrets.kv.v2.put_calls) == 1


@pytest.mark.asyncio
async def test_merge_read_names_the_caller_when_the_write_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"username": "svc"}
    fake.secrets.kv.v2.put_exc = hvac.exceptions.Forbidden("permission denied")
    endpoint = VaultKvSecretEndpoint(f"{_LOGIN_PATH}#password")
    with track_secret_read_as():
        with pytest.raises(SecretMergeRefusedError):
            await endpoint.write_secret_merged(
                _operator("person-sub"), _material("made-up-value-0zzz")
            )
        assert current_secret_read_as() == "person-sub"


@pytest.mark.asyncio
async def test_merge_into_a_missing_sink_secret_leaves_it_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.read_exc = hvac.exceptions.InvalidPath("no secret")
    assert await _merge_in_scope(fake) is None
    assert fake.secrets.kv.v2.put_calls[0]["cas"] == 0


@pytest.mark.asyncio
async def test_read_after_a_failed_merge_write_names_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first read finds no secret, another writer creates it, the write fails."""
    fake = install_fake_vault(monkeypatch)
    kv = fake.secrets.kv.v2
    kv.read_exc = hvac.exceptions.InvalidPath("no secret")

    def _other_writer_wins(**kwargs: Any) -> None:
        kv.put_calls.append(kwargs)
        kv.read_exc = None
        raise hvac.exceptions.InvalidRequest("check-and-set parameter did not match")

    monkeypatch.setattr(kv, "create_or_update_secret", _other_writer_wins)
    endpoint = VaultKvSecretEndpoint(f"{_LOGIN_PATH}#password")
    with track_secret_read_as():
        with pytest.raises(SecretMergeChangedError):
            await endpoint.write_secret_merged(
                _operator("person-sub"), _material("made-up-value-0zzz")
            )
        assert len(kv.read_calls) == 2
        assert kv.put_calls[0]["cas"] == 0
        assert current_secret_read_as() == "person-sub"


# ---------------------------------------------------------------------------
# Each call has its own scope from its first check on
# ---------------------------------------------------------------------------


async def _refusing_preflight(ctx: PreviewContext) -> dict[str, Any]:
    """A permission check that says the write will be denied."""
    del ctx
    return {
        "check": "vault.capabilities-self",
        "path": "secret/data/x",
        "required": ["create", "update"],
        "granted": ["read"],
        "will_be_denied": True,
    }


async def _parent_reads_then_calls_child(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    await load_basic_credentials(target, operator)
    depth_token = composite_depth_var.set(1)
    try:
        child = await dispatch(
            operator=operator,
            connector_id="vault-1.x",
            op_id=_CHILD_OP,
            target=target,
            params={},
        )
    finally:
        composite_depth_var.reset(depth_token)
    return {"child_status": child.status, "child_error_code": child.extras.get("error_code")}


async def _parent_reads_then_gates_step(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    await load_basic_credentials(target, operator)
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id="vault-1.x",
        op_id=_STEP_OP,
        safety_level="caution",
        requires_approval=True,
        target=target,
        params={},
    )
    return {"step_refused": gate is not None and gate.status != "ok"}


async def _register(
    op_id: str, handler: Any, embedding: AsyncMock, *, requires_approval: bool = False
) -> None:
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=handler,
        summary="Demo op for the secret-read-as scope tests.",
        description="Demo op used by the secret-read-as scope tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="caution" if requires_approval else "safe",
        requires_approval=requires_approval,
    )


async def _child_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("the refused child must not run")


@pytest.mark.asyncio
async def test_child_refused_by_the_policy_holds_no_value_of_the_parent(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"username": "svc", "password": "made-up-value-0zzz"}
    await _register("vault.demo.parent_child", _parent_reads_then_calls_child, embedding)
    await _register(_CHILD_OP, _child_handler, embedding, requires_approval=True)
    register_permission_preflight(_CHILD_OP, _refusing_preflight)
    result = await dispatch(
        operator=_operator("person-sub"),
        connector_id="vault-1.x",
        op_id="vault.demo.parent_child",
        target=_Target(),
        params={},
    )
    assert result.status == "ok", result.error
    assert result.result["child_status"] != "ok"
    assert (await _row("vault.demo.parent_child")).secret_read_as == "person-sub"
    child_row = await _row(_CHILD_OP)
    assert child_row.payload.get("result_status") != "ok"
    assert child_row.secret_read_as is None


@pytest.mark.asyncio
async def test_step_refused_by_the_step_gate_holds_no_value_of_the_parent(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"username": "svc", "password": "made-up-value-0zzz"}
    await _register("vault.demo.parent_step", _parent_reads_then_gates_step, embedding)
    register_permission_preflight(_STEP_OP, _refusing_preflight)
    result = await dispatch(
        operator=_operator("person-sub"),
        connector_id="vault-1.x",
        op_id="vault.demo.parent_step",
        target=_Target(),
        params={},
    )
    assert result.status == "ok", result.error
    assert result.result["step_refused"] is True
    assert (await _row("vault.demo.parent_step")).secret_read_as == "person-sub"
    assert (await _row(_STEP_OP)).secret_read_as is None
