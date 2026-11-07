# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A waiting request keeps the secret fields of its params only until it ends.

``approval_request.params`` holds the params of a waiting request, because
MEHO runs the approved call with them. A deployment spec in those params
holds passwords and licence keys. The queue replaces each secret-named
value with a marker as soon as:

* the run claim is won (``claim_resume``): the run got the real values,
  the row does not keep them;
* the request is rejected, withdrawn or expires (``end_run_input``);
* an approved run is refused because MEHO was updated (it wins the claim).

A row whose params hold no secret-named value is not rewritten. A re-run
of a failed run needs the params sent again, as for a credential write.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import meho_backplane.operations.dispatcher as dispatcher_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    claim_resume,
    create_pending_request,
    expire_stale_requests,
    expiry_operator,
    get_request,
    reject_request,
    resume_dispatch_after_approval,
    withdraw_request,
)
from meho_backplane.operations.approval_rerun import (
    RerunParamsRequiredError,
    rerun_failed_request,
)
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000559")
_REQUESTER_SUB = "agent:requester"
_REVIEWER_SUB = "op:reviewer"
_OP_ID = "installer.sddc.bringup.start"
_CONNECTOR_ID = "vcf-installer-9.0"
_PW_SENTINEL = "made-up-pw-value"
_LICENCE = "LIC-SENTINEL-77"
_MARKER = "[REDACTED:param_name]"
_SPEC_PARAMS: dict[str, Any] = {
    "spec": {
        "sddcId": "lab-1",
        "vcenterSpec": {"rootPassword": _PW_SENTINEL, "licenseKey": _LICENCE},
        "hostSpecs": [{"hostname": "esx-1", "credentials": {"password": _PW_SENTINEL}}],
    }
}
_PLAIN_PARAMS: dict[str, Any] = {"vm": "vm-42", "state": "off"}
_NUMERIC_PW = 12345678
_NUMERIC_PARAMS: dict[str, Any] = {
    "spec": {"sddcId": "lab-1", "rootPassword": _NUMERIC_PW, "hostCount": 4}
}
_HIDDEN_NUMERIC_PARAMS: dict[str, Any] = {
    "spec": {"sddcId": "lab-1", "rootPassword": _MARKER, "hostCount": 4}
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _requester() -> Operator:
    return Operator(
        sub=_REQUESTER_SUB,
        name="Requester",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


def _reviewer() -> Operator:
    return Operator(
        sub=_REVIEWER_SUB,
        name="Reviewer",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


class _FakeDispatch:
    """Stand-in for ``dispatch``: records the params it was called with."""

    def __init__(self, status: str = "ok") -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> OperationResult:
        self.calls.append(kwargs)
        return OperationResult(
            status=self.status,
            op_id=kwargs["op_id"],
            result={"done": True} if self.status == "ok" else None,
            error=None if self.status == "ok" else "connector_error: still broken",
            duration_ms=1.0,
        )


@pytest.fixture
def fake_dispatch(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatch:
    fake = _FakeDispatch()
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)
    return fake


async def _persist_target(name: str) -> uuid.UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=name,
                product="vcf-installer",
                host="first.invalid",
                port=443,
                secret_ref="kv/installer/first",
                aliases=[],
                extras={},
            )
        )
        await s.commit()
    return target_id


async def _park(
    params: dict[str, Any],
    *,
    approved: bool = False,
    release_id: str | None = None,
    expires_at: datetime | None = None,
    target_id: uuid.UUID | None = None,
) -> uuid.UUID:
    target = SimpleNamespace(id=target_id, fqdn=None) if target_id is not None else None
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_requester(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params=params,
            params_hash=compute_params_hash(params),
        )
        if approved:
            request.status = ApprovalRequestStatus.APPROVED.value
            request.reviewed_by = _REVIEWER_SUB
            request.decided_at = datetime.now(UTC)
        if release_id is not None:
            request.release_id = release_id
        if expires_at is not None:
            request.expires_at = expires_at
        await s.commit()
        return request.id


async def _stored_params(request_id: uuid.UUID) -> dict[str, Any] | None:
    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
        return row.params


async def _load(request_id: uuid.UUID) -> Any:
    async with get_sessionmaker()() as s:
        return await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)


def _assert_hidden(params: dict[str, Any] | None) -> None:
    """The secret fields hold the marker; the other fields keep their value."""
    assert params is not None
    text = json.dumps(params)
    assert _PW_SENTINEL not in text
    assert _LICENCE not in text
    spec = params["spec"]
    assert spec["sddcId"] == "lab-1"
    assert spec["vcenterSpec"] == {"rootPassword": _MARKER, "licenseKey": _MARKER}
    assert spec["hostSpecs"][0] == {"hostname": "esx-1", "credentials": _MARKER}


@pytest.mark.asyncio
async def test_waiting_request_keeps_the_values_its_run_needs() -> None:
    request_id = await _park(_SPEC_PARAMS)

    assert await _stored_params(request_id) == _SPEC_PARAMS


@pytest.mark.asyncio
async def test_run_gets_the_real_values_and_the_row_keeps_none(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park(_SPEC_PARAMS, approved=True)

    result = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )

    assert result.status == "ok"
    assert fake_dispatch.calls[0]["params"] == _SPEC_PARAMS
    _assert_hidden(await _stored_params(request_id))


