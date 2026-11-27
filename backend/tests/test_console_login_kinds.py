# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The five console steps that check a person's token name the kind ``person``.

Each step checks the token with the shared token check
(:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`), which sets no
mark. The person is at the web console and acts in that moment, so each
step gives its login ``in_code_kind="person"``:

* the approval sign-in (``/ui/auth/approval-callback``);
* the console sign-in (``/ui/auth/callback``);
* the tenant choice (``POST /ui/tenant/switch``);
* the reset of a session whose tenant is gone;
* the read of the approval sign-in, the proof of a console approve.

A whole-path test signs in through the real approval sign-in route, then
approves a waiting request: the approve and its proof carry ``person``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx

from meho_backplane.auth.caller_kind import caller_kind
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import approval_sign_in_routes, approval_token_store
from meho_backplane.ui.auth import routes as auth_routes
from meho_backplane.ui.auth import tenant as auth_tenant
from meho_backplane.ui.auth.routes import SESSION_COOKIE_NAME
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from meho_backplane.ui.routes import tenant as tenant_routes
from meho_backplane.ui.routes.approvals import routes as approvals_routes
from meho_backplane.ui.routes.approvals import sign_in as approvals_sign_in
from tests.conftest import (
    DEFAULT_AUDIENCE,
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    public_jwks,
)
from tests.test_ui_approval_sign_in_routes import (
    _approval_token,
    _patch_token_answers,
    _query,
    _token_body,
)
from tests.test_ui_approvals import (
    _RESUME_DISPATCH,
    _csrf_token,
    _dispatch_result,
    _request_status,
    _seed_request,
)
from tests.test_ui_approvals import _build_app as _approvals_app
from tests.test_ui_auth_flow import _bff_env, _https_client, _mock_oidc_metadata

__all__ = ["_bff_env"]

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)


def _plain_person() -> Operator:
    """What the shared token check gives: a person's login, no mark, no kind."""
    return Operator(
        sub="op-42",
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        in_code_kind=None,
    )


def _assert_person(operator: Operator) -> None:
    assert operator.in_code_kind == "person"
    assert operator.token_checked is False
    assert caller_kind(operator, None) == "person"


@pytest.mark.asyncio
async def test_the_tenant_choice_is_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Operator] = []

    async def _refresh(operator: Operator) -> None:
        seen.append(operator)

    current = _plain_person().model_copy(update={"web_session": True})
    monkeypatch.setattr(
        tenant_routes,
        "lift_session_operator",
        AsyncMock(return_value=(SimpleNamespace(access_token="t"), current)),
    )
    monkeypatch.setattr(
        tenant_routes, "verify_jwt_for_audience", AsyncMock(return_value=_plain_person())
    )
    monkeypatch.setattr(tenant_routes, "save_session_tenant", AsyncMock())
    monkeypatch.setattr(tenant_routes, "_bind_switch_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(tenant_routes, "refresh_at_sign_in", _refresh)
    form = AsyncMock(return_value={"tenant": str(_TENANT)})
    session_ctx = SimpleNamespace(session_id=uuid.uuid4(), operator_sub="op-42")

    response = await tenant_routes._switch(SimpleNamespace(form=form), session_ctx)  # type: ignore[arg-type]

    assert response.status_code == 303
    (operator,) = seen
    _assert_person(operator)


@pytest.mark.asyncio
async def test_the_reset_of_a_session_whose_tenant_is_gone_is_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Operator] = []

    def _can_switch(operator: Operator) -> bool:
        seen.append(operator)
        return False

    monkeypatch.setattr(
        auth_tenant, "verify_jwt_for_audience", AsyncMock(return_value=_plain_person())
    )
    monkeypatch.setattr(auth_tenant, "save_session_tenant", AsyncMock())
    monkeypatch.setattr(auth_tenant, "can_switch_tenant", _can_switch)
    decrypted = SimpleNamespace(id=uuid.uuid4(), operator_sub="op-42", tenant_id=_TENANT)

    with pytest.raises(auth_tenant.TenantRemovedError):
        await auth_tenant.fall_back_after_removal(
            decrypted,  # type: ignore[arg-type]
            access_token="t",
            expected_audience=DEFAULT_AUDIENCE,
        )

    (operator,) = seen
    _assert_person(operator)


@pytest.mark.asyncio
async def test_the_read_of_the_approval_sign_in_is_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approval_token_store, "load_approval_token", AsyncMock(return_value="t"))
    monkeypatch.setattr(
        approval_token_store, "verify_jwt_for_audience", AsyncMock(return_value=_plain_person())
    )

    operator = await approval_token_store.approval_operator(uuid.uuid4())

    assert operator is not None
    _assert_person(operator)


def _sign_in_then_approval_sign_in(
    monkeypatch: pytest.MonkeyPatch, key: Any, client: Any
) -> uuid.UUID:
    """Log in to the console, then run the approval sign-in, inside one respx mock."""
    login_access = mint_token(key, sub="op-42", audience=DEFAULT_AUDIENCE)
    answers = [_token_body(login_access), _token_body(_approval_token(key), refresh=False)]
    _patch_token_answers(monkeypatch, answers)
    login = client.get("/ui/auth/login?return_to=/ui/approvals")
    state = _query(login.headers["location"])["state"][0]
    callback = client.get(f"/ui/auth/callback?code=login-code&state={state}")
    assert callback.status_code == 302, callback.text
    start = client.get("/ui/auth/approval-sign-in?return_to=/ui/approvals")
    query = _query(start.headers["location"])
    kept = client.get(f"/ui/auth/approval-callback?code=approval-code&state={query['state'][0]}")
    assert kept.status_code == 302, kept.text
    return uuid.UUID(client.cookies[SESSION_COOKIE_NAME])


def test_the_console_sign_in_and_the_approval_sign_in_are_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed_in: list[Operator] = []
    approval_sign_ins: list[Operator] = []
    real_check = approval_sign_in_routes.is_web_approval_sign_in

    async def _refresh(operator: Operator) -> None:
        signed_in.append(operator)

    def _check(operator: Operator, settings: Any) -> bool:
        approval_sign_ins.append(operator)
        return real_check(operator, settings)

    monkeypatch.setattr(auth_routes, "refresh_at_sign_in", _refresh)
    monkeypatch.setattr(approval_sign_in_routes, "is_web_approval_sign_in", _check)
    key = make_rsa_keypair("test-kid")
    with respx.mock(assert_all_called=False) as router:
        _mock_oidc_metadata(router, jwks=public_jwks(key))
        _sign_in_then_approval_sign_in(monkeypatch, key, _https_client())

    (operator,) = signed_in
    _assert_person(operator)
    (approval,) = approval_sign_ins
    _assert_person(approval)


def test_an_approve_after_the_real_approval_sign_in_carries_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "required")
    get_settings.cache_clear()
    approvers: list[Operator] = []
    proofs: list[Operator | None] = []
    real_approve = approvals_routes.approve_request
    real_check = approvals_sign_in.check_web_approval_sign_in

    async def _approve(*args: Any, operator: Operator, **kwargs: Any) -> Any:
        approvers.append(operator)
        return await real_approve(*args, operator=operator, **kwargs)

    def _check(operator: Operator, approval: Operator | None, *args: Any, **kwargs: Any) -> Any:
        proofs.append(approval)
        return real_check(operator, approval, *args, **kwargs)

    monkeypatch.setattr(approvals_routes, "approve_request", _approve)
    monkeypatch.setattr(approvals_sign_in, "check_web_approval_sign_in", _check)
    key = make_rsa_keypair("test-kid")
    client = _https_client(_approvals_app())
    with (
        respx.mock(assert_all_called=False) as router,
        patch(_RESUME_DISPATCH, AsyncMock(return_value=_dispatch_result())),
    ):
        _mock_oidc_metadata(router, jwks=public_jwks(key))
        session_id = _sign_in_then_approval_sign_in(monkeypatch, key, client)
        rid = _seed_request(tenant_id=_TENANT)
        csrf = _csrf_token(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(
            f"/ui/approvals/{rid}/approve",
            data={"reason": "checked"},
            headers={CSRF_HEADER_NAME: csrf},
        )

    assert response.status_code == 200, response.text
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    (approver,) = approvers
    assert caller_kind(approver, None) == "person"
    (proof,) = proofs
    assert proof is not None
    _assert_person(proof)
    for login in (approver, proof):
        assert caller_kind(login, None) not in ("system", "personal_agent")
