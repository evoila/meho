# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A console approval needs the same sign-in level as one on the command line (#6282).

``APPROVAL_SIGN_IN_LEVEL`` decides for the console too:

* ``required``: only the console's approval sign-in (client
  ``meho-web-approve``) with the approval level approves. A normal console
  session, also a fresh one, gets the approval sign-in link, not the
  Approve button.
* ``observe``: the console approves as before and records the level it saw.
* ``off``: the console approves as before.

The approval sign-in token is read from the session row
(:func:`~meho_backplane.ui.auth.approval_token_store.approval_operator`);
the tests replace that read with the token's person.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import respx

from meho_backplane.auth.approval_sign_in_flow import ApprovalSignInStatus, SignInLevel
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.approvals.sign_in import web_sign_in_level
from tests.test_ui_approvals import (
    _REQUESTER_SUB,
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _authenticated_client,
    _bff_env,
    _decision_approval_auth,
    _operator,
    _post_decision,
    _request_status,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

__all__ = ["_bff_env"]

_APPROVAL_OPERATOR = "meho_backplane.ui.routes.approvals.sign_in.approval_operator"
_APPROVAL_SIGN_IN_URL = "/ui/auth/approval-sign-in?return_to=%2Fui%2Fapprovals"


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", mode)
    get_settings.cache_clear()


def _approval_sign_in(*, acr: str = "meho-approve", age: int = 5) -> Operator:
    """The person's console approval sign-in, as the session row gives it back."""
    return Operator(
        sub=_REVIEWER_SUB,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        azp="meho-web-approve",
        acr=acr,
        auth_time=int(time.time()) - age,
        sid="kc-sid-approve",
    )


def _approve(approval: Operator | None, *, sign_in_age: int = 5) -> tuple[object, uuid.UUID]:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB, sign_in_age=sign_in_age)
    with patch(_APPROVAL_OPERATOR, new_callable=AsyncMock, return_value=approval):
        response = _post_decision(operator, rid)
    return response, rid


@pytest.mark.parametrize("mode", ["off", "observe", "required"])
def test_a_console_approval_after_the_approval_sign_in_is_accepted(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _set_mode(monkeypatch, mode)
    approval = _approval_sign_in()

    response, rid = _approve(approval)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    recorded = _decision_approval_auth(rid)
    assert isinstance(recorded, dict)
    assert recorded["channel"] == "web"
    assert recorded["acr"] == "meho-approve"
    assert recorded["auth_time"] == approval.auth_time
    assert recorded["level_in_force"] == mode
    assert "level_missing" not in recorded


@pytest.mark.parametrize("sign_in_age", [1, 30], ids=["fresh-login", "recent-login"])
def test_required_refuses_a_normal_console_session_and_offers_the_approval_sign_in(
    monkeypatch: pytest.MonkeyPatch, sign_in_age: int
) -> None:
    _set_mode(monkeypatch, "required")

    response, rid = _approve(None, sign_in_age=sign_in_age)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    text = response.text  # type: ignore[attr-defined]
    assert 'data-testid="approval-fresh-sign-in"' in text
    assert f'href="{_APPROVAL_SIGN_IN_URL}"' in text
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value


def test_required_refuses_an_approval_sign_in_without_the_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "required")

    response, rid = _approve(_approval_sign_in(acr="1"))

    text = response.text  # type: ignore[attr-defined]
    assert f'href="{_APPROVAL_SIGN_IN_URL}"' in text
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value


def test_observe_approves_a_normal_session_and_records_the_missing_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "observe")

    response, rid = _approve(None, sign_in_age=30)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    recorded = _decision_approval_auth(rid)
    assert isinstance(recorded, dict)
    assert recorded["acr"] == "1"
    assert recorded["level_missing"] is True
    assert recorded["level_in_force"] == "observe"


def test_off_approves_a_normal_session_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_mode(monkeypatch, "off")

    response, rid = _approve(None, sign_in_age=30)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    recorded = _decision_approval_auth(rid)
    assert isinstance(recorded, dict)
    assert recorded["level_in_force"] == "off"
    assert "level_missing" not in recorded


def _modal(approval: Operator | None) -> str:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_request(tenant_id=_TENANT_A, principal_sub=_REQUESTER_SUB)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB, sign_in_age=5)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_APPROVAL_OPERATOR, new_callable=AsyncMock, return_value=approval),
        ):
            response = client.get(f"/ui/approvals/{rid}")
    assert response.status_code == 200, response.text
    return response.text


def test_required_modal_offers_the_approval_sign_in_for_a_fresh_normal_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "required")

    text = _modal(None)

    assert 'data-action="sign-in-to-approve"' in text
    assert f'href="{_APPROVAL_SIGN_IN_URL}"' in text
    assert 'data-action="approve"' not in text


def test_required_modal_shows_approve_after_the_approval_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "required")

    text = _modal(_approval_sign_in())

    assert 'data-action="approve"' in text
    assert 'data-action="sign-in-to-approve"' not in text


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_observe_and_off_modal_keep_the_approve_button_and_the_old_sign_in_link(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _set_mode(monkeypatch, mode)

    text = _modal(None)

    assert 'data-action="approve"' in text
    assert _APPROVAL_SIGN_IN_URL not in text


_READ_APPROVAL_SIGN_IN = "meho_backplane.auth.approval_sign_in_flow.read_approval_sign_in"


def _empty_console_client(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("UI_KEYCLOAK_APPROVE_CLIENT_ID", "")
    _set_mode(monkeypatch, mode)


def _bound_cli_client() -> ApprovalSignInStatus:
    return ApprovalSignInStatus(state="ok", client=get_settings().keycloak_cli_approve_client_id)


async def test_auto_with_an_empty_console_client_records_only_also_when_the_cli_client_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_console_client(monkeypatch, "auto")

    with patch(
        _READ_APPROVAL_SIGN_IN, new_callable=AsyncMock, return_value=_bound_cli_client()
    ) as read:
        level = await web_sign_in_level()

    assert level == SignInLevel("observe")
    read.assert_not_called()


def test_auto_with_an_empty_console_client_keeps_the_approve_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_console_client(monkeypatch, "auto")

    with patch(_READ_APPROVAL_SIGN_IN, new_callable=AsyncMock, return_value=_bound_cli_client()):
        text = _modal(None)

    assert 'data-action="approve"' in text
    assert _APPROVAL_SIGN_IN_URL not in text


def test_auto_with_an_empty_console_client_approves_and_records_observe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_console_client(monkeypatch, "auto")

    with patch(_READ_APPROVAL_SIGN_IN, new_callable=AsyncMock, return_value=_bound_cli_client()):
        response, rid = _approve(None, sign_in_age=1)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert _request_status(rid) == ApprovalRequestStatus.APPROVED.value
    recorded = _decision_approval_auth(rid)
    assert isinstance(recorded, dict)
    assert recorded["level_in_force"] == "observe"


def test_required_with_an_empty_console_client_still_refuses_a_console_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_console_client(monkeypatch, "required")

    response, rid = _approve(None, sign_in_age=1)

    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert 'data-testid="approval-fresh-sign-in"' in response.text  # type: ignore[attr-defined]
    assert _request_status(rid) == ApprovalRequestStatus.PENDING.value
