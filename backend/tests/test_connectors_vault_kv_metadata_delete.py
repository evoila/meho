# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.kv.metadata.delete`` — the governed permanent removal of a KV v2 secret (#3424).

The op sends ``DELETE /v1/<mount>/metadata/<path>``, which removes every
version of the secret and its metadata. It cannot be undone, so it sits in
the ``destructive`` tier (``requires_approval=True``). These tests prove:

* **The handler.** It reads the metadata before and after the delete. A
  missing path returns ``status="unchanged"`` and sends nothing (safe to run
  twice). ``status="deleted"`` comes only after the read after the delete
  finds nothing; a secret that is still there returns ``still_present``.
  The result names the mount and path only, never a secret value.
* **The preview.** The approver sees the mount and path plus a
  ``blast_radius`` block (what dies, and that it is permanent).
* **The permission check before approval.** It probes
  ``<mount>/metadata/<path>`` and needs ``read`` and ``delete`` there.
* **The full governed flow.** preview → the call waits for approval →
  a second person approves → the delete runs once → a ``vault.kv.versions``
  read of the same path returns not-found (``InvalidPath``).
* **The tier holds.** No call without a matching preview hash, and no agent
  path.

The Vault HTTP layer is the shared in-process fake (``tests/_vault_fakes.py``).
All values are obvious fakes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import hvac.exceptions
import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault import kv_write_preview as kv_write_preview_module
from meho_backplane.connectors.vault.ops import (
    _KV_OP_SPECS,
    VAULT_KV_METADATA_DELETE_PARAMETER_SCHEMA,
    VAULT_KV_WRITE_CAPABILITIES,
    register_vault_typed_operations,
    vault_kv_write_capability_preflight,
    vault_kv_write_target_path,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
)
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

from ._vault_fakes import _FakeClient, install_fake_client, install_fake_vault

_OP_ID = "vault.kv.metadata.delete"
_CONNECTOR_ID = "vault-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003424")
#: In the default tenant-scope layout (``secret/tenants/{tenant_id}/``).
_PATH = f"tenants/{_TENANT_ID}/app/old-token"
#: An obvious fake value. It must never show up in a result, preview or audit row.
_FAKE_SECRET_VALUE = "fake-value-do-not-echo"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def _registered_ops() -> None:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_vault_typed_operations(embedding_service=service)


