# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A role given in MEHO's own login realm is judged by what it really gives.

* The approval card lists the parts of the role, on every level, and marks
  the ``realm-management`` roles and the roles MEHO's login maps to platform
  admin rights.
* In the login realm, a role with such a part needs a platform admin, also
  when the role itself has a harmless name.
* A plain role keeps the usual rule, and so does a realm outside the login.
* A list that cannot be read counts as the strict case.
* When the parts of the role change before the run, the run is refused.
* A request that waited from before MEHO read the parts is refused, in every
  realm.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write_effective_roles import (
    DEPTH_CAP,
    ROLE_PARTS_CHANGED,
    ROLE_PARTS_NOT_READ,
)
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations.approval_queue import ApprovalRoleFloorError
from meho_backplane.operations.grant_approval import (
    GRANT_REASON_STRICTER,
    grant_changed_since_request,
)
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import (
    _LOGIN_REALMS,
    _OTHER_USER,
    _REALM,
    _approve,
    _ctx,
    _park,
    _person,
    _row,
    _status,
)

_REALM_ROLE = "keycloak.role_mapping.assign"
_CLIENT_ROLE = "keycloak.role_mapping.client.assign"
_EFFECTIVE = "meho_backplane.connectors.keycloak.ops_write_effective_roles"
_MGMT_UUID = "c0000000-0000-0000-0000-00000000000a"
_APP_UUID = "c0000000-0000-0000-0000-00000000000b"


def _role(
    role_id: str, name: str, *, composite: bool = False, client: str | None = None
) -> dict[str, Any]:
    row: dict[str, Any] = {"id": role_id, "name": name, "composite": composite}
    if client is not None:
        row.update(clientRole=True, containerId=client)
    return row


# An outer realm role "reporting" holds a role "audit", which holds the
# realm-management role "realm-admin". "viewer" holds nothing.
_ROLES: dict[str, dict[str, Any]] = {
    "reporting": _role("r-1", "reporting", composite=True),
    "audit": _role("r-2", "audit", composite=True),
    "viewer": _role("r-3", "viewer"),
    "admin-realm": _role("r-4", "admin-realm", composite=True),
    "platform_admin": _role("r-5", "platform_admin"),
}
_PARTS: dict[str, list[dict[str, Any]]] = {
    "r-1": [_ROLES["audit"], _ROLES["viewer"]],
    "r-2": [_role("m-1", "realm-admin", client=_MGMT_UUID)],
    "r-4": [_ROLES["platform_admin"]],
}
_CLIENTS = {"realm-management": _MGMT_UUID, "app": _APP_UUID}
_CLIENT_ROLES = {
    _APP_UUID: {
        "editor": _role("a-1", "editor", client=_APP_UUID),
        "platform_admin": _role("a-2", "platform_admin", client=_APP_UUID),
    }
}


def _connector(
    parts: dict[str, list[dict[str, Any]]] | None = None, *, fails: bool = False
) -> tuple[KeycloakConnector, list[str]]:
    connector = KeycloakConnector()
    reads: list[str] = []
    table = _PARTS if parts is None else parts

    async def _find_role(_t: Any, _r: str, name: str, *, operator: Any) -> dict[str, Any] | None:
        reads.append(name)
        return _ROLES.get(name)

    async def _find_client(_t: Any, _r: str, client_id: str, *, operator: Any) -> str | None:
        return _CLIENTS.get(client_id)

    async def _get_json(_t: Any, path: str, *, operator: Any) -> dict[str, Any]:
        segments = path.split("/")
        if "roles" in segments:  # .../clients/{uuid}/roles/{name}
            return _CLIENT_ROLES[segments[-3]][segments[-1]]
        names = {uuid: name for name, uuid in _CLIENTS.items()}
        return {"clientId": names[segments[-1]]}

    async def _get_list(
        _t: Any, path: str, *, operator: Any, params: Any = None, strict: bool = False
    ) -> list[Any]:
        if fails:
            raise RuntimeError("read failed")
        assert strict
        return table[path.split("/")[-2]]

    connector._find_realm_role = _find_role  # type: ignore[method-assign]
    connector._find_client_uuid = _find_client  # type: ignore[method-assign]
    connector._get_admin_json = _get_json  # type: ignore[method-assign]
    connector._get_admin_list = _get_list  # type: ignore[method-assign]
    return connector, reads


