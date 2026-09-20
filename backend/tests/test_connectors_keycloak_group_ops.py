# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak group-lifecycle op E2E + unit tests (#3280).

Drives the two group **read** ops (``keycloak.group.list`` /
``keycloak.group.member.list``) and the four approval-gated group **write**
ops (``keycloak.group.create`` / ``.update_attributes`` / ``.member.add`` /
``.member.remove``) through the full ``call_operation`` dispatch stack
against a respx-mocked Keycloak Admin REST API — no running Keycloak, no
live Vault. The admin-credential loader is stubbed; ``respx`` replays the
Admin REST fixtures; the connector instance is preseeded into the
dispatcher's instance cache.

Acceptance criteria verified (Issue #3280)
==========================================

(a) The group ops register with the stated safety levels — reads safe,
    writes ``dangerous`` + ``requires_approval=True``.
(b) ``group.create`` accepts an ``attributes`` map (Keycloak's
    ``{key: [values]}`` shape; a plain string is wrapped) and sets it on
    the group; ``group.list`` with ``brief=false`` reads attributes back.
(c) ``group.create`` is idempotent by (parent, name): a 409 returns
    ``already_exists=True`` with the existing id.
(d) membership add/remove resolve ``username`` → UUID and are idempotent
    (already a member → ``unchanged``).
(e) the group writes classify as plain ``write`` on the broadcast feed
    (group attributes are not secret material).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import respx

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "rdc-keycloak-group-e2e"
_KC_HOST = "keycloak-group-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_TOKEN = "kc-admin-token-group-e2e"
_REALM = "meho"

_GROUP_UUID = "77777777-7777-7777-7777-777777777777"
_NEW_GROUP_UUID = "88888888-8888-8888-8888-888888888888"
_PARENT_UUID = "99999999-9999-9999-9999-999999999999"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

_OPERATOR_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
_OPERATOR = Operator(
    sub="keycloak-group-e2e-test",
    name="Keycloak Group E2E Test Operator",
    email=None,
    raw_jwt="<keycloak-group-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)


def _location(path: str) -> dict[str, str]:
    return {"Location": f"{_KC_BASE_URL}{path}"}


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": _ADMIN_TOKEN, "expires_in": 300}
    )


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


async def _seed_keycloak_target() -> TargetORM:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="keycloak",
            host=_KC_HOST,
            port=443,
            fqdn=None,
            secret_ref="rdc-hetzner-dc/keycloak/admin",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={"managed_realm": _REALM},
            fingerprint={"version": "26.0.5"},
            notes="seeded by test_connectors_keycloak_group_ops",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _wire_seeded_connector() -> KeycloakConnector:
    instance = KeycloakConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    return instance


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
async def keycloak_group_e2e() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    await _seed_keycloak_target()
    connector = _wire_seeded_connector()
    yield connector
    await connector.aclose()


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool) -> dict[str, Any]:
    """Dispatch an op by name; ``approved`` bypasses the write approval gate."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved_target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=resolved_target,
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# Read ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_list_brief_false_requests_attributes(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """group.list with brief=false asks Keycloak for the full (attribute) body."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        route = mock.get(f"/admin/realms/{_REALM}/groups").respond(
            200,
            json=[
                {
                    "id": _GROUP_UUID,
                    "name": "role-tenant",
                    "path": "/role-tenant",
                    "attributes": {"tenant_id": ["t-2"], "tenant_role": ["admin"]},
                }
            ],
        )
        result = await _dispatch("keycloak.group.list", {"brief": False}, approved=False)
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["total"] == 1
    assert result["result"]["rows"][0]["attributes"]["tenant_id"] == ["t-2"]
    assert route.calls.last.request.url.params.get("briefRepresentation") == "false"


@pytest.mark.asyncio
async def test_group_list_by_parent_lists_children(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """group.list with parent_id hits the children endpoint of that parent."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        route = mock.get(f"/admin/realms/{_REALM}/groups/{_PARENT_UUID}/children").respond(
            200, json=[{"id": _GROUP_UUID, "name": "child", "path": "/parent/child"}]
        )
        result = await _dispatch("keycloak.group.list", {"parent_id": _PARENT_UUID}, approved=False)
    assert result["status"] == "ok", result.get("error")
    assert route.called
    assert result["result"]["rows"][0]["name"] == "child"


@pytest.mark.asyncio
async def test_group_member_list_redacts_credentials(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """group.member.list returns members with credential material scrubbed."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}/members").respond(
            200,
            json=[{"id": _USER_UUID, "username": "operator-a", "credentials": [{"value": "x"}]}],
        )
        result = await _dispatch("keycloak.group.member.list", {"id": _GROUP_UUID}, approved=False)
    assert result["status"] == "ok", result.get("error")
    row = result["result"]["rows"][0]
    assert row["username"] == "operator-a"
    assert row["credentials"] == "***REDACTED***"


