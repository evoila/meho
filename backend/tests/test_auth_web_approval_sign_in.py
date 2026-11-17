# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console approval needs the same sign-in level as the command line (#6282).

:func:`~meho_backplane.auth.web_approval_sign_in.check_web_approval_sign_in`
takes the console session's person and, when there is one, the token of the
person's console approval sign-in (client ``meho-web-approve``).

* ``required``: only that token approves, and only with the approval level.
  A normal console session, also a fresh one, gets the ``401``.
* ``observe``: the console approves as before and records the level it saw.
* ``off``: the console approves as before.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from meho_backplane.auth.approval_sign_in import (
    LEVEL_UNCHECKED_REMEDIATION,
    WEB_LEVEL_MISSING_REMEDIATION,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.web_approval_sign_in import (
    check_web_approval_sign_in,
    is_web_approval_sign_in,
    web_approval_sign_in_ready,
)
from meho_backplane.settings import Settings, get_settings

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_SUB = "op-42"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    for name in (
        "FRESH_SIGN_IN_MAX_AGE_SECONDS",
        "PER_ACTION_SIGN_IN_MAX_AGE_SECONDS",
        "UI_KEYCLOAK_APPROVE_CLIENT_ID",
        "APPROVAL_ONE_TIME_CODE_ACR_VALUES",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


def _person(
    *,
    azp: str | None = "meho-web",
    acr: str | None = "1",
    age: int | None = 10,
    sub: str = _SUB,
    sid: str = "kc-sid-1",
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        azp=azp,
        acr=acr,
        auth_time=None if age is None else int(time.time()) - age,
        sid=sid,
    )


def _approval(*, acr: str | None = "meho-approve", **kwargs: object) -> Operator:
    return _person(azp="meho-web-approve", acr=acr, sid="kc-sid-2", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["off", "observe", "required"])
def test_an_approval_sign_in_with_the_level_approves_with_each_switch_value(mode: str) -> None:
    session = _person()
    approval = _approval()

    proof = check_web_approval_sign_in(session, approval, _settings(), level_in_force=mode)

    assert proof.channel == "web"
    assert proof.acr == "meho-approve"
    assert proof.auth_time == approval.auth_time
    assert proof.sid == "kc-sid-2"
    assert proof.level_missing is False
    assert proof.level_in_force == mode
    assert web_approval_sign_in_ready(session, approval, _settings(), level_in_force=mode)


def test_the_passkey_touch_level_counts_as_the_approval_level() -> None:
    proof = check_web_approval_sign_in(
        _person(), _approval(acr="meho-touch"), _settings(), level_in_force="required"
    )
    assert proof.acr == "meho-touch"


@pytest.mark.parametrize("age", [5, 1])
def test_required_refuses_a_normal_console_session_also_a_fresh_one(age: int) -> None:
    session = _person(age=age)

    with pytest.raises(HTTPException) as refused:
        check_web_approval_sign_in(session, None, _settings(), level_in_force="required")

    assert refused.value.status_code == 401
    assert str(refused.value.detail).startswith("fresh_sign_in_required: Sign in again to approve")
    assert not web_approval_sign_in_ready(session, None, _settings(), level_in_force="required")


@pytest.mark.parametrize("acr", ["1", None, "0"])
def test_required_refuses_an_approval_sign_in_without_the_level(acr: str | None) -> None:
    approval = _approval(acr=acr)

    with pytest.raises(HTTPException) as refused:
        check_web_approval_sign_in(_person(), approval, _settings(), level_in_force="required")

    assert refused.value.status_code == 401
    detail = str(refused.value.detail)
    if acr == "1":
        assert detail.endswith(WEB_LEVEL_MISSING_REMEDIATION)
        assert "approvals.web_sign_in" in detail
    else:
        assert WEB_LEVEL_MISSING_REMEDIATION not in detail


def test_required_names_a_failed_realm_check_when_only_the_level_is_missing() -> None:
    with pytest.raises(HTTPException) as refused:
        check_web_approval_sign_in(
            _person(),
            _approval(acr="1"),
            _settings(),
            level_in_force="required",
            level_check_failed=True,
        )
    assert str(refused.value.detail).endswith(LEVEL_UNCHECKED_REMEDIATION)


@pytest.mark.parametrize(
    "approval",
    [
        pytest.param(_approval(sub="someone-else"), id="another-person"),
        pytest.param(_person(azp="meho-cli-approve", acr="meho-approve"), id="cli-client"),
        pytest.param(_person(azp="meho-web", acr="meho-approve"), id="console-client"),
        pytest.param(_approval(age=2 * 3600), id="too-old"),
        pytest.param(_approval(age=None), id="no-auth-time"),
    ],
)
def test_required_refuses_a_token_that_is_not_this_persons_console_approval_sign_in(
    approval: Operator,
) -> None:
    with pytest.raises(HTTPException) as refused:
        check_web_approval_sign_in(_person(), approval, _settings(), level_in_force="required")
    assert refused.value.status_code == 401
    assert WEB_LEVEL_MISSING_REMEDIATION not in str(refused.value.detail)


@pytest.mark.parametrize("mode", ["auto", "", "something"])
def test_an_unknown_level_value_counts_as_required(mode: str) -> None:
    with pytest.raises(HTTPException):
        check_web_approval_sign_in(_person(), None, _settings(), level_in_force=mode)


def test_observe_approves_a_normal_session_as_before_and_records_the_missing_level() -> None:
    session = _person(age=30)

    with capture_logs() as logs:
        proof = check_web_approval_sign_in(session, None, _settings(), level_in_force="observe")

    assert proof.channel == "web"
    assert proof.acr == "1"
    assert proof.auth_time == session.auth_time
    assert proof.level_missing is True
    assert proof.level_in_force == "observe"
    assert proof.audit_payload(_SUB)["level_missing"] is True
    assert any(log["event"] == "approval_sign_in_level_missing" for log in logs)


def test_observe_records_the_missing_level_of_an_approval_sign_in_too() -> None:
    proof = check_web_approval_sign_in(
        _person(), _approval(acr="1"), _settings(), level_in_force="observe"
    )
    assert proof.acr == "1"
    assert proof.level_missing is True


def test_off_approves_a_normal_session_as_before_and_records_nothing_missing() -> None:
    with capture_logs() as logs:
        proof = check_web_approval_sign_in(_person(), None, _settings(), level_in_force="off")
    assert proof.level_missing is False
    assert proof.level_in_force == "off"
    assert not any(log["event"] == "approval_sign_in_level_missing" for log in logs)


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_observe_and_off_still_refuse_an_old_session_sign_in(mode: str) -> None:
    session = _person(age=10 * 24 * 3600)
    with pytest.raises(HTTPException) as refused:
        check_web_approval_sign_in(session, None, _settings(), level_in_force=mode)
    assert refused.value.status_code == 401
    assert not web_approval_sign_in_ready(session, None, _settings(), level_in_force=mode)


def test_an_empty_client_setting_means_no_token_is_a_console_approval_sign_in() -> None:
    settings = _settings(ui_keycloak_approve_client_id="")
    assert not is_web_approval_sign_in(_approval(), settings)
    with pytest.raises(HTTPException):
        check_web_approval_sign_in(_person(), _approval(), settings, level_in_force="required")