async def _card(ctx: Any) -> dict[str, Any]:
    from meho_backplane.operations._preview import build_proposed_effect

    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect["preview"]


def _realm_ctx(role: str, connector: KeycloakConnector, realm: str = _REALM) -> Any:
    return _ctx(_REALM_ROLE, {"id": _OTHER_USER, "roles": [role]}, realm=realm, connector=connector)


@pytest.fixture(autouse=True)
def _login_realm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    monkeypatch.setattr(f"{_EFFECTIVE}.get_settings", lambda: _Settings(None))


class _Settings:
    def __init__(self, platform_role: str | None) -> None:
        self.jwt_platform_admin_role_name = platform_role


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_composite_role_lists_its_parts_and_marks_the_realm_admin_role() -> None:
    connector, _ = _connector()

    card = await _card(_realm_ctx("reporting", connector))

    by_name = {r["name"]: r for r in card["effective_roles"]}
    assert set(by_name) == {"reporting", "audit", "viewer", "realm-admin"}
    assert by_name["realm-admin"]["client_id"] == "realm-management"
    assert by_name["realm-admin"]["realm_management"] is True
    assert by_name["viewer"]["realm_management"] is False
    assert card["grant"]["platform_admin"] is True


@pytest.mark.asyncio
async def test_a_plain_role_keeps_the_usual_rule() -> None:
    connector, _ = _connector()

    card = await _card(_realm_ctx("viewer", connector))

    assert [r["name"] for r in card["effective_roles"]] == ["viewer"]
    assert card["grant"]["platform_admin"] is False


@pytest.mark.asyncio
async def test_a_part_that_the_login_maps_to_platform_rights_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_EFFECTIVE}.get_settings", lambda: _Settings("platform_admin"))
    connector, _ = _connector()

    card = await _card(_realm_ctx("admin-realm", connector))

    marked = {r["name"] for r in card["effective_roles"] if r["login_role"]}
    assert marked == {"platform_admin"}
    assert card["grant"]["platform_admin"] is True


@pytest.mark.asyncio
async def test_the_configured_platform_role_name_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_EFFECTIVE}.get_settings", lambda: _Settings("viewer"))
    connector, _ = _connector()

    card = await _card(_realm_ctx("reporting", connector))

    assert {r["name"] for r in card["effective_roles"] if r["login_role"]} == {"viewer"}
    assert card["grant"]["platform_admin"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, "VIEWER", "Platform_Admin"])
