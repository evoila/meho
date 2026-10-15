# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-merge-write for ``keycloak.client.update`` / ``keycloak.realm.update`` (#3941).

Keycloak's client and realm ``PUT`` writes its own defaults for some
fields that are missing from the body. The update ops therefore read the
object, put the caller's fields on top, PUT the full object back, and
read it again to report any field that changed without being asked for.

These tests drive both ops through the ``dispatch`` stack against a
respx-mocked Keycloak Admin REST API, with recorded before/after
representations:

* ``{"enabled": false}`` leaves ``frontchannelLogout``, ``webOrigins``,
  ``redirectUris`` and every protocol-mapper config unchanged in the PUT
  body, and the read-back finds no unrequested change.
* When the read-back shows a field the caller did not send has changed,
  the result names it in ``unrequested_changes``.
* A failed read-back does not fail the (already done) write.
* ``realm.update`` keeps realm settings and merges ``attributes`` key by
  key.
* When the caller sends part of a map and the read-back no longer has that
  map, every key the caller did not send is reported as gone. A missing
  key is not the same as a key holding ``null``.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.http import ResponseTooLargeError
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.connector import KeycloakAdminTokenError
from meho_backplane.connectors.keycloak.ops_write_merge import (
    merge_representation,
    read_merge_write,
    unrequested_changes,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "keycloak-update-merge"
_KC_HOST = "keycloak-update-merge.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_CLIENT_UUID = "12121212-1212-1212-1212-121212121212"
_CLIENT_PATH = f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}"
_REALM_PATH = f"/admin/realms/{_REALM}"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000039a1")
_OPERATOR = Operator(
    sub="keycloak-update-merge-test",
    name="Keycloak Update Merge Test Operator",
    email=None,
    raw_jwt="<keycloak-update-merge-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

#: Recorded client representation before the update. The fields named in
#: the bug report (frontchannelLogout, webOrigins, the groups mapper's
#: ``multivalued``) are set to the values Keycloak overwrote.
_CLIENT_BEFORE: dict[str, Any] = {
    "id": _CLIENT_UUID,
    "clientId": "meho-web",
    "enabled": True,
    "publicClient": True,
    "frontchannelLogout": False,
    "authorizationServicesEnabled": False,
    "redirectUris": ["http://localhost:5173/*", "http://127.0.0.1:5173/*"],
    "webOrigins": [],
    "attributes": {"post.logout.redirect.uris": "+", "pkce.code.challenge.method": "S256"},
    "protocolMappers": [
        {
            "id": "mapper-groups",
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {"claim.name": "groups", "multivalued": "true", "full.path": "false"},
        },
        {
            "id": "mapper-tenant",
            "name": "tenant_id",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {"claim.name": "tenant_id", "user.attribute": "tenant_id"},
        },
    ],
}

#: Recorded realm representation before the update. The device-code and
#: CIBA settings are the ones Keycloak resets when they are missing.
_REALM_BEFORE: dict[str, Any] = {
    "id": "realm-uuid-1",
    "realm": _REALM,
    "enabled": True,
    "displayName": "MEHO",
    "oauth2DeviceCodeLifespan": 900,
    "oauth2DevicePollingInterval": 10,
    "attributes": {"cibaExpiresIn": "300", "parRequestUriLifespan": "120"},
}


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token-update-merge", "expires_in": 300}
    )


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


