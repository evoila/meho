# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval level of an approval sign-in (#6281).

An approve on the command line or REST needs a token of the approval client
with a recent ``auth_time``. ``APPROVAL_SIGN_IN_LEVEL`` adds a check of the
token's ``acr``: only the approval sign-in flow gives ``meho-approve``, and
the higher passkey touch level ``meho-touch`` counts too.

* ``required``: any other ``acr`` gets the same ``401`` as an old sign-in;
* ``observe`` (the default): the approve goes through, MEHO logs
  ``approval_sign_in_level_missing`` and the audit row says so;
* ``off``: the checks as before.

The requests go through the real REST route with signed tokens.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.settings import Settings, get_settings

from .test_api_v1_approvals_fresh_sign_in import (
    _approval_auth,
    _approve,
    _assert_challenge,
    _client,
    _fake_resume,
    _park,
    _settings_env,
    _status,
    _token,
)

__all__ = ["_client", "_fake_resume", "_settings_env"]

_MODES = ["off", "observe", "required"]


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str | None) -> None:
    if mode is None:
        monkeypatch.delenv("APPROVAL_SIGN_IN_LEVEL", raising=False)
    else:
        monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", mode)
    get_settings.cache_clear()


def _fresh(key: Any, **claims: Any) -> str:
    return _token(key, auth_time=int(time.time()) - 5, sid=f"s-{uuid.uuid4()}", **claims)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("acr", ["meho-approve", "meho-touch"])
async def test_a_sign_in_with_the_approval_level_approves_with_each_switch_value(
    mode: str,
    acr: str,
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    _set_mode(monkeypatch, mode)
    ac, key = _client
    request_id = await _park()

    with capture_logs() as logs:
        response = await _approve(ac, "approve", request_id, _fresh(key, acr=acr))

    assert response.status_code == 200, response.text
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    recorded = await _approval_auth(request_id)
    assert recorded["acr"] == acr
    assert "level_missing" not in recorded
    assert not [e for e in logs if e["event"] == "approval_sign_in_level_missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
@pytest.mark.parametrize(
    "claims",
    [
        {"acr": "1"},
        {"acr": None},
        {"acr": "2"},
        {"acr": "meho-approve", "azp": "meho-cli"},
    ],
    ids=["normal sign-in", "no acr", "level without its name", "another client"],
)
async def test_required_refuses_a_token_without_the_approval_level(
    route: str,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """Also a token of an approval client that is not bound to the approval flow.

    Such a client signs in through the realm's normal login flow, so its
    token carries ``acr`` ``"1"``: the first case.
    """
    _set_mode(monkeypatch, "required")
    ac, key = _client
    request_id = await _park()

    with capture_logs() as logs:
        response = await _approve(ac, route, request_id, _fresh(key, **claims))

    _assert_challenge(response)
    assert response.json()["detail"].startswith("fresh_sign_in_required:")
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []
    refused = next(e for e in logs if e["event"] == "approval_sign_in_not_fresh")
    assert refused["approval_level"] is (claims["acr"] == "meho-approve")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["observe", None], ids=["observe", "default"])
async def test_observe_logs_and_records_the_missing_level_and_approves(
    mode: str | None,
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    _set_mode(monkeypatch, mode)
    ac, key = _client
    request_id = await _park()

    with capture_logs() as logs:
        response = await _approve(ac, "approve", request_id, _fresh(key, acr="1"))

    assert response.status_code == 200, response.text
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    recorded = await _approval_auth(request_id)
    assert recorded["acr"] == "1"
    assert recorded["level_missing"] is True
    missing = [e for e in logs if e["event"] == "approval_sign_in_level_missing"]
    assert len(missing) == 1
    assert missing[0]["acr"] == "1"


@pytest.mark.asyncio
async def test_off_approves_as_before(
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    _set_mode(monkeypatch, "off")
    ac, key = _client
    request_id = await _park()

    with capture_logs() as logs:
        response = await _approve(ac, "approve", request_id, _fresh(key, acr="1"))

    assert response.status_code == 200, response.text
    assert "level_missing" not in await _approval_auth(request_id)
    assert not [e for e in logs if e["event"] == "approval_sign_in_level_missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
async def test_the_level_never_replaces_the_other_checks(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: list[uuid.UUID],
) -> None:
    """A token with the level but an old sign-in, or of another client, is refused."""
    _set_mode(monkeypatch, mode)
    ac, key = _client
    request_id = await _park()
    old = _token(key, auth_time=int(time.time()) - 901, sid="s-old", acr="meho-approve")
    other = _fresh(key, acr="meho-approve", azp="meho-cli")

    _assert_challenge(await _approve(ac, "approve", request_id, old))
    _assert_challenge(await _approve(ac, "approve", request_id, other))
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


def test_the_setting_defaults_to_observe_and_refuses_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, None)
    assert get_settings().approval_sign_in_level == "observe"
    _set_mode(monkeypatch, " Required ")
    assert get_settings().approval_sign_in_level == "required"
    with pytest.raises(ValueError, match="APPROVAL_SIGN_IN_LEVEL must be one of"):
        Settings(
            keycloak_issuer_url="https://kc.test/realms/meho",
            keycloak_audience="meho-backplane",
            vault_addr="https://vault.test",
            approval_sign_in_level="on",
        )