async def test_only_the_exact_configured_role_name_is_marked(
    monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    # The login matches only JWT_PLATFORM_ADMIN_ROLE_NAME, by exact name.
    monkeypatch.setattr(f"{_EFFECTIVE}.get_settings", lambda: _Settings(configured))
    connector, _ = _connector({"r-1": [_ROLES["viewer"]], "r-4": [_ROLES["platform_admin"]]})

    for role in ("reporting", "admin-realm"):
        card = await _card(_realm_ctx(role, connector))

        assert not any(r["login_role"] for r in card["effective_roles"])
        assert card["grant"]["platform_admin"] is False


@pytest.mark.asyncio
async def test_a_role_named_like_a_platform_role_without_rights_needs_no_platform_admin() -> None:
    connector, _ = _connector()

    card = await _card(_realm_ctx("platform_admin", connector))

    assert [r["name"] for r in card["effective_roles"]] == ["platform_admin"]
    assert card["grant"]["platform_admin"] is False


@pytest.mark.asyncio
async def test_a_client_role_named_like_a_platform_role_needs_no_platform_admin() -> None:
    connector, _ = _connector()
    ctx = _ctx(
        _CLIENT_ROLE,
        {"id": _OTHER_USER, "client_id": "app", "roles": ["platform_admin"]},
        realm=_REALM,
        connector=connector,
    )

    card = await _card(ctx)

    assert card["effective_roles"][0]["login_role"] is False
    assert card["grant"]["platform_admin"] is False


@pytest.mark.asyncio
async def test_a_client_role_is_expanded_too() -> None:
    connector, _ = _connector()
    ctx = _ctx(
        _CLIENT_ROLE,
        {"id": _OTHER_USER, "client_id": "app", "roles": ["editor"]},
        realm=_REALM,
        connector=connector,
    )

    card = await _card(ctx)

    assert [(r["client_id"], r["name"]) for r in card["effective_roles"]] == [("app", "editor")]
    assert card["grant"]["platform_admin"] is False


@pytest.mark.asyncio
async def test_a_realm_outside_the_login_is_not_read() -> None:
    connector, reads = _connector()

    card = await _card(_realm_ctx("reporting", connector, realm="customer-realm"))

    assert card["effective_roles"] == []
    assert card["grant"]["platform_admin"] is False
    assert card["grant"]["login_realm"] is False
    assert reads == []


@pytest.mark.asyncio
async def test_roles_that_cannot_be_read_are_unknown_and_strict() -> None:
    connector, _ = _connector(fails=True)

    card = await _card(_realm_ctx("reporting", connector))

    assert card["effective_roles"] is None
    assert card["grant"]["platform_admin"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["no-such-role"])
async def test_a_role_that_does_not_exist_is_unknown_and_strict(role: str) -> None:
    connector, _ = _connector()

    card = await _card(_realm_ctx(role, connector))

    assert card["effective_roles"] is None
    assert card["grant"]["platform_admin"] is None


@pytest.mark.asyncio
async def test_a_part_that_is_not_an_object_is_unknown() -> None:
    connector, _ = _connector({"r-1": ["audit"]})  # type: ignore[list-item]

    card = await _card(_realm_ctx("reporting", connector))

    assert card["effective_roles"] is None


@pytest.mark.asyncio
async def test_a_loop_of_roles_ends_and_lists_each_role_once() -> None:
    looped = {"r-1": [_ROLES["audit"]], "r-2": [_ROLES["reporting"]]}
    connector, _ = _connector(looped)

    card = await _card(_realm_ctx("reporting", connector))

    assert sorted(r["name"] for r in card["effective_roles"]) == ["audit", "reporting"]


@pytest.mark.asyncio
async def test_a_nesting_deeper_than_the_cap_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    chain: dict[str, dict[str, Any]] = {
        f"n{i}": _role(f"n-{i}", f"n{i}", composite=True) for i in range(DEPTH_CAP + 3)
    }
    parts = {f"n-{i}": [chain[f"n{i + 1}"]] for i in range(DEPTH_CAP + 2)}
    _ROLES.update(chain)
    try:
        connector, _ = _connector(parts)
        card = await _card(_realm_ctx("n0", connector))
    finally:
        for name in chain:
            _ROLES.pop(name)

    assert card["effective_roles"] is None


# ---------------------------------------------------------------------------
# Who may approve
# ---------------------------------------------------------------------------


async def _parked_card(role: str) -> tuple[dict[str, Any], dict[str, Any]]:
    connector, _ = _connector()
    ctx = _realm_ctx(role, connector)
    from meho_backplane.operations._preview import build_proposed_effect

    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return ctx.params, effect


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_approve_a_composite_role_that_holds_realm_admin() -> None:
    params, effect = await _parked_card("reporting")
    request_id = await _park(params, effect, op_id=_REALM_ROLE)

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-admin"), params)
    assert "platform admin" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value

    await _approve(request_id, _person("person-platform", platform_admin=True), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_a_tenant_admin_approves_a_plain_role() -> None:
    params, effect = await _parked_card("viewer")
    request_id = await _park(params, effect, op_id=_REALM_ROLE)

    await _approve(request_id, _person("person-admin", role=TenantRole.TENANT_ADMIN), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_approve_when_the_roles_could_not_be_read() -> None:
    connector, _ = _connector(fails=True)
    ctx = _realm_ctx("reporting", connector)
    from meho_backplane.operations._preview import build_proposed_effect

    effect = await build_proposed_effect(ctx)
    request_id = await _park(ctx.params, effect, op_id=_REALM_ROLE)

    with pytest.raises(ApprovalRoleFloorError):
        await _approve(request_id, _person("person-admin"), ctx.params)


# ---------------------------------------------------------------------------
# Right before the run
# ---------------------------------------------------------------------------


def _live_card(monkeypatch: pytest.MonkeyPatch, preview: dict[str, Any]) -> None:
    async def _fake(operator: Any, **kwargs: Any) -> dict[str, Any]:
        return {"op_class": "write", "preview": preview}

    import meho_backplane.operations.approval_object_check as object_check_module

    monkeypatch.setattr(object_check_module, "live_card", _fake)


async def _live_preview(role: str, parts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    connector, _ = _connector(parts)
    return await _card(_realm_ctx(role, connector))


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_parts_of_the_role_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params, effect = await _parked_card("reporting")
    request = await _row(await _park(params, effect, op_id=_REALM_ROLE))
    _live_card(monkeypatch, await _live_preview("reporting", {**_PARTS, "r-1": [_ROLES["viewer"]]}))

    reason = await grant_changed_since_request(_person("person-platform"), request, None, params)

    assert reason == ROLE_PARTS_CHANGED


@pytest.mark.asyncio
async def test_the_run_is_refused_when_a_plain_role_became_a_composite_with_admin_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params, effect = await _parked_card("viewer")
    request = await _row(await _park(params, effect, op_id=_REALM_ROLE))
    grown = {**_PARTS, "r-3": [_role("m-1", "realm-admin", client=_MGMT_UUID)]}
    _ROLES["viewer"]["composite"] = True
    try:
        _live_card(monkeypatch, await _live_preview("viewer", grown))
    finally:
        _ROLES["viewer"]["composite"] = False

    reason = await grant_changed_since_request(_person("person-platform"), request, None, params)

    assert reason == GRANT_REASON_STRICTER


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_parts_cannot_be_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params, effect = await _parked_card("reporting")
    request = await _row(await _park(params, effect, op_id=_REALM_ROLE))
    connector, _ = _connector(fails=True)
    _live_card(monkeypatch, await _card(_realm_ctx("reporting", connector)))

    reason = await grant_changed_since_request(_person("person-platform"), request, None, params)

    assert reason is not None


@pytest.mark.asyncio
async def test_the_run_goes_ahead_when_the_parts_are_the_approved_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params, effect = await _parked_card("reporting")
    request = await _row(await _park(params, effect, op_id=_REALM_ROLE))
    _live_card(monkeypatch, await _live_preview("reporting", _PARTS))

    reason = await grant_changed_since_request(_person("person-platform"), request, None, params)

    assert reason is None


@pytest.mark.asyncio
async def test_a_realm_outside_the_login_runs_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, _ = _connector()
    ctx = _realm_ctx("reporting", connector, realm="customer-realm")
    effect = await _card(ctx)
    request = await _row(await _park(ctx.params, {"preview": effect}, op_id=_REALM_ROLE))
    _live_card(monkeypatch, effect)

    reason = await grant_changed_since_request(_person("person-admin"), request, None, ctx.params)

    assert reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", [_REALM, "customer-realm"])
@pytest.mark.parametrize("op_id", [_REALM_ROLE, _CLIENT_ROLE])
async def test_a_request_from_before_the_role_check_is_refused(
    monkeypatch: pytest.MonkeyPatch, realm: str, op_id: str
) -> None:
    connector, _ = _connector()
    ctx = _realm_ctx("reporting", connector, realm=realm)
    live = await _card(ctx)
    old = {key: value for key, value in live.items() if key != "effective_roles"}
    request = await _row(await _park(ctx.params, {"preview": old}, op_id=op_id))
    _live_card(monkeypatch, live)

    reason = await grant_changed_since_request(
        _person("person-platform"), request, None, ctx.params
    )

    assert reason == ROLE_PARTS_NOT_READ
