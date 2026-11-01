# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved run goes only to the target and preview the person approved.

This suite pins the checks every approved run makes before it starts:

* The request stores a hash of the target's connection settings. The run
  is refused when a connection field of the target changed (for example
  its host), and runs when only the name changed.
* The agent's own resume of a direct request runs against the target the
  request saved by id, never the name the agent used when it parked.
* A request with a preview hash builds the preview again. The run is
  refused when the hash differs, and runs when it matches.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet

import meho_backplane.operations._request_preview as preview_module
import meho_backplane.operations.dispatcher as dispatcher_module
import meho_backplane.operations.meta_tools as meta_tools_module
from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approval_resume_checks import (
    TARGET_HASH_FIELDS,
    compute_target_hash,
)
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000a7a05")
_OP_ID = "resumecheck.op"
_CONNECTOR_ID = "resumecheck-1.x"
_PARAMS: dict[str, Any] = {"vm": "vm-7"}
_PREVIEW: dict[str, Any] = {
    "status": "ok",
    "connector_id": _CONNECTOR_ID,
    "op_id": _OP_ID,
    "method": "DELETE",
    "resolved_path": "/api/vm/vm-7",
    "query": {},
    "redacted_body": None,
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(sub: str = "agent-resume-check") -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


async def _persist_target(name: str) -> uuid.UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=name,
                product="resumecheck",
                host="first.invalid",
                port=443,
                secret_ref="kv/resumecheck/first",
                aliases=[],
                extras={"vault_role": "first"},
            )
        )
        await s.commit()
    return target_id


async def _update_target(target_id: uuid.UUID, **fields: Any) -> None:
    async with get_sessionmaker()() as s:
        row = await s.get(TargetORM, target_id)
        assert row is not None
        for key, value in fields.items():
            setattr(row, key, value)
        await s.commit()


async def _park_approved(
    target_id: uuid.UUID | None, *, preview_hash: str | None = None, fqdn: str | None = None
) -> ApprovalRequest:
    """Park one request against *target_id* the way the dispatcher does, then approve it.

    The queue gets an in-memory target that may carry a per-call *fqdn*.
    """
    target = SimpleNamespace(id=target_id, fqdn=fqdn) if target_id is not None else None
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params=dict(_PARAMS),
            params_hash=compute_params_hash(_PARAMS),
            run_id=uuid.uuid4(),
            preview_hash=preview_hash,
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.decided_at = datetime.now(UTC)
        request.reviewed_by = "op-approver"
        await s.commit()
    return request


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return row


def _spy_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the dispatcher; every call stands for a call to the vendor."""
    calls: list[dict[str, Any]] = []

    async def _fake_dispatch(**kwargs: Any) -> OperationResult:
        calls.append(kwargs)
        return OperationResult(status="ok", op_id=kwargs["op_id"], data={}, duration_ms=1.0)

    monkeypatch.setattr(dispatcher_module, "dispatch", _fake_dispatch)
    return calls


def _spy_agent_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the agent's approved re-dispatch; record arguments and target."""
    seen: list[dict[str, Any]] = []

    async def _spy(
        _operator: Operator, arguments: dict[str, Any], *, resolved_target: Any = None
    ) -> dict[str, Any]:
        seen.append({"arguments": arguments, "target_id": getattr(resolved_target, "id", None)})
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    return seen


def _fixed_preview(monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any]) -> None:
    async def _preview(**_kwargs: Any) -> dict[str, Any]:
        return envelope

    monkeypatch.setattr(preview_module, "preview_dispatch", _preview)


async def _agent_resume(request: ApprovalRequest, target_name: str | None) -> dict[str, Any]:
    return await _resume_approved_or_already_resumed(
        operator=_operator(),
        call_arguments={
            "connector_id": _CONNECTOR_ID,
            "op_id": _OP_ID,
            "params": dict(_PARAMS),
            "target": {"name": target_name} if target_name is not None else None,
        },
        awaiting_envelope={"op_id": _OP_ID, "extras": {}},
        approval_request_id=request.id,
    )


# ---------------------------------------------------------------------------
# The target hash
# ---------------------------------------------------------------------------


async def test_target_hash_covers_connection_fields_only() -> None:
    base: dict[str, Any] = dict.fromkeys(TARGET_HASH_FIELDS)
    base.update(host="a.invalid", port=443, extras={"vault_role": "r"})
    reference = compute_target_hash(base)

    for field, value in {
        "host": "b.invalid",
        "port": 8443,
        "secret_ref": "kv/other",
        "verify_tls": False,
        "extras": {"vault_role": "other"},
    }.items():
        assert compute_target_hash({**base, field: value}) != reference, field
    unrelated = {"name": "renamed", "aliases": ["x"], "notes": "n", "version": "9.0"}
    assert compute_target_hash({**base, **unrelated}) == reference


async def test_request_stores_the_hash_of_the_stored_target_row() -> None:
    target_id = await _persist_target("vc-hash")

    request = await _park_approved(target_id, fqdn="per-call.invalid")

    async with get_sessionmaker()() as s:
        stored = await s.get(TargetORM, target_id)
        assert stored is not None
        expected = compute_target_hash({f: getattr(stored, f) for f in TARGET_HASH_FIELDS})
    assert request.target_hash == expected, "the per-call fqdn must not count"