async def _seed_keycloak_target() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            TargetORM(
                tenant_id=_OPERATOR_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_connectors_keycloak_update_merge",
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def keycloak_connector() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    await _seed_keycloak_target()
    instance = KeycloakConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    yield instance
    await instance.aclose()


async def _dispatch(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an approved write (``_approved=True`` skips the approval wait)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved_target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=resolved_target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _reads(*bodies: dict[str, Any] | int) -> list[httpx.Response]:
    """GET responses in call order: a dict is a 200 body, an int an error status."""
    return [
        httpx.Response(body) if isinstance(body, int) else httpx.Response(200, json=body)
        for body in bodies
    ]


# ---------------------------------------------------------------------------
# client.update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_update_enabled_false_keeps_every_other_field(
    keycloak_connector: KeycloakConnector,
) -> None:
    """Only ``enabled`` changes; the PUT carries the full client as it was."""
    after = {**copy.deepcopy(_CLIENT_BEFORE), "enabled": False}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        get_route = mock.get(_CLIENT_PATH).mock(side_effect=_reads(_CLIENT_BEFORE, after))
        put_route = mock.put(_CLIENT_PATH).respond(204)
        result = await _dispatch(
            "keycloak.client.update", {"id": _CLIENT_UUID, "representation": {"enabled": False}}
        )

    assert result["status"] == "ok", result.get("error")
    assert get_route.call_count == 2, "should read before the write and read back after it"
    body = json.loads(put_route.calls.last.request.content)
    assert body["enabled"] is False
    assert body["frontchannelLogout"] is False
    assert body["webOrigins"] == []
    assert body["redirectUris"] == _CLIENT_BEFORE["redirectUris"]
    assert body["protocolMappers"] == _CLIENT_BEFORE["protocolMappers"]
    assert body["authorizationServicesEnabled"] is False
    assert body == {**_CLIENT_BEFORE, "enabled": False}

    out = result["result"]
    assert out["updated"] is True
    assert out["changed_fields"] == ["enabled"]
    assert out["read_back_checked"] is True
    assert out["unrequested_changes"] == []


@pytest.mark.asyncio
async def test_client_update_reports_unrequested_changes(
    keycloak_connector: KeycloakConnector,
) -> None:
    """A read-back that shows other fields changed names them in the result."""
    after = copy.deepcopy(_CLIENT_BEFORE)
    after["enabled"] = False
    after["frontchannelLogout"] = True
    after["webOrigins"] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    del after["protocolMappers"][0]["config"]["multivalued"]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CLIENT_PATH).mock(side_effect=_reads(_CLIENT_BEFORE, after))
        mock.put(_CLIENT_PATH).respond(204)
        result = await _dispatch(
            "keycloak.client.update", {"id": _CLIENT_UUID, "representation": {"enabled": False}}
        )

    assert result["status"] == "ok", result.get("error")
    out = result["result"]
    assert out["read_back_checked"] is True
    assert out["unrequested_changes"] == ["frontchannelLogout", "protocolMappers", "webOrigins"]


