# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved request whose run failed must not look like a plain "approved" (#3543).

After a human approves a request, MEHO runs the operation and stores the
result envelope on ``approval_request.resume_result`` (#3209). The request
``status`` stays ``approved`` either way. This suite pins the run summary
that the list and show views now carry next to ``result_state``:
``resume_outcome`` with the run start time, the result status, the error
code and the run's audit id. The result body and the free-text error never
leave the owner-only ``/result`` read.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.main import app
from meho_backplane.mcp.tools.approvals import _row_to_dict
from meho_backplane.operations._errors import (
    result_composite_terminal_error,
    result_connector_error,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_outcome import (
    ResumeOutcome,
    resume_outcome,
    resume_outcome_from_envelope,
)
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    get_request,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003543")
_OWNER_SUB = "addon:kv-writer"
_APPROVER_SUB = "op-approver"
_OP_ID = "vault.kv.put"
_CONNECTOR_ID = "vault-1.x"
_AUDIT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_CDROM_OP_ID = "vmware.composite.vm.device.cdrom"
_GUIDANCE = (
    "The change did not answer in time and may still run (vCenter tasks: task-7). "
    "Do not start it again yet. The VM waits on question _vmx1: show it to the user "
    "and answer it with vmware.vm.question.answer."
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
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
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="Test Principal",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _park_approved(op_id: str = _OP_ID, connector_id: str = _CONNECTOR_ID) -> uuid.UUID:
    params = {"path": "ssh/known_hosts", "value": "<pin>"}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(_OWNER_SUB),
            connector_id=connector_id,
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        await s.commit()
        return request.id


def _failed_run() -> OperationResult:
    """The envelope a connector error produces, with the run's audit id."""
    failed = result_connector_error(_OP_ID, RuntimeError("permission denied"), 7.0)
    return failed.model_copy(update={"audit_id": _AUDIT_ID})


#: One signing key for the module: the JWKS cache keeps the first key it saw.
_KEY = make_rsa_keypair("kid-3543")


async def _get_json(path: str) -> Any:
    key = _KEY
    token = mint_token(
        key,
        sub=_APPROVER_SUB,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_ID),
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            response = await ac.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Reading the summary off the envelope
# ---------------------------------------------------------------------------


def test_outcome_reads_status_error_code_and_audit_id() -> None:
    envelope = _failed_run().model_dump(mode="json")
    outcome = resume_outcome_from_envelope(
        envelope, parked_op_id=_OP_ID, resumed_at="2026-09-12T10:00:00+00:00"
    )
    assert outcome == ResumeOutcome(
        resumed_at="2026-09-12T10:00:00+00:00",
        result_status="error",
        error_code="connector_error",
        audit_id=_AUDIT_ID,
    )


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        {},
        {"status": 5, "extras": "not-a-dict", "audit_id": "not-a-uuid"},
        {"status": "", "extras": {"error_code": None}, "audit_id": 42},
    ],
)
def test_outcome_tolerates_odd_envelopes(envelope: dict[str, Any] | None) -> None:
    """The column is plain JSON: an odd field reads as ``None``, never raises."""
    assert resume_outcome_from_envelope(envelope, parked_op_id=_CDROM_OP_ID) == ResumeOutcome(
        None, None, None, None, None
    )


@pytest.mark.asyncio
async def test_outcome_is_none_until_an_approved_request_runs() -> None:
    request_id = await _park_approved()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert resume_outcome(row) is None  # approved, not run yet

        row.resumed_at = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
        started = resume_outcome(row)
        assert started is not None
        assert started.resumed_at == "2026-09-12T10:00:00+00:00"
        assert started.result_status is None

        row.status = ApprovalRequestStatus.REJECTED.value
        assert resume_outcome(row) is None


# ---------------------------------------------------------------------------
# End to end: a resume that errors shows up on the REST show and list views
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_resume_shows_on_show_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = await _park_approved()

    async def _fake_dispatch(**_kwargs: object) -> OperationResult:
        return _failed_run()

    monkeypatch.setattr(dispatcher_module, "dispatch", _fake_dispatch)
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    result = await resume_dispatch_after_approval(operator=_operator(_APPROVER_SUB), request=row)
    assert result.status == "error"

    shown = await _get_json(f"/api/v1/approvals/{request_id}")
    assert shown["status"] == "approved"
    assert shown["result_state"] == "failed"
    outcome = shown["resume_outcome"]
    assert outcome["result_status"] == "error"
    assert outcome["error_code"] == "connector_error"
    assert outcome["audit_id"] == str(_AUDIT_ID)
    assert outcome["resumed_at"] is not None
    # Only the summary leaves the envelope: no result body, no free-text error.
    assert set(outcome) == {"resumed_at", "result_status", "error_code", "audit_id", "guidance"}
    assert outcome["guidance"] is None
    assert "permission denied" not in str(shown)

    listed = await _get_json("/api/v1/approvals?status=approved")
    (row_view,) = [v for v in listed if v["id"] == str(request_id)]
    assert row_view["result_state"] == "failed"
    assert row_view["resume_outcome"] == outcome

    # The MCP operator plane renders the same summary.
    async with get_sessionmaker()() as s:
        stored = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    mcp_row = _row_to_dict(stored)
    assert mcp_row["result_state"] == "failed"
    assert mcp_row["resume_outcome"] == outcome


@pytest.mark.asyncio
async def test_not_run_request_has_no_outcome() -> None:
    request_id = await _park_approved()
    shown = await _get_json(f"/api/v1/approvals/{request_id}")
    assert shown["result_state"] == "not_started"
    assert shown["resume_outcome"] is None


# ---------------------------------------------------------------------------
# The next step a failed multi-step operation names (#5377)
# ---------------------------------------------------------------------------


def _composite_error(
    op_id: str = _CDROM_OP_ID,
    *,
    status: str = "result_unknown",
    guidance: str | None = _GUIDANCE,
) -> OperationResult:
    """The error a multi-step operation returns itself (#3809), with the run's audit id."""
    body = {
        "status": status,
        "vm_question": {"id": "_vmx1", "text": "The guest has locked the CD-ROM door."},
        "guidance": guidance,
        "issues": [{"severity": "error", "message": "the device change timed out"}],
    }
    failed = result_composite_terminal_error(
        op_id,
        error_code=status,
        envelope=body,
        error_summary=f"{status}: the device change timed out",
        duration_ms=30175.0,
    )
    return failed.model_copy(update={"audit_id": _AUDIT_ID})


def test_outcome_keeps_the_guidance_of_a_multi_step_operation_error() -> None:
    envelope = _composite_error().model_dump(mode="json")
    outcome = resume_outcome_from_envelope(envelope, parked_op_id=_CDROM_OP_ID)
    assert outcome.result_status == "error"
    assert outcome.error_code == "result_unknown"
    assert outcome.guidance == _GUIDANCE


def test_outcome_has_no_guidance_for_an_ok_run() -> None:
    envelope = {"status": "ok", "op_id": _CDROM_OP_ID, "result": {"guidance": "do this"}}
    assert resume_outcome_from_envelope(envelope, parked_op_id=_CDROM_OP_ID).guidance is None


def test_outcome_has_no_guidance_from_a_body_that_is_not_the_operation_error() -> None:
    """A result body whose status is not the error code is not MEHO's own error answer."""
    envelope = _composite_error().model_dump(mode="json")
    envelope["result"]["status"] = "vendor_status"
    assert resume_outcome_from_envelope(envelope, parked_op_id=_CDROM_OP_ID).guidance is None


def test_outcome_cuts_a_long_guidance() -> None:
    envelope = _composite_error(guidance="x" * 2000).model_dump(mode="json")
    guidance = resume_outcome_from_envelope(envelope, parked_op_id=_CDROM_OP_ID).guidance
    assert guidance is not None
    assert guidance.endswith(" [truncated]")
    assert len(guidance) < 600


def test_credential_operation_run_summary_has_no_vendor_text_and_no_params() -> None:
    """For a credential operation, the summary holds only codes and ids (#4854 rule)."""
    vendor_error = RuntimeError("vault said: value hunter2 rejected at secret/app")
    failed = result_connector_error(_OP_ID, vendor_error, 7.0).model_copy(
        update={"audit_id": _AUDIT_ID}
    )
    outcome = resume_outcome_from_envelope(failed.model_dump(mode="json"), parked_op_id=_OP_ID)
    assert outcome.guidance is None
    assert outcome.error_code == "connector_error"
    assert "hunter2" not in str(outcome)
    assert "secret/app" not in str(outcome)

    # The same holds when the credential operation returns its own error answer
    # with a guidance that repeats a param.
    own_error = _composite_error(_OP_ID, status="write_error", guidance="path secret/app: hunter2")
    outcome = resume_outcome_from_envelope(own_error.model_dump(mode="json"), parked_op_id=_OP_ID)
    assert outcome.guidance is None
    assert "hunter2" not in str(outcome)


def test_outcome_has_no_guidance_when_the_parked_step_is_a_credential_operation() -> None:
    """A multi-step operation that resumed for a parked credential step keeps no guidance."""
    envelope = _composite_error().model_dump(mode="json")
    assert resume_outcome_from_envelope(envelope, parked_op_id=_OP_ID).guidance is None


@pytest.mark.asyncio
async def test_result_unknown_resume_shows_the_guidance_on_show_list_and_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = await _park_approved(op_id=_CDROM_OP_ID, connector_id="vmware-rest-8.0")

    async def _fake_dispatch(**_kwargs: object) -> OperationResult:
        return _composite_error()

    monkeypatch.setattr(dispatcher_module, "dispatch", _fake_dispatch)
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    await resume_dispatch_after_approval(operator=_operator(_APPROVER_SUB), request=row)

    shown = await _get_json(f"/api/v1/approvals/{request_id}")
    outcome = shown["resume_outcome"]
    assert shown["result_state"] == "failed"
    assert outcome["error_code"] == "result_unknown"
    assert outcome["guidance"] == _GUIDANCE
    # The vCenter question text stays on the owner-only /result read.
    assert "locked the CD-ROM door" not in str(shown)

    listed = await _get_json("/api/v1/approvals?status=approved")
    (row_view,) = [v for v in listed if v["id"] == str(request_id)]
    assert row_view["resume_outcome"] == outcome

    async with get_sessionmaker()() as s:
        stored = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    assert _row_to_dict(stored)["resume_outcome"] == outcome