def _operator(
    sub: str = "op-requester", *, principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


async def _run(params: dict[str, Any], *, operator: Operator | None = None) -> OperationResult:
    """Run the op as the approval-resume path does (the approval already happened)."""
    return await dispatch(
        operator=operator or _operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
        _approved=True,
    )


def _fake_with_secret(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    return install_fake_client(monkeypatch, secret={"password": _FAKE_SECRET_VALUE})


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


# ===========================================================================
# Handler — read before, delete, read after
# ===========================================================================


async def test_deletes_secret_and_reports_deleted_after_read_finds_nothing(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)
    result = await _run({"path": _PATH})

    assert result.status == "ok", result.error
    assert result.result == {
        "status": "deleted",
        "object": {"kind": "kv_secret", "mount": "secret", "path": _PATH},
        "guidance": None,
    }
    kv = fake.secrets.kv.v2
    assert kv.metadata_delete_calls == [{"path": _PATH, "mount_point": "secret"}]
    # One metadata read before the delete and one after it.
    assert kv.versions_calls == [
        {"path": _PATH, "mount_point": "secret"},
        {"path": _PATH, "mount_point": "secret"},
    ]
    # The secret value is never read.
    assert kv.read_calls == []


async def test_already_missing_path_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)
    fake.secrets.kv.v2.absent_paths.add(f"secret/{_PATH}")

    result = await _run({"path": _PATH})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["status"] == "unchanged"
    assert result.result["object"] == {"kind": "kv_secret", "mount": "secret", "path": _PATH}
    assert "Nothing was deleted" in result.result["guidance"]
    # Nothing was sent: only the metadata read ran.
    assert fake.secrets.kv.v2.metadata_delete_calls == []
    assert len(fake.secrets.kv.v2.versions_calls) == 1


async def test_safe_to_run_twice(monkeypatch: pytest.MonkeyPatch, _registered_ops: None) -> None:
    fake = _fake_with_secret(monkeypatch)

    first = await _run({"path": _PATH})
    second = await _run({"path": _PATH})

    assert first.result is not None and first.result["status"] == "deleted"
    assert second.result is not None and second.result["status"] == "unchanged"
    assert len(fake.secrets.kv.v2.metadata_delete_calls) == 1


async def test_secret_still_there_after_delete_is_not_reported_deleted(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)
    fake.secrets.kv.v2.metadata_delete_keeps_path = True

    result = await _run({"path": _PATH})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["status"] == "still_present"
    assert "vault.kv.versions" in result.result["guidance"]
    assert len(fake.secrets.kv.v2.metadata_delete_calls) == 1


async def test_explicit_mount_is_forwarded(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "kv/tenants/{tenant_id}/")
    get_settings.cache_clear()

    result = await _run({"mount": "kv", "path": _PATH})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert result.result["object"] == {"kind": "kv_secret", "mount": "kv", "path": _PATH}
    assert fake.secrets.kv.v2.metadata_delete_calls == [{"path": _PATH, "mount_point": "kv"}]


async def test_result_never_carries_secret_values_or_custom_metadata(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)
    original = fake.secrets.kv.v2.read_secret_metadata

    def _metadata_with_custom_fields(path: str, mount_point: str = "secret") -> dict[str, Any]:
        payload = original(path, mount_point=mount_point)
        payload["data"]["custom_metadata"] = {"note": _FAKE_SECRET_VALUE}
        return payload

    monkeypatch.setattr(fake.secrets.kv.v2, "read_secret_metadata", _metadata_with_custom_fields)

    result = await _run({"path": _PATH})

    assert result.status == "ok", result.error
    assert _FAKE_SECRET_VALUE not in json.dumps(result.result)
    assert set(result.result or {}) == {"status", "object", "guidance"}


async def test_mount_prefixed_path_is_rejected_before_any_vault_call(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)

    result = await _run({"path": f"secret/{_PATH}"})

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultPathShapeError"
    assert "mount-relative" in result.extras.get("exception_message", "")
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.metadata_delete_calls == []


async def test_out_of_tenant_path_is_denied_before_any_vault_call(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)
    other_tenant = UUID("00000000-0000-0000-0000-00000000beef")

    result = await _run({"path": f"tenants/{other_tenant}/app/old-token"})

    assert result.status == "error"
    assert result.extras.get("exception_class") == "VaultTenantScopeError"
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.metadata_delete_calls == []


async def test_vault_forbidden_names_read_and_delete_on_the_metadata_path(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)
    fake.secrets.kv.v2.metadata_delete_exc = hvac.exceptions.Forbidden("permission denied")

    result = await _run({"path": _PATH})

    assert result.status == "error"
    assert result.extras["error_code"] == "vault_write_identity_forbidden"
    assert result.extras["path"] == f"secret/metadata/{_PATH}"
    assert result.error is not None
    assert "lacks 'read' or 'delete' on that metadata path" in result.error


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"path": ""},
        {"path": "   "},
        {"path": _PATH, "versions": [1]},
        {"mount": "secret/data", "path": _PATH},
    ],
    ids=["missing-path", "empty-path", "blank-path", "extra-key", "slash-in-mount"],
)
async def test_invalid_params_are_rejected(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, Any], _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)

    result = await _run(params)

    assert result.status == "error"
    assert result.extras.get("error_code") == "invalid_params"
    assert fake.secrets.kv.v2.metadata_delete_calls == []


def test_path_description_says_relative_to_mount_with_example() -> None:
    description = VAULT_KV_METADATA_DELETE_PARAMETER_SCHEMA["properties"]["path"]["description"]
    assert "relative to the mount" in description
    assert "'meho/test/old-token'" in description


# ===========================================================================
# Registration — destructive tier, write class
# ===========================================================================


def test_spec_is_destructive_and_requires_approval() -> None:
    spec = {s["op_id"]: s for s in _KV_OP_SPECS}[_OP_ID]
    assert spec["safety_level"] == "destructive"
    assert spec["requires_approval"] is True
    assert "destructive" in spec["tags"]


async def test_registered_row_is_destructive(_registered_ops: None) -> None:
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID))
        ).scalar_one()
    assert row.safety_level == "destructive"
    assert row.requires_approval is True
    assert row.source_kind == "typed"
    assert "destructive" in (row.tags or [])


def test_classifies_as_plain_write() -> None:
    """No secret rides in the params, so the op is a plain ``write`` (full-detail broadcast)."""
    assert classify_op(_OP_ID) == "write"


# ===========================================================================
# Preview — mount + path and the blast radius, never a value
# ===========================================================================


def _preview_ctx(params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=_operator(),
        target=None,
        params=params,
        connector_id=_CONNECTOR_ID,
    )


async def test_preview_names_mount_path_and_blast_radius() -> None:
    preview = await kv_write_preview_module._kv_metadata_delete_preview(
        _preview_ctx({"path": _PATH})
    )

    assert preview == {
        "resource": "kv_secret",
        "mount": "secret",
        "path": _PATH,
        "kv_version": 2,
        "semantics": "permanent_delete",
        "blast_radius": {
            "object": {"kind": "kv_secret", "mount": "secret", "path": _PATH},
            "children": [
                {"kind": "kv_versions", "which": "all"},
                {"kind": "kv_metadata"},
            ],
            "irreversibility": "permanent",
        },
    }
    # Well-formed for the destructive park gate.
    assert blast_radius_missing_reason(preview) is None


