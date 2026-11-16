# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks a ``vault-target:`` ref passes before any Vault login.

* The named target must have its own ``vault_role``.
* The caller's MEHO policy for the matching ``vault.kv.*`` op on the named
  target counts: ``vault.kv.read`` for a source, ``vault.kv.put`` for a sink,
  ``vault.kv.patch`` for a sink with ``merge``. A caller who may not use the
  target is refused before the move waits for approval. When a reviewer
  runs an approved move, the requester's policy is checked again.
* The tenant path rule holds for a real tenant id, for source and sink.
* A merge into a secret whose latest version is deleted writes nothing.
* ``secret.handoff.export`` refuses a ``vault-target:`` source.

The Vault client is faked at the single ``_build_client`` seam; no real HTTP.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.handoff import (
    SecretHandoffRefusedError,
    secret_handoff_export,
)
from meho_backplane.connectors.secret.handoff_ops import register_secret_handoff_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest, PermissionVerdict
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)

from ._vault_fakes import install_fake_client
from .test_secret_broker_vault_target import (  # noqa: F401  (fixtures)
    _OPERATOR,
    _SECRET_VALUE,
    _TENANT_ID,
    _env,
    _move,
    _roles,
    _seed_target,
    _settings_env,
    stub_embedding_service,
)

# The shared fixtures (settings, registered ops, seeded targets) of the
# vault-target suite run for every test here.
pytestmark = pytest.mark.usefixtures("_env")

_SRC = "vault-target:vault-src/secret/a#password"
_DST = "vault-target:vault-dst/secret/b#password"


def _agent(sub: str = "agent-mover") -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