@pytest.mark.asyncio
async def test_client_update_read_back_failure_keeps_success(
    keycloak_connector: KeycloakConnector,
) -> None:
    """The write already happened, so a failed read-back is reported, not raised."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CLIENT_PATH).mock(side_effect=_reads(_CLIENT_BEFORE, 404))
        put_route = mock.put(_CLIENT_PATH).respond(204)
        result = await _dispatch(
            "keycloak.client.update", {"id": _CLIENT_UUID, "representation": {"enabled": False}}
        )

    assert result["status"] == "ok", result.get("error")
    assert put_route.called
    assert result["result"]["read_back_checked"] is False
    assert result["result"]["unrequested_changes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read_back",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json=[{"id": _CLIENT_UUID}]),
    ],
    ids=["not-json", "not-an-object"],
)
async def test_client_update_bad_read_back_body_keeps_success(
    keycloak_connector: KeycloakConnector, read_back: httpx.Response
) -> None:
    """A read-back body that is not JSON, or not an object, is reported, not raised."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CLIENT_PATH).mock(
            side_effect=[httpx.Response(200, json=_CLIENT_BEFORE), read_back]
        )
        put_route = mock.put(_CLIENT_PATH).respond(204)
        result = await _dispatch(
            "keycloak.client.update", {"id": _CLIENT_UUID, "representation": {"enabled": False}}
        )

    assert result["status"] == "ok", result.get("error")
    assert put_route.called
    assert result["result"]["read_back_checked"] is False
    assert result["result"]["unrequested_changes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        OSError("connection reset"),
        ValueError("bad body"),
        KeycloakAdminTokenError("token mint failed"),
        CredentialsReadError("credential store unavailable"),
        ResponseTooLargeError("too large"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_read_merge_write_read_back_errors_keep_success(error: Exception) -> None:
    """Every error the read-back can raise leaves the finished write as a success."""
    connector = AsyncMock()
    connector._get_admin_json.side_effect = [copy.deepcopy(_CLIENT_BEFORE), error]

    out = await read_merge_write(
        connector,
        _OPERATOR,
        AsyncMock(),
        path=_CLIENT_PATH,
        requested={"enabled": False},
    )

    connector._write_admin.assert_awaited_once()
    assert out == {
        "changed_fields": ["enabled"],
        "unrequested_changes": [],
        "read_back_checked": False,
    }


# ---------------------------------------------------------------------------
# realm.update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_realm_update_keeps_settings_and_merges_attributes(
    keycloak_connector: KeycloakConnector,
) -> None:
    """Device-code and CIBA settings survive; one sent attribute keeps the others."""
    requested = {"displayName": "MEHO lab", "attributes": {"frontendUrl": "https://kc.test"}}
    after = merge_representation(copy.deepcopy(_REALM_BEFORE), requested)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(side_effect=_reads(_REALM_BEFORE, after))
        put_route = mock.put(_REALM_PATH).respond(204)
        result = await _dispatch("keycloak.realm.update", {"representation": requested})

    assert result["status"] == "ok", result.get("error")
    body = json.loads(put_route.calls.last.request.content)
    assert body["realm"] == _REALM
    assert body["displayName"] == "MEHO lab"
    assert body["oauth2DeviceCodeLifespan"] == 900
    assert body["oauth2DevicePollingInterval"] == 10
    assert body["attributes"] == {
        "cibaExpiresIn": "300",
        "parRequestUriLifespan": "120",
        "frontendUrl": "https://kc.test",
    }
    out = result["result"]
    assert out["realm"] == _REALM
    assert out["changed_fields"] == ["attributes", "displayName"]
    assert out["read_back_checked"] is True
    assert out["unrequested_changes"] == []


@pytest.mark.asyncio
async def test_realm_update_reports_reset_attribute(
    keycloak_connector: KeycloakConnector,
) -> None:
    """An attribute the caller did not send that changed is named ``attributes.<key>``."""
    requested = {"attributes": {"frontendUrl": "https://kc.test"}}
    after = merge_representation(copy.deepcopy(_REALM_BEFORE), requested)
    after["attributes"]["cibaExpiresIn"] = "120"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(side_effect=_reads(_REALM_BEFORE, after))
        mock.put(_REALM_PATH).respond(204)
        result = await _dispatch("keycloak.realm.update", {"representation": requested})

    assert result["status"] == "ok", result.get("error")
    assert result["result"]["unrequested_changes"] == ["attributes.cibaExpiresIn"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", [None, "missing"], ids=["null", "missing"])
async def test_realm_update_reports_attributes_lost_after_write(
    keycloak_connector: KeycloakConnector, gone: Any
) -> None:
    """A sent map that is gone after the write reports every key the caller did not send."""
    requested = {"attributes": {"frontendUrl": "https://kc.test"}}
    after = merge_representation(copy.deepcopy(_REALM_BEFORE), requested)
    if gone is None:
        after["attributes"] = None
    else:
        del after["attributes"]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(side_effect=_reads(_REALM_BEFORE, after))
        mock.put(_REALM_PATH).respond(204)
        result = await _dispatch("keycloak.realm.update", {"representation": requested})

    assert result["status"] == "ok", result.get("error")
    assert result["result"]["read_back_checked"] is True
    assert result["result"]["unrequested_changes"] == [
        "attributes.cibaExpiresIn",
        "attributes.parRequestUriLifespan",
    ]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_merge_replaces_lists_and_merges_maps() -> None:
    current = {"a": 1, "uris": ["x", "y"], "attributes": {"k1": "v1", "k2": "v2"}}
    merged = merge_representation(current, {"uris": ["z"], "attributes": {"k2": "new"}})
    assert merged == {"a": 1, "uris": ["z"], "attributes": {"k1": "v1", "k2": "new"}}
    assert current["attributes"] == {"k1": "v1", "k2": "v2"}, "input must not be mutated"


def test_unrequested_changes_ignores_list_order() -> None:
    before = {"webOrigins": ["a", "b"], "protocolMappers": [{"id": "1"}, {"id": "2"}]}
    after = {"webOrigins": ["b", "a"], "protocolMappers": [{"id": "2"}, {"id": "1"}]}
    assert unrequested_changes(before, after, {}) == []


def test_unrequested_changes_names_added_and_removed_fields() -> None:
    before = {"enabled": True, "old": 1}
    after = {"enabled": False, "new": 2}
    assert unrequested_changes(before, after, {"enabled": False}) == ["new", "old"]


@pytest.mark.parametrize(
    "after",
    [
        {"enabled": False},
        {"enabled": False, "attributes": None},
        {"enabled": False, "attributes": "x"},
    ],
    ids=["missing", "null", "not-a-map"],
)
def test_unrequested_changes_reports_untouched_keys_when_map_is_gone(after: dict[str, Any]) -> None:
    """Part of a map was sent; the map is gone after the write: the untouched keys are named."""
    before = {"enabled": True, "attributes": {"sent": "1", "kept": "2", "other": None}}
    requested = {"enabled": False, "attributes": {"sent": "1"}}
    assert unrequested_changes(before, after, requested) == ["attributes.kept", "attributes.other"]


def test_unrequested_changes_does_not_report_a_map_that_had_only_sent_keys() -> None:
    before = {"attributes": {"sent": "1"}}
    assert unrequested_changes(before, {}, {"attributes": {"sent": "1"}}) == []


def test_unrequested_changes_tells_a_missing_key_from_null() -> None:
    assert unrequested_changes({"a": None}, {}, {}) == ["a"]
    assert unrequested_changes({}, {"a": None}, {}) == ["a"]
    assert unrequested_changes({"a": None}, {"a": None}, {}) == []
    before = {"attributes": {"sent": "1", "k": None}}
    after = {"attributes": {"sent": "1"}}
    assert unrequested_changes(before, after, {"attributes": {"sent": "1"}}) == ["attributes.k"]
    assert unrequested_changes(after, before, {"attributes": {"sent": "1"}}) == ["attributes.k"]