def test_preview_builder_is_registered() -> None:
    assert _PREVIEW_BUILDERS[_OP_ID] is kv_write_preview_module._kv_metadata_delete_preview


# ===========================================================================
# Permission check before approval — read + delete on the metadata path
# ===========================================================================


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"path": "meho/test/x"}, "secret/metadata/meho/test/x"),
        ({"mount": "kv", "path": "meho/y"}, "kv/metadata/meho/y"),
    ],
)
def test_write_target_path_is_the_metadata_path(params: dict[str, Any], expected: str) -> None:
    assert vault_kv_write_target_path(_OP_ID, params) == expected


def test_capabilities_need_read_and_delete() -> None:
    assert VAULT_KV_WRITE_CAPABILITIES[_OP_ID] == frozenset({"read", "delete"})


@pytest.mark.parametrize(
    ("granted", "will_be_denied"),
    [
        (["read", "delete"], False),
        (["delete"], True),
        (["read"], True),
        (["create", "read", "update"], True),
    ],
)
async def test_preflight_probes_metadata_path(
    monkeypatch: pytest.MonkeyPatch, granted: list[str], will_be_denied: bool
) -> None:
    fake = install_fake_vault(monkeypatch)
    fake.sys.capabilities_by_path = {"secret/metadata/meho/test/x": granted}

    result = await vault_kv_write_capability_preflight(_operator(), _OP_ID, {"path": "meho/test/x"})

    assert result is not None
    assert result["path"] == "secret/metadata/meho/test/x"
    assert result["required"] == ["delete", "read"]
    assert result["will_be_denied"] is will_be_denied


# ===========================================================================
# The full governed flow
# ===========================================================================


async def test_full_governed_flow_then_versions_read_is_not_found(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    """preview → wait for approval → second person approves → delete → not-found."""
    fake = _fake_with_secret(monkeypatch)
    fake.sys.capabilities_by_path = {f"secret/metadata/{_PATH}": ["read", "delete"]}
    requester = _operator("op-requester")
    args = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "params": {"path": _PATH}}

    # 1) The preview binds a hash and shows the blast radius.
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    bound_hash = preview["preview_hash"]
    blast = preview["proposed_effect"]["preview"]["blast_radius"]
    assert blast["irreversibility"] == "permanent"

    # 2) The call with the bound hash waits for approval; nothing is deleted.
    call = await call_operation(requester, {**args, "preview_hash": bound_hash})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert fake.secrets.kv.v2.metadata_delete_calls == []

    # 3) The waiting row carries the hash, the blast radius and the
    #    permission check — and the params hold only the path.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        assert row.params == {"path": _PATH}
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"] == {
        "kind": "kv_secret",
        "mount": "secret",
        "path": _PATH,
    }
    assert effect["permission_preflight"]["path"] == f"secret/metadata/{_PATH}"
    assert effect["permission_preflight"]["will_be_denied"] is False
    assert _FAKE_SECRET_VALUE not in json.dumps(effect)

    # 4) A second person approves.
    approver = _operator("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    # 5) The resume runs the delete exactly once and reports it.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert fake.secrets.kv.v2.metadata_delete_calls == [{"path": _PATH, "mount_point": "secret"}]

    # 6) A vault.kv.versions read of the same path now returns not-found.
    versions = await dispatch(
        operator=approver,
        connector_id=_CONNECTOR_ID,
        op_id="vault.kv.versions",
        target=None,
        params={"path": _PATH},
    )
    assert versions.status == "error"
    assert versions.extras.get("exception_class") == "InvalidPath"

    # 7) The audit rows carry the path only: no secret value anywhere.
    async with get_sessionmaker()() as s:
        audit_rows = (await s.execute(select(AuditLog))).scalars().all()
    op_rows = [r for r in audit_rows if r.path == _OP_ID]
    assert len(op_rows) == 1
    assert op_rows[0].payload["op_id"] == _OP_ID
    assert "params_hash" in op_rows[0].payload
    assert _FAKE_SECRET_VALUE not in json.dumps([r.payload for r in audit_rows], default=str)


async def test_call_without_preview_hash_is_refused(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params={"path": _PATH},
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert fake.secrets.kv.v2.metadata_delete_calls == []
    assert await _pending_count() == 0


async def test_agent_principal_is_denied(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = _fake_with_secret(monkeypatch)

    result = await dispatch(
        operator=_operator("agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params={"path": _PATH},
    )

    assert result.status == "denied", result
    assert fake.secrets.kv.v2.metadata_delete_calls == []
    assert await _pending_count() == 0