async def test_request_without_target_stores_no_hash() -> None:
    request = await _park_approved(None)

    assert request.target_hash is None


# ---------------------------------------------------------------------------
# Operator surfaces
# ---------------------------------------------------------------------------


async def test_operator_resume_is_refused_when_the_target_host_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("vc-host")
    request = await _park_approved(target_id)
    calls = _spy_dispatch(monkeypatch)
    await _update_target(target_id, host="second.invalid")

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == [], "the vendor must get no call"
    assert result.status == "denied"
    assert result.extras["error_code"] == "target_changed_since_approval"
    assert result.extras["target_id"] == str(target_id)


@pytest.mark.parametrize(
    "fields",
    [
        {"secret_ref": "kv/resumecheck/second"},
        {"extras": {"vault_role": "second"}},
        {"verify_tls": False},
    ],
)
async def test_operator_resume_is_refused_when_other_connection_fields_changed(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, Any]
) -> None:
    target_id = await _persist_target("vc-fields")
    request = await _park_approved(target_id)
    calls = _spy_dispatch(monkeypatch)
    await _update_target(target_id, **fields)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == []
    assert result.extras["error_code"] == "target_changed_since_approval"


async def test_operator_resume_runs_when_only_the_name_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("vc-name")
    request = await _park_approved(target_id)
    calls = _spy_dispatch(monkeypatch)
    await _update_target(target_id, name="vc-name-new", notes="moved rack")

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok"
    assert len(calls) == 1
    assert calls[0]["target"].id == target_id


async def test_operator_resume_of_row_without_hash_runs_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("vc-old-row")
    request = await _park_approved(target_id)
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request.id)
        assert row is not None
        row.target_hash = None
        await s.commit()
    calls = _spy_dispatch(monkeypatch)
    await _update_target(target_id, host="second.invalid")

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# The agent's own resume
# ---------------------------------------------------------------------------


async def test_agent_direct_resume_uses_the_saved_target_not_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_id = await _persist_target("vc-a")
    request = await _park_approved(original_id)
    await _update_target(original_id, name="vc-b")
    impostor_id = await _persist_target("vc-a")
    seen = _spy_agent_dispatch(monkeypatch)

    resumed = await _agent_resume(request, "vc-a")

    assert resumed == {"status": "ok"}
    assert len(seen) == 1
    assert seen[0]["target_id"] == original_id
    assert seen[0]["target_id"] != impostor_id
    assert seen[0]["arguments"]["target"] is None


async def test_agent_direct_resume_is_refused_when_the_target_host_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("vc-agent-host")
    request = await _park_approved(target_id)
    await _update_target(target_id, host="second.invalid")
    seen = _spy_agent_dispatch(monkeypatch)

    resumed = await _agent_resume(request, "vc-agent-host")

    assert seen == []
    assert resumed["status"] == "denied"
    assert resumed["extras"]["error_code"] == "target_changed_since_approval"
    assert (await _row(request.id)).resumed_at is None, "a refused resume keeps the claim free"


async def test_agent_resume_of_tenant_wide_request_has_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _park_approved(None)
    seen = _spy_agent_dispatch(monkeypatch)

    resumed = await _agent_resume(request, None)

    assert resumed == {"status": "ok"}
    assert seen[0]["target_id"] is None
    assert seen[0]["arguments"]["target"] is None


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


async def test_operator_resume_is_refused_when_the_preview_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("vc-preview")
    request = await _park_approved(target_id, preview_hash="0" * 64)
    _fixed_preview(monkeypatch, _PREVIEW)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == []
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_changed_since_approval"
    stored = await _row(request.id)
    assert stored.resumed_at is not None, "the request can never run later"
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "preview_changed_since_approval"


async def test_operator_resume_is_refused_when_the_preview_no_longer_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _park_approved(None, preview_hash=preview_module.compute_preview_hash(_PREVIEW))
    _fixed_preview(monkeypatch, {"status": "error", "error": "unknown_op"})
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert calls == []
    assert result.extras["error_code"] == "preview_changed_since_approval"


async def test_operator_resume_runs_when_the_preview_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("vc-preview-same")
    request = await _park_approved(
        target_id, preview_hash=preview_module.compute_preview_hash(_PREVIEW)
    )
    _fixed_preview(monkeypatch, _PREVIEW)
    calls = _spy_dispatch(monkeypatch)

    result = await resume_dispatch_after_approval(
        operator=_operator("op-approver"), request=await _row(request.id)
    )

    assert result.status == "ok"
    assert len(calls) == 1


async def test_agent_resume_is_refused_when_the_preview_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _persist_target("vc-agent-preview")
    request = await _park_approved(target_id, preview_hash="0" * 64)
    _fixed_preview(monkeypatch, _PREVIEW)
    seen = _spy_agent_dispatch(monkeypatch)

    resumed = await _agent_resume(request, "vc-agent-preview")

    assert seen == []
    assert resumed["status"] == "denied"
    assert resumed["extras"]["error_code"] == "preview_changed_since_approval"
    stored = await _row(request.id)
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "preview_changed_since_approval"