@pytest.mark.asyncio
async def test_rerun_after_a_failed_run_needs_the_params_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDispatch(status="error")
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)
    request_id = await _park(_SPEC_PARAMS, approved=True)
    first = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )
    assert first.status == "error"
    _assert_hidden(await _stored_params(request_id))

    with pytest.raises(RerunParamsRequiredError):
        await rerun_failed_request(operator=_requester(), request_id=request_id)
    assert len(fake.calls) == 1, "nothing ran with the hidden values"

    fake.status = "ok"
    outcome = await rerun_failed_request(
        operator=_requester(), request_id=request_id, params=dict(_SPEC_PARAMS)
    )
    assert outcome.result.status == "ok"
    assert fake.calls[1]["params"] == _SPEC_PARAMS
    _assert_hidden(await _stored_params(request_id))


@pytest.mark.asyncio
async def test_params_without_secret_fields_stay_as_they_are(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park(_PLAIN_PARAMS, approved=True)

    result = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )

    assert result.status == "ok"
    assert await _stored_params(request_id) == _PLAIN_PARAMS


@pytest.mark.asyncio
async def test_password_sent_as_a_number_is_hidden_after_the_run(
    fake_dispatch: _FakeDispatch,
) -> None:
    request_id = await _park(_NUMERIC_PARAMS, approved=True)

    result = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )

    assert result.status == "ok"
    assert fake_dispatch.calls[0]["params"] == _NUMERIC_PARAMS
    assert await _stored_params(request_id) == _HIDDEN_NUMERIC_PARAMS


@pytest.mark.asyncio
async def test_rerun_with_a_hidden_numeric_password_needs_the_params_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDispatch(status="error")
    monkeypatch.setattr(dispatcher_module, "dispatch", fake)
    request_id = await _park(_NUMERIC_PARAMS, approved=True)
    first = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )
    assert first.status == "error"

    with pytest.raises(RerunParamsRequiredError):
        await rerun_failed_request(operator=_requester(), request_id=request_id)
    assert len(fake.calls) == 1, "nothing ran with the hidden values"


@pytest.mark.asyncio
async def test_rejected_request_keeps_no_numeric_password() -> None:
    request_id = await _park(_NUMERIC_PARAMS)

    async with get_sessionmaker()() as s:
        await reject_request(s, request_id, operator=_reviewer(), reason="wrong spec")
        await s.commit()

    assert await _stored_params(request_id) == _HIDDEN_NUMERIC_PARAMS


@pytest.mark.asyncio
async def test_lost_claim_leaves_the_row_alone() -> None:
    request_id = await _park(_SPEC_PARAMS, approved=True)
    assert await claim_resume(request_id) is True
    _assert_hidden(await _stored_params(request_id))

    assert await claim_resume(request_id) is False
    _assert_hidden(await _stored_params(request_id))


@pytest.mark.asyncio
async def test_pending_request_cannot_be_claimed_and_keeps_its_values() -> None:
    request_id = await _park(_SPEC_PARAMS)

    assert await claim_resume(request_id) is False
    assert await _stored_params(request_id) == _SPEC_PARAMS


@pytest.mark.asyncio
async def test_rejected_request_keeps_no_secret_values() -> None:
    request_id = await _park(_SPEC_PARAMS)

    async with get_sessionmaker()() as s:
        await reject_request(s, request_id, operator=_reviewer(), reason="wrong spec")
        await s.commit()

    _assert_hidden(await _stored_params(request_id))


@pytest.mark.asyncio
async def test_withdrawn_request_keeps_no_secret_values() -> None:
    request_id = await _park(_SPEC_PARAMS)

    async with get_sessionmaker()() as s:
        await withdraw_request(s, request_id, operator=_requester(), reason="plan changed")
        await s.commit()

    _assert_hidden(await _stored_params(request_id))


@pytest.mark.asyncio
async def test_expired_request_keeps_no_secret_values() -> None:
    request_id = await _park(_SPEC_PARAMS, expires_at=datetime.now(UTC) - timedelta(minutes=1))

    async with get_sessionmaker()() as s:
        expired = await expire_stale_requests(s, operator=expiry_operator(_TENANT_ID))
        await s.commit()

    assert [r.id for r in expired] == [request_id]
    _assert_hidden(await _stored_params(request_id))


@pytest.mark.asyncio
async def test_run_refused_after_a_meho_update_keeps_no_secret_values(
    monkeypatch: pytest.MonkeyPatch, fake_dispatch: _FakeDispatch
) -> None:
    request_id = await _park(_SPEC_PARAMS, approved=True, release_id="1" * 40)
    monkeypatch.setenv("GIT_SHA", "2" * 40)

    result = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )

    assert result.extras["error_code"] == "release_changed_while_waiting"
    assert fake_dispatch.calls == []
    _assert_hidden(await _stored_params(request_id))


@pytest.mark.asyncio
async def test_run_refused_after_a_target_change_keeps_no_secret_values(
    fake_dispatch: _FakeDispatch,
) -> None:
    target_id = await _persist_target("installer-1")
    request_id = await _park(_SPEC_PARAMS, approved=True, target_id=target_id)
    async with get_sessionmaker()() as s:
        target = await s.get(TargetORM, target_id)
        assert target is not None
        target.host = "second.invalid"
        await s.commit()

    result = await resume_dispatch_after_approval(
        operator=_requester(), request=await _load(request_id)
    )

    assert result.extras["error_code"] == "target_changed_since_approval"
    assert fake_dispatch.calls == []
    _assert_hidden(await _stored_params(request_id))