def _reviewer() -> Operator:
    return Operator(
        sub="op-reviewer",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _deny(sub: str, op_id: str, target_name: str) -> None:
    async with get_sessionmaker()() as session:
        target_id = (
            await session.execute(select(TargetORM.id).where(TargetORM.name == target_name))
        ).scalar_one()
        session.add(
            AgentPermission(
                tenant_id=_TENANT_ID,
                principal_sub=sub,
                op_pattern=op_id,
                target_scope=str(target_id),
                verdict=PermissionVerdict.DENY,
                created_by_sub="op-admin",
            )
        )
        await session.commit()


async def _ask(operator: Operator, params: dict[str, Any]) -> OperationResult:
    """Dispatch ``secret.move`` without approval, so it would wait for a person."""
    return await dispatch(
        operator=operator,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
    )


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


def _message(result: OperationResult) -> str:
    return str((result.extras or {}).get("exception_message"))


# -- a target without its own role ---------------------------------------


@pytest.mark.parametrize("side", ["from", "to"])
async def test_a_target_without_a_role_is_refused_before_any_login(
    side: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_target("vault-norole")
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    params = {"from": "vault:secret/a#password", "to": "vault:secret/b#password"}
    params[side] = "vault-target:vault-norole/secret/x#password"

    result = await _move(params)

    assert result.status == "error"
    assert result.error == "connector_error: VaultTargetRefusedError"
    assert "has no vault_role" in _message(result)
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.put_calls == []


async def test_a_target_without_a_role_does_not_wait_for_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_target("vault-norole")
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _ask(
        _OPERATOR, {"from": "vault:secret/a#password", "to": "vault-target:vault-norole/s/b#pw"}
    )

    assert result.status == "denied", result
    assert result.extras["error_code"] == "write_capability_denied"
    assert "has no vault_role" in str(result.extras["reason"])
    assert await _approval_rows() == []
    assert fake.auth.jwt.login_calls == []


# -- the caller's rights on the named target -----------------------------


async def test_a_caller_with_rights_waits_for_approval_with_a_requester_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _ask(_OPERATOR, {"from": _SRC, "to": _DST})

    assert result.status == "awaiting_approval", result
    (row,) = await _approval_rows()
    preflight = dict(row.proposed_effect)["permission_preflight"]
    assert preflight["requester"]["sub"] == _OPERATOR.sub
    assert preflight["requester_denied"] is False
    assert fake.auth.jwt.login_calls == []


@pytest.mark.parametrize(
    ("side", "op_id", "target", "merge"),
    [
        ("source", "vault.kv.read", "vault-src", False),
        ("sink", "vault.kv.put", "vault-dst", False),
        ("sink with merge", "vault.kv.patch", "vault-dst", True),
    ],
)
async def test_a_caller_without_rights_is_refused_before_approval(
    side: str, op_id: str, target: str, merge: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent()
    await _deny(agent.sub, op_id, target)
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await _ask(agent, {"from": _SRC, "to": _DST, "merge": merge})

    assert result.status == "denied", (side, result)
    assert result.extras["error_code"] == "write_capability_denied"
    assert op_id in str(result.extras["reason"])
    assert target in str(result.extras["reason"])
    assert await _approval_rows() == []
    assert fake.auth.jwt.login_calls == []


@pytest.mark.parametrize(
    ("op_id", "target"), [("vault.kv.read", "vault-src"), ("vault.kv.put", "vault-dst")]
)
async def test_a_caller_without_rights_is_refused_at_run_time_before_any_login(
    op_id: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent("agent-run")
    await _deny(agent.sub, op_id, target)
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    result = await dispatch(
        operator=agent,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params={"from": _SRC, "to": _DST},
        _approved=True,
    )

    assert result.status == "error", result
    assert _message(result).startswith(f"policy_denied: {op_id} on Vault target {target!r}")
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.put_calls == []


async def _approve_and_run(request_id: UUID) -> OperationResult:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=_reviewer())
        await session.commit()
    result: OperationResult = await resume_dispatch_after_approval(
        operator=_reviewer(), request=request, params=None
    )
    return result


@pytest.mark.parametrize("deny_while_waiting", [False, True])
async def test_an_agents_approved_move_checks_the_agent_again(
    deny_while_waiting: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer runs the approved move; the agent's policy on the target still counts."""
    agent = _agent("agent-asks")
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    parked = await _ask(agent, {"from": _SRC, "to": _DST})
    assert parked.status == "awaiting_approval", parked
    (row,) = await _approval_rows()
    assert dict(row.proposed_effect)["permission_preflight"]["requester"]["sub"] == agent.sub
    if deny_while_waiting:
        await _deny(agent.sub, "vault.kv.put", "vault-dst")

    result = await _approve_and_run(row.id)

    if deny_while_waiting:
        assert result.status == "error", result
        assert _message(result).startswith(f"policy_denied: requester {agent.sub!r}")
        assert fake.secrets.kv.v2.put_calls == []
        assert fake.auth.jwt.login_calls == []
    else:
        assert result.status == "ok", result
        assert _roles(fake) == ["role-src", "role-dst"]


# -- the tenant path rule with a real tenant id --------------------------

_REAL_TENANT = UUID("6a1f0c3e-2b7d-4e59-8c14-93d0b5a7e2f1")


@pytest.mark.parametrize("side", ["from", "to"])
async def test_a_ref_outside_the_tenant_path_is_refused_before_any_login(
    side: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_REAL_TENANT,
                name="vault-tenant",
                aliases=[],
                product="vault",
                host="vault.test",
                port=8200,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"vault_role": "role-tenant"},
                fingerprint={},
                notes="seeded by test_secret_broker_vault_target_policy",
            )
        )
        await session.commit()
    operator = _OPERATOR.model_copy(update={"tenant_id": _REAL_TENANT})
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    inside = f"vault-target:vault-tenant/tenants/{_REAL_TENANT}/app#password"
    params = {"from": inside, "to": inside.replace("/app#", "/copy#")}
    params[side] = "vault-target:vault-tenant/tenants/other/app#password"

    result = await dispatch(
        operator=operator,
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params=params,
        _approved=True,
    )

    assert result.status == "error", result
    assert result.error == "connector_error: VaultTenantScopeError"
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.put_calls == []


# -- merge into a deleted version ----------------------------------------


@pytest.mark.parametrize("how", ["deleted", "destroyed"])
async def test_merge_into_a_deleted_latest_version_writes_nothing(
    how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})
    kv = fake.secrets.kv.v2
    real_read = kv.read_secret_version
    answer = {"data": {"data": None, "metadata": {"version": 3, "deletion_time": ""}}}
    if how == "deleted":
        answer["data"]["metadata"]["deletion_time"] = "2026-11-01T10:00:00Z"
    else:
        answer["data"]["metadata"]["destroyed"] = True

    def read(path: str, **kwargs: Any) -> dict[str, Any]:
        if path != "secret/b":
            return real_read(path, **kwargs)
        if how == "destroyed":
            raise hvac.exceptions.InvalidPath("not found", json=answer)
        return answer

    monkeypatch.setattr(kv, "read_secret_version", read)

    result = await _move({"from": "vault:secret/a#password", "to": _DST, "merge": True})

    assert result.status == "error", result
    assert result.error == "connector_error: SecretMergeDeletedVersionError"
    assert "is deleted" in _message(result)
    assert kv.put_calls == []


# -- secret.handoff.export -----------------------------------------------


async def test_handoff_export_refuses_a_vault_target_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_secret_handoff_operations(embedding_service=service)
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    for approved in (False, True):
        result = await dispatch(
            operator=_OPERATOR,
            connector_id="secret-broker-1.x",
            op_id="secret.handoff.export",
            target=None,
            params={"from": _SRC, "reason": "copy to the other instance"},
            _approved=approved,
        )
        assert result.status == "error", result
        assert result.extras["error_code"] == "invalid_params"
    assert await _approval_rows() == []
    assert fake.auth.jwt.login_calls == []


async def test_handoff_export_handler_refuses_a_vault_target_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"password": _SECRET_VALUE})

    with pytest.raises(SecretHandoffRefusedError, match="does not read from a 'vault-target'"):
        await secret_handoff_export(_OPERATOR, None, {"from": _SRC})

    assert fake.auth.jwt.login_calls == []
