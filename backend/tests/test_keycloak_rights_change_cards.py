# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval cards of Keycloak changes of rights: the object now and the new values.

``keycloak.client.update``, ``keycloak.realm.update`` and
``keycloak.protocol_mapper.create`` read the object when the request waits
and show it next to the new values. Secrets are hidden on both sides, and
a failed read says so instead of showing an empty "now" side.

The approval request a person reads always shows these cards. What the
caller gets back from ``preview_operation`` is behind the switch
``KEYCLOAK_RIGHTS_CHANGE_CARDS``. The tests turn it on, except the tests
that check a default install, with the switch off.

Fixture values are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import (  # loading the package registers the ops
    KeycloakConnector,
)
from meho_backplane.connectors.keycloak.ops_write_realm_smtp import _realm_update_preview
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    has_preview_builder,
)
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    required_settings_env,  # noqa: F401 -- autouse fixture: pins the env get_settings needs
)

_CLIENT_UUID = "33333333-3333-3333-3333-333333333333"
_CLIENT_SECRET = "made-up-client-secret-value-7f3a"
_SMTP_PASSWORD = "made-up-smtp-password-1c9e"


@pytest.fixture(autouse=True)
def _cards_switch(
    required_settings_env: None,  # noqa: F811 -- runs first, so get_settings has its env
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", raising=False)
    get_settings.cache_clear()


@dataclass
class _FakeDescriptor:
    op_id: str


@dataclass
class _FakeTarget:
    extras: dict[str, Any] = field(default_factory=lambda: {"managed_realm": "example"})


def _operator() -> Operator:
    return Operator(
        sub="person-requester",
        raw_jwt="<test-raw-jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000c0de"),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _connector(current: Any = None, *, error: Exception | None = None) -> KeycloakConnector:
    connector = KeycloakConnector()
    get = AsyncMock(side_effect=error) if error else AsyncMock(return_value=current)
    connector._get_admin_json = get  # type: ignore[method-assign]
    connector._find_client_uuid = AsyncMock(return_value=_CLIENT_UUID)  # type: ignore[method-assign]
    return connector


def _ctx(
    op_id: str, params: dict[str, Any], connector: Any, *, for_agent: bool = False
) -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_FakeTarget(),
        params=params,
        connector_id="keycloak-admin-26.x",
        agent_preview=for_agent,
    )


async def _card(
    op_id: str, params: dict[str, Any], connector: Any, *, for_agent: bool = False
) -> dict[str, Any]:
    effect = await build_proposed_effect(_ctx(op_id, params, connector, for_agent=for_agent))
    assert effect is not None
    card: dict[str, Any] = effect["preview"]
    return card


_CLIENT_NOW = {
    "id": _CLIENT_UUID,
    "clientId": "example-app",
    "fullScopeAllowed": False,
    "defaultClientScopes": ["basic"],
    "redirectUris": ["https://app.example.test/*"],
    "secret": _CLIENT_SECRET,
}


@pytest.mark.asyncio
async def test_client_update_shows_the_values_now_and_the_new_values() -> None:
    params = {
        "client_id": "example-app",
        "representation": {
            "fullScopeAllowed": True,
            "defaultClientScopes": ["basic", "tenant"],
            "serviceAccountsEnabled": True,
        },
    }

    card = await _card("keycloak.client.update", params, _connector(_CLIENT_NOW))

    assert card["id"] == _CLIENT_UUID
    assert card["current_values_available"] is True
    assert card["current_values"] == {
        "fullScopeAllowed": False,
        "defaultClientScopes": ["basic"],
        "serviceAccountsEnabled": None,
    }
    assert card["new_values"] == params["representation"]


@pytest.mark.asyncio
async def test_client_update_shows_only_the_sent_keys_of_a_map_field() -> None:
    client_now = {**_CLIENT_NOW, "attributes": {"pkce.code.challenge.method": "S256", "a": "1"}}
    params = {"id": _CLIENT_UUID, "representation": {"attributes": {"a": "2", "b": "3"}}}

    card = await _card("keycloak.client.update", params, _connector(client_now))

    assert card["current_values"] == {"attributes": {"a": "1", "b": None}}