# ---------------------------------------------------------------------------
# group.create — attributes + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_create_sets_attributes_and_returns_id_path(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """group.create sends the attribute map and returns the new id + path."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        post_route = mock.post(f"/admin/realms/{_REALM}/groups").respond(
            201, headers=_location(f"/admin/realms/{_REALM}/groups/{_NEW_GROUP_UUID}")
        )
        mock.get(f"/admin/realms/{_REALM}/groups").respond(
            200,
            json=[{"id": _NEW_GROUP_UUID, "name": "role-tenant-2", "path": "/role-tenant-2"}],
        )
        result = await _dispatch(
            "keycloak.group.create",
            {
                "name": "role-tenant-2",
                # A plain string must be wrapped into a single-element list.
                "attributes": {"tenant_id": "t-2", "tenant_role": ["admin"]},
            },
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["created"] is True
    assert result["result"]["already_exists"] is False
    assert result["result"]["id"] == _NEW_GROUP_UUID
    assert result["result"]["path"] == "/role-tenant-2"
    body = json.loads(post_route.calls.last.request.content)
    assert body["attributes"] == {"tenant_id": ["t-2"], "tenant_role": ["admin"]}


@pytest.mark.asyncio
async def test_group_create_409_is_idempotent(keycloak_group_e2e: KeycloakConnector) -> None:
    """A 409 already-exists on group.create returns already_exists + the existing id."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post(f"/admin/realms/{_REALM}/groups").respond(409, json={"errorMessage": "exists"})
        mock.get(f"/admin/realms/{_REALM}/groups").respond(
            200, json=[{"id": _GROUP_UUID, "name": "role-tenant", "path": "/role-tenant"}]
        )
        result = await _dispatch("keycloak.group.create", {"name": "role-tenant"}, approved=True)
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["already_exists"] is True
    assert result["result"]["created"] is False
    assert result["result"]["id"] == _GROUP_UUID


@pytest.mark.asyncio
async def test_group_create_under_parent_uses_children_endpoint(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """group.create with parent_id POSTs to the parent's children endpoint."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        post_route = mock.post(f"/admin/realms/{_REALM}/groups/{_PARENT_UUID}/children").respond(
            201, headers=_location(f"/admin/realms/{_REALM}/groups/{_NEW_GROUP_UUID}")
        )
        mock.get(f"/admin/realms/{_REALM}/groups/{_PARENT_UUID}/children").respond(
            200, json=[{"id": _NEW_GROUP_UUID, "name": "child", "path": "/parent/child"}]
        )
        result = await _dispatch(
            "keycloak.group.create",
            {"name": "child", "parent_id": _PARENT_UUID},
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert post_route.called
    assert result["result"]["id"] == _NEW_GROUP_UUID


# ---------------------------------------------------------------------------
# group.update_attributes — merge vs replace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_update_attributes_merges_by_default(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """update_attributes merges onto the group's current attributes by default."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(
            200,
            json={"id": _GROUP_UUID, "name": "role-tenant", "attributes": {"existing": ["v"]}},
        )
        put_route = mock.put(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(204)
        result = await _dispatch(
            "keycloak.group.update_attributes",
            {"id": _GROUP_UUID, "attributes": {"tenant_id": "t-2"}},
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["replaced"] is False
    assert result["result"]["attribute_keys"] == ["existing", "tenant_id"]
    body = json.loads(put_route.calls.last.request.content)
    assert body["attributes"] == {"existing": ["v"], "tenant_id": ["t-2"]}
    assert body["name"] == "role-tenant"


@pytest.mark.asyncio
async def test_group_update_attributes_replace_drops_existing(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """update_attributes with replace=true sets the attribute map wholesale."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(
            200,
            json={"id": _GROUP_UUID, "name": "role-tenant", "attributes": {"existing": ["v"]}},
        )
        put_route = mock.put(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(204)
        result = await _dispatch(
            "keycloak.group.update_attributes",
            {"id": _GROUP_UUID, "attributes": {"tenant_id": ["t-2"]}, "replace": True},
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["replaced"] is True
    assert result["result"]["attribute_keys"] == ["tenant_id"]
    body = json.loads(put_route.calls.last.request.content)
    assert body["attributes"] == {"tenant_id": ["t-2"]}


# ---------------------------------------------------------------------------
# Membership — resolution + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_add_resolves_username_and_puts(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """member.add resolves username → UUID, checks membership, then PUTs the join."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[{"id": _USER_UUID, "username": "operator-a"}]
        )
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups").respond(200, json=[])
        put_route = mock.put(
            f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups/{_GROUP_UUID}"
        ).respond(204)
        result = await _dispatch(
            "keycloak.group.member.add",
            {"group_id": _GROUP_UUID, "username": "operator-a"},
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["added"] is True
    assert result["result"]["unchanged"] is False
    assert result["result"]["user_id"] == _USER_UUID
    assert put_route.called


@pytest.mark.asyncio
async def test_member_add_already_member_is_unchanged(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """member.add on an existing member returns unchanged and issues no PUT."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups").respond(
            200, json=[{"id": _GROUP_UUID, "name": "role-tenant"}]
        )
        put_route = mock.put(
            f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups/{_GROUP_UUID}"
        ).respond(204)
        result = await _dispatch(
            "keycloak.group.member.add",
            {"group_id": _GROUP_UUID, "user_id": _USER_UUID},
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["added"] is False
    assert result["result"]["unchanged"] is True
    assert not put_route.called, "an existing membership must not re-issue the PUT"


@pytest.mark.asyncio
async def test_member_remove_deletes_when_member(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """member.remove DELETEs the membership when the user is a member."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups").respond(
            200, json=[{"id": _GROUP_UUID, "name": "role-tenant"}]
        )
        del_route = mock.delete(
            f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups/{_GROUP_UUID}"
        ).respond(204)
        result = await _dispatch(
            "keycloak.group.member.remove",
            {"group_id": _GROUP_UUID, "user_id": _USER_UUID},
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["removed"] is True
    assert result["result"]["unchanged"] is False
    assert del_route.called


@pytest.mark.asyncio
async def test_member_remove_non_member_is_unchanged(
    keycloak_group_e2e: KeycloakConnector,
) -> None:
    """member.remove on a non-member returns unchanged and issues no DELETE."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups").respond(200, json=[])
        del_route = mock.delete(
            f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups/{_GROUP_UUID}"
        ).respond(204)
        result = await _dispatch(
            "keycloak.group.member.remove",
            {"group_id": _GROUP_UUID, "user_id": _USER_UUID},
            approved=True,
        )
    assert result["status"] == "ok", result.get("error")
    assert result["result"]["removed"] is False
    assert result["result"]["unchanged"] is True
    assert not del_route.called


# ---------------------------------------------------------------------------
# Broadcast classification (criterion e)
# ---------------------------------------------------------------------------


def test_group_ops_broadcast_classification() -> None:
    """Group writes classify plain ``write``; group reads classify ``read``."""
    for op_id in (
        "keycloak.group.create",
        "keycloak.group.update_attributes",
        "keycloak.group.member.add",
        "keycloak.group.member.remove",
    ):
        assert classify_op(op_id) == "write", f"{op_id} should classify as a plain write"
    for op_id in ("keycloak.group.list", "keycloak.group.member.list"):
        assert classify_op(op_id) == "read"


# ---------------------------------------------------------------------------
# Park-time preview builders (pure, no network)
# ---------------------------------------------------------------------------


@dataclass
class _FakeDescriptor:
    op_id: str


@dataclass
class _FakeTarget:
    extras: dict[str, Any]


def _preview_ctx(op_id: str, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_OPERATOR,
        target=_FakeTarget(extras={"managed_realm": _REALM}),
        params=params,
        connector_id="keycloak-1.x",
    )


@pytest.mark.asyncio
async def test_group_create_preview_shows_name_and_attribute_keys() -> None:
    """The create preview hoists the group name + attribute keys; realm labelled."""
    ctx = _preview_ctx(
        "keycloak.group.create",
        {"name": "role-tenant-2", "attributes": {"tenant_id": ["t-2"], "tenant_role": ["admin"]}},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["op_class"] == "write"
    preview = effect["preview"]
    assert preview["resource"] == "group"
    assert preview["name"] == "role-tenant-2"
    assert preview["realm"] == _REALM
    assert preview["attribute_keys"] == ["tenant_id", "tenant_role"]


@pytest.mark.asyncio
async def test_group_create_preview_scrubs_password_keyed_attribute() -> None:
    """A stray ``password``-keyed attribute value is scrubbed in the durable row."""
    ctx = _preview_ctx(
        "keycloak.group.create",
        {"name": "g", "attributes": {"password": ["leak-me"], "tenant_id": ["t-2"]}},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    # The key name is visible, but the value is redacted (defence in depth).
    assert preview["attributes"]["password"] == "***REDACTED***"
    assert "leak-me" not in json.dumps(effect)


@pytest.mark.asyncio
async def test_group_member_add_preview_shows_action_and_targets() -> None:
    """The membership preview surfaces the action + resolved-by-name identity."""
    ctx = _preview_ctx(
        "keycloak.group.member.add",
        {"group_id": _GROUP_UUID, "username": "operator-a"},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["resource"] == "group_membership"
    assert preview["action"] == "add"
    assert preview["group_id"] == _GROUP_UUID
    assert preview["username"] == "operator-a"


# ---------------------------------------------------------------------------
# update_attributes park-time preview — before→after delta (B1)
# ---------------------------------------------------------------------------


def _update_attrs_ctx(
    params: dict[str, Any],
    *,
    current: dict[str, Any] | None,
    raises: bool = False,
) -> PreviewContext:
    """Build an update_attributes preview ctx with a connector that returns
    *current* attributes from the group GET (or raises, for the fail-soft test).

    ``current=None`` leaves ``connector_instance=None`` so the builder cannot
    read the before-state (the unit-context degrade path).
    """
    connector: Any = None
    if current is not None or raises:
        connector = KeycloakConnector(credentials_loader=_stub_loader)

        async def _fake_get_admin_json(
            _target: Any, _path: str, *, operator: Any
        ) -> dict[str, Any]:
            if raises:
                raise RuntimeError("keycloak unreachable")
            return {"id": _GROUP_UUID, "name": "role-tenant", "attributes": current or {}}

        connector._get_admin_json = _fake_get_admin_json  # type: ignore[method-assign]
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id="keycloak.group.update_attributes"),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_OPERATOR,
        target=_FakeTarget(extras={"managed_realm": _REALM}),
        params=params,
        connector_id="keycloak-1.x",
    )


@pytest.mark.asyncio
async def test_update_attributes_preview_merge_shows_before_and_added_no_removed() -> None:
    """A merge surfaces the current keys + added key, and removes nothing."""
    ctx = _update_attrs_ctx(
        {"id": _GROUP_UUID, "attributes": {"tenant_role": ["admin"]}},
        current={"tenant_id": ["t-1"], "keep": ["x"]},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["current_attributes_available"] is True
    assert preview["current_attribute_keys"] == ["keep", "tenant_id"]
    assert preview["added_keys"] == ["tenant_role"]
    assert preview["removed_keys"] == []  # a merge never drops a key
    assert preview["resulting_attribute_keys"] == ["keep", "tenant_id", "tenant_role"]
    assert "warning" not in preview


@pytest.mark.asyncio
async def test_update_attributes_preview_replace_surfaces_removed_tenant_claim_keys() -> None:
    """The headline B1 case: a replace dropping tenant_id/tenant_role is loud."""
    ctx = _update_attrs_ctx(
        {"id": _GROUP_UUID, "attributes": {"other": ["y"]}, "replace": True},
        current={"tenant_id": ["t-1"], "tenant_role": ["admin"]},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["replace"] is True
    assert preview["current_attributes_available"] is True
    assert preview["removed_keys"] == ["tenant_id", "tenant_role"]
    assert preview["added_keys"] == ["other"]
    assert preview["resulting_attribute_keys"] == ["other"]
    # An explicit warning names the dropped tenant-claim keys.
    assert "warning" in preview
    assert "tenant_id" in preview["warning"] and "tenant_role" in preview["warning"]


@pytest.mark.asyncio
async def test_update_attributes_preview_detects_changed_value() -> None:
    """A key present before and after with a different value is a changed_key."""
    ctx = _update_attrs_ctx(
        {"id": _GROUP_UUID, "attributes": {"tenant_id": ["t-2"]}},
        current={"tenant_id": ["t-1"]},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["changed_keys"] == ["tenant_id"]
    assert preview["added_keys"] == []
    assert preview["removed_keys"] == []


@pytest.mark.asyncio
async def test_update_attributes_preview_scrubs_current_password_attribute() -> None:
    """A stray password-keyed CURRENT attribute is scrubbed in the durable row."""
    ctx = _update_attrs_ctx(
        {"id": _GROUP_UUID, "attributes": {"tenant_id": ["t-2"]}},
        current={"password": ["leak-me"], "tenant_id": ["t-1"]},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    assert preview["current_attributes"]["password"] == "***REDACTED***"
    assert "leak-me" not in json.dumps(effect)


@pytest.mark.asyncio
async def test_update_attributes_preview_failsoft_when_current_unreadable() -> None:
    """A fetch fault degrades to the incoming-only view — never blocks the park."""
    ctx = _update_attrs_ctx(
        {"id": _GROUP_UUID, "attributes": {"tenant_id": ["t-2"]}, "replace": True},
        current=None,
        raises=True,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    preview = effect["preview"]
    # Degraded: the before-state could not be read, but the preview still
    # carries the incoming payload and marks the delta unavailable.
    assert preview["current_attributes_available"] is False
    assert preview["attribute_keys"] == ["tenant_id"]
    assert "removed_keys" not in preview


@pytest.mark.asyncio
async def test_update_attributes_preview_no_connector_degrades() -> None:
    """With no connector instance (unit context) the preview still returns."""
    ctx = _update_attrs_ctx(
        {"id": _GROUP_UUID, "attributes": {"tenant_id": ["t-2"]}},
        current=None,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    assert effect["preview"]["current_attributes_available"] is False
