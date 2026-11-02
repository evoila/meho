# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The appliance password change end to end: the new password never leaves the PATCH body.

One person asks for ``vmware.appliance.local_account.password.set``. The
call waits for approval. A second person approves it, and MEHO runs it. The
test sends this through the real dispatcher, the approval queue (with its
encrypted handoff store), the real ``VmwareRestConnector`` over respx, and a
fake Vault that holds the target's login and the new password at two paths.
The flight recorder is switched on and its spans are kept.

It then checks that the new password is in the ``PATCH`` request to vCenter
and nowhere else: not in the call's params, the approval request, any audit
row, the result, any flight-recorder span, or any log line. No live system
is used.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.appliance_local_account_password import (
    PASSWORD_SET_OP_ID,
)
from meho_backplane.connectors.vmware_rest.typed_ops import register_vmware_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder import capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a1c5")
_HOST = "vcenter-local-accounts.test.invalid"
_BASE_URL = f"https://{_HOST}"
_TARGET_SECRET_REF = "targets/local-accounts/vcenter"
_NEW_PASSWORD_REF = f"tenants/{_TENANT_ID}/appliance/vcenter/root-new"
#: Made-up values. The test fails if the new password shows up anywhere but the PATCH.
_SERVICE_ACCOUNT = "svc-meho-local-accounts"
_SERVICE_PASSWORD = "svc-login-canary-7c1d"
_NEW_PASSWORD = "Root-canary-9f4e-must-not-leak"
_SESSION_TOKEN = "local-accounts-session-token"
_ROOT_PATH = "/api/appliance/local-accounts/root"
_AFTER: dict[str, Any] = {
    "roles": ["superAdmin"],
    "enabled": True,
    "has_password": True,
    "last_password_change": "2026-10-24T00:00:00.000Z",
    "password_expires_at": "2027-01-22T00:00:00.000Z",
    "max_days_between_password_change": 90,
    "min_days_between_password_change": 0,
    "warn_days_before_password_expiration": 7,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=f"Person {sub}",
        email=None,
        raw_jwt=f"{sub}.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _two_path_vault(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A fake Vault with the target login and the new password at two paths."""
    fake = install_fake_client(monkeypatch, secret={})
    secrets = {
        _TARGET_SECRET_REF: {"username": _SERVICE_ACCOUNT, "password": _SERVICE_PASSWORD},
        _NEW_PASSWORD_REF: {"password": _NEW_PASSWORD},
    }
    reads: list[str] = []

    def _read(path: str, mount_point: str = "secret", **_: Any) -> dict[str, Any]:
        reads.append(path)
        return {"data": {"data": secrets[path], "metadata": {"version": 1, "path": path}}}

    monkeypatch.setattr(fake.secrets.kv.v2, "read_secret_version", _read)
    return reads


def _keep_flight_recorder_spans(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Switch the flight recorder on and keep each trace's spans in memory."""
    spans: list[Any] = []

    async def _record_trace(**kwargs: Any) -> None:
        spans.extend(kwargs["spans"])

    monkeypatch.setattr(capture, "should_capture", AsyncMock(return_value=True))
    monkeypatch.setattr(capture, "record_trace", _record_trace)
    return spans


async def _persist_target() -> TargetORM:
    target = TargetORM(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        name="vcenter-local-accounts",
        product="vmware",
        version="9.0.0",
        host=_HOST,
        port=443,
        secret_ref=_TARGET_SECRET_REF,
        auth_model="shared_service_account",
        aliases=[],
    )
    async with get_sessionmaker()() as s:
        s.add(target)
        await s.commit()
        await s.refresh(target)
    return target


def _assert_no_password(label: str, blob: str) -> None:
    for form in (_NEW_PASSWORD, json.dumps(_NEW_PASSWORD)[1:-1]):
        assert form not in blob, f"the new password appears in {label}"


@pytest.mark.asyncio
async def test_password_change_waits_for_approval_and_the_password_stays_in_the_patch(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    vault_reads = _two_path_vault(monkeypatch)
    spans = _keep_flight_recorder_spans(monkeypatch)
    target = await _persist_target()
    requester, approver = _person("ops-requester"), _person("ops-approver")
    params = {"username": "root", "password_secret_ref": _NEW_PASSWORD_REF}

    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json=_SESSION_TOKEN)
            mock.delete("/api/session").respond(204)
            patch_route = mock.patch(_ROOT_PATH).respond(204)
            mock.get(_ROOT_PATH).respond(200, json=_AFTER)

            # 1. The requester's call waits for approval; nothing reaches vCenter.
            parked = await dispatch(
                operator=requester,
                connector_id=_CONNECTOR_ID,
                op_id=PASSWORD_SET_OP_ID,
                target=target,
                params=params,
            )
            assert parked.status == "awaiting_approval", parked.error
            assert patch_route.call_count == 0
            assert _NEW_PASSWORD_REF not in vault_reads
            request_id = UUID(parked.extras["approval_request_id"])

            async with get_sessionmaker()() as s:
                pending = await s.get(ApprovalRequest, request_id)
                assert pending is not None
                assert pending.params is None  # kept in the encrypted handoff store
                preview = pending.proposed_effect["preview"]
                assert preview["username"] == "root"
                assert preview["password_secret_ref"] == _NEW_PASSWORD_REF

            # 2. A second person approves; MEHO runs the change under the approver.
            async with get_sessionmaker()() as s:
                approved = await approve_request(s, request_id, operator=approver)
                await s.commit()
            assert approved.status == ApprovalRequestStatus.APPROVED.value
            async with get_sessionmaker()() as s:
                row = await s.get(ApprovalRequest, request_id)
                assert row is not None
                result = await resume_dispatch_after_approval(operator=approver, request=row)

    assert result.status == "ok", result.error
    assert result.result == {
        "username": "root",
        "password_set": True,
        "password_expires_at": "2027-01-22T00:00:00.000Z",
        "read_note": None,
    }
    assert patch_route.call_count == 1
    assert json.loads(patch_route.calls[0].request.content) == {"password": _NEW_PASSWORD}
    assert _NEW_PASSWORD_REF in vault_reads

    async with get_sessionmaker()() as s:
        approval = await s.get(ApprovalRequest, request_id)
        audit_rows = (await s.execute(select(AuditLog))).scalars().all()
    assert approval is not None
    assert audit_rows, "the dispatch wrote no audit rows"
    _assert_no_password("the call params", json.dumps(params))
    _assert_no_password("the result", result.model_dump_json())
    _assert_no_password(
        "the approval request",
        json.dumps(
            {
                column.name: getattr(approval, column.name)
                for column in ApprovalRequest.__table__.columns
            },
            default=str,
        ),
    )
    _assert_no_password(
        "the audit rows",
        json.dumps(
            [
                {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns}
                for row in audit_rows
            ],
            default=str,
        ),
    )
    vendor_spans = [span for span in spans if span.span_kind == "vendor_call"]
    patch_spans = [span for span in vendor_spans if span.name.startswith("PATCH ")]
    assert len(patch_spans) == 1, "no flight-recorder span for the PATCH"
    assert patch_spans[0].attributes["body_recorded"] is False
    _assert_no_password(
        "the flight-recorder spans",
        json.dumps([dataclasses.asdict(span) for span in spans], default=str),
    )
    _assert_no_password("the structlog events", json.dumps(logs, default=str))
    _assert_no_password("the stdlib log records", caplog.text)


@pytest.mark.asyncio
async def test_two_401_answers_that_echo_the_password_leak_nothing(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """vCenter answers 401 twice and echoes the password; MEHO logs in again once and leaks nothing.

    The first 401 makes the dispatcher drop the session, log in again and run
    the change once more. The second 401 becomes ``connector_auth_failed``.
    The status and the error class must survive the handler (or the retry
    would not run), and the echoed password must not reach the result, the
    audit rows, the flight recorder or the logs.
    """
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    _two_path_vault(monkeypatch)
    spans = _keep_flight_recorder_spans(monkeypatch)
    target = await _persist_target()
    params = {"username": "root", "password_secret_ref": _NEW_PASSWORD_REF}
    echo = {
        "error_type": "UNAUTHENTICATED",
        "messages": [
            {"default_message": f"session rejected for {_NEW_PASSWORD}", "args": [_NEW_PASSWORD]}
        ],
        "message": f"password {_NEW_PASSWORD} rejected",
    }

    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            session_route = mock.post("/api/session").respond(200, json=_SESSION_TOKEN)
            mock.delete("/api/session").respond(204)
            patch_route = mock.patch(_ROOT_PATH).respond(401, json=echo)
            result = await dispatch(
                operator=_person("ops-approver"),
                connector_id=_CONNECTOR_ID,
                op_id=PASSWORD_SET_OP_ID,
                target=target,
                params=params,
                _approved=True,
            )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["http_status"] == 401
    assert result.extras["cause"] == "session_dispatch_401_after_relogin"
    assert patch_route.call_count == 2, "the login retry did not run"
    assert session_route.call_count == 2

    async with get_sessionmaker()() as s:
        audit_rows = (await s.execute(select(AuditLog))).scalars().all()
    assert audit_rows, "the dispatch wrote no audit rows"
    _assert_no_password("the result", result.model_dump_json())
    _assert_no_password(
        "the audit rows",
        json.dumps(
            [
                {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns}
                for row in audit_rows
            ],
            default=str,
        ),
    )
    _assert_no_password(
        "the flight-recorder spans",
        json.dumps([dataclasses.asdict(span) for span in spans], default=str),
    )
    _assert_no_password("the structlog events", json.dumps(logs, default=str))
    _assert_no_password("the stdlib log records", caplog.text)