@pytest.mark.asyncio
async def test_client_update_hides_the_client_secret_on_both_sides() -> None:
    params = {"id": _CLIENT_UUID, "representation": {"secret": "made-up-new-secret-0b2d"}}

    card = await _card("keycloak.client.update", params, _connector(_CLIENT_NOW))

    text = json.dumps(card)
    assert _CLIENT_SECRET not in text
    assert "made-up-new-secret-0b2d" not in text
    assert card["current_values_available"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connector",
    [None, "error", "unknown-client"],
    ids=["no-live-connection", "read-error", "unknown-client"],
)
async def test_client_update_says_when_the_client_cannot_be_read(connector: Any) -> None:
    if connector == "error":
        connector = _connector(error=RuntimeError("made-up read error"))
    elif connector == "unknown-client":
        connector = _connector(_CLIENT_NOW)
        connector._find_client_uuid = AsyncMock(return_value=None)
    params = {"client_id": "example-app", "representation": {"fullScopeAllowed": True}}

    card = await _card("keycloak.client.update", params, connector)

    assert card["current_values_available"] is False
    assert "current_values" not in card
    assert card["new_values"] == {"fullScopeAllowed": True}


@pytest.mark.asyncio
async def test_realm_update_shows_the_values_now_and_hides_the_smtp_password() -> None:
    realm_now = {
        "realm": "example",
        "registrationAllowed": False,
        "smtpServer": {"host": "mail.example.test", "password": _SMTP_PASSWORD},
    }
    params = {
        "representation": {
            "registrationAllowed": True,
            "smtpServer": {"host": "smtp.example.test"},
        },
        "smtp_password_secret_ref": "example/keycloak/smtp",
    }

    card = await _card("keycloak.realm.update", params, _connector(realm_now))

    assert card["realm"] == "example"
    assert card["current_values_available"] is True
    assert card["current_values"]["registrationAllowed"] is False
    assert card["current_values"]["smtpServer"] == {"host": "mail.example.test"}
    assert card["representation"]["registrationAllowed"] is True
    assert card["smtp_password_secret_ref"] == "example/keycloak/smtp"
    assert _SMTP_PASSWORD not in json.dumps(card)


@pytest.mark.asyncio
async def test_realm_update_says_when_the_realm_cannot_be_read() -> None:
    params = {"representation": {"registrationAllowed": True}}

    card = await _card(
        "keycloak.realm.update", params, _connector(error=RuntimeError("made-up read error"))
    )

    assert card["current_values_available"] is False
    assert card["representation"] == {"registrationAllowed": True}


@pytest.mark.asyncio
async def test_protocol_mapper_create_shows_the_mappers_now_and_the_new_one() -> None:
    client_now = {
        **_CLIENT_NOW,
        "protocolMappers": [
            {
                "name": "tenant",
                "protocolMapper": "oidc-usermodel-attribute-mapper",
                "config": {"claim.name": "tenant_id", "client.secret": _CLIENT_SECRET},
            }
        ],
    }
    new_mapper = {
        "name": "role",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {"claim.name": "tenant_role", "claim.value": "admin"},
    }

    card = await _card(
        "keycloak.protocol_mapper.create",
        {"client_id": "example-app", "representation": new_mapper},
        _connector(client_now),
    )

    assert card["id"] == _CLIENT_UUID
    assert card["new_mapper"] == new_mapper
    assert card["current_mappers_available"] is True
    assert [m["name"] for m in card["current_mappers"]] == ["tenant"]
    assert card["current_mappers"][0]["config"]["claim.name"] == "tenant_id"
    assert _CLIENT_SECRET not in json.dumps(card)


@pytest.mark.asyncio
async def test_protocol_mapper_create_on_a_client_without_mappers_shows_an_empty_list() -> None:
    card = await _card(
        "keycloak.protocol_mapper.create",
        {"id": _CLIENT_UUID, "representation": {"name": "role"}},
        _connector({"id": _CLIENT_UUID, "clientId": "example-app"}),
    )

    assert card["current_mappers_available"] is True
    assert card["current_mappers"] == []


@pytest.mark.asyncio
async def test_protocol_mapper_create_says_when_the_client_cannot_be_read() -> None:
    card = await _card(
        "keycloak.protocol_mapper.create",
        {"id": _CLIENT_UUID, "representation": {"name": "role"}},
        _connector(error=RuntimeError("made-up read error")),
    )

    assert card["current_mappers_available"] is False
    assert "current_mappers" not in card
    assert card["new_mapper"] == {"name": "role"}


def test_the_switch_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)

    assert get_settings().keycloak_rights_change_cards is False


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["keycloak.client.update", "keycloak.protocol_mapper.create"])
async def test_switch_off_the_agent_preview_of_client_update_and_mapper_create_echoes_the_params(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_off(monkeypatch)
    connector = _connector(_CLIENT_NOW)
    params = {"client_id": "example-app", "representation": {"name": "example"}}

    effect = await build_proposed_effect(_ctx(op_id, params, connector, for_agent=True))

    assert not has_preview_builder(op_id)
    assert effect is not None
    assert "preview" not in effect
    assert effect["params_echo"]["representation"] == {"name": "example"}
    connector._get_admin_json.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_switch_off_the_agent_preview_of_realm_update_has_no_values_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    connector = _connector({"realm": "example", "registrationAllowed": False})
    params = {"representation": {"registrationAllowed": True}}
    ctx = _ctx("keycloak.realm.update", params, connector, for_agent=True)

    card = await _card("keycloak.realm.update", params, connector, for_agent=True)

    assert card == await _realm_update_preview(ctx)
    assert "current_values" not in card
    assert "current_values_available" not in card
    connector._get_admin_json.assert_not_awaited()  # type: ignore[attr-defined]


# -- a default install: the person who approves always sees the values -------


@pytest.mark.asyncio
async def test_default_install_client_update_card_shows_the_values_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    params = {"id": _CLIENT_UUID, "representation": {"fullScopeAllowed": True}}

    card = await _card("keycloak.client.update", params, _connector(_CLIENT_NOW))

    assert card["current_values"] == {"fullScopeAllowed": False}
    assert card["new_values"] == {"fullScopeAllowed": True}


@pytest.mark.asyncio
async def test_default_install_realm_update_card_shows_the_values_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    connector = _connector({"realm": "example", "registrationAllowed": False})
    params = {"representation": {"registrationAllowed": True}}

    card = await _card("keycloak.realm.update", params, connector)

    assert card["current_values"] == {"registrationAllowed": False}


@pytest.mark.asyncio
async def test_default_install_protocol_mapper_create_card_shows_the_mappers_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    client = {**_CLIENT_NOW, "protocolMappers": [{"name": "groups", "protocolMapper": "x"}]}
    params = {"id": _CLIENT_UUID, "representation": {"name": "role"}}

    card = await _card("keycloak.protocol_mapper.create", params, _connector(client))

    assert [m["name"] for m in card["current_mappers"]] == ["groups"]
    assert card["new_mapper"] == {"name": "role"}


@pytest.mark.asyncio
async def test_default_install_card_hides_a_client_attribute_with_a_secret_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_off(monkeypatch)
    attribute_now = "made-up-rotated-secret-old-4a1e"
    attribute_new = "made-up-rotated-secret-new-9c2f"
    client = {**_CLIENT_NOW, "attributes": {"client.secret.rotated": attribute_now}}
    params = {
        "id": _CLIENT_UUID,
        "representation": {"attributes": {"client.secret.rotated": attribute_new}},
    }

    card = await _card("keycloak.client.update", params, _connector(client))

    text = json.dumps(card)
    assert attribute_now not in text
    assert attribute_new not in text
    assert card["current_values_available"] is True
