# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for finding a person in MEHO's own sign-in realm, and their tenant.

A fake Keycloak admin API (respx) answers the reads of
:mod:`meho_backplane.auth.realm_users`:

* by id and by exact username; a name that is only part of another name
  does not match; upper and lower case do not count; an id need not be a
  UUID; a lookup by id only never searches the usernames;
* the tenant comes from a role group's ``meho_tenant`` or ``tenant_id``
  attribute, or from a parent group's;
* every page of the person's groups is read;
* a failed read raises and never reads as "not found" or "not in the
  tenant".
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminError,
    KeycloakForbiddenError,
)
from meho_backplane.auth.realm_users import (
    GROUP_PAGE_SIZE,
    MAX_GROUP_PAGES,
    RealmUser,
    RealmUserNotFoundError,
    find_realm_user,
    user_in_tenant,
)

_ADMIN_URL = "https://keycloak.test/admin/realms/meho"
_TOKEN_URL = "https://keycloak.test/realms/meho/protocol/openid-connect/token"
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ALICE = "5d0c1a52-0000-4000-8000-00000000aaaa"
#: The id of a person from a linked user directory: not a UUID.
_DIRECTORY_ID = "f:3c5e7a90-directory:dora"


def _kc() -> KeycloakAdminClient:
    return KeycloakAdminClient(
        admin_url=_ADMIN_URL,
        token_url=_TOKEN_URL,
        client_id="meho-admin",
        client_secret="made-up-admin-secret",
    )


def _token(router: respx.MockRouter) -> None:
    router.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))


def _no_id(router: respx.MockRouter, user: str) -> None:
    router.get(f"{_ADMIN_URL}/users/{quote(user, safe='')}").mock(return_value=httpx.Response(404))


def _group(name: str, **attributes: list[str]) -> dict[str, Any]:
    return {"id": f"g-{name}", "name": name, "path": f"/{name}", "attributes": attributes}


# ---------------------------------------------------------------------------
# find_realm_user
# ---------------------------------------------------------------------------


async def test_a_person_is_found_by_id() -> None:
    with respx.mock as router:
        _token(router)
        router.get(f"{_ADMIN_URL}/users/{_ALICE}").mock(
            return_value=httpx.Response(200, json={"id": _ALICE, "username": "alice"})
        )
        async with _kc() as kc:
            found = await find_realm_user(kc, _ALICE)
    assert found == RealmUser(id=_ALICE, username="alice")


async def test_a_person_is_found_by_exact_username_only() -> None:
    with respx.mock as router:
        _token(router)
        _no_id(router, "alice")
        route = router.get(f"{_ADMIN_URL}/users").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "id-other", "username": "alice2"},
                    {"id": _ALICE, "username": "Alice"},
                ],
            )
        )
        async with _kc() as kc:
            found = await find_realm_user(kc, "alice")
    assert found == RealmUser(id=_ALICE, username="Alice")
    params = route.calls.last.request.url.params
    assert params["username"] == "alice"
    assert params["exact"] == "true"


async def test_an_id_that_is_not_a_uuid_is_found_by_id() -> None:
    with respx.mock as router:
        _token(router)
        router.get(f"{_ADMIN_URL}/users/{quote(_DIRECTORY_ID, safe='')}").mock(
            return_value=httpx.Response(200, json={"id": _DIRECTORY_ID, "username": "dora"})
        )
        async with _kc() as kc:
            found = await find_realm_user(kc, _DIRECTORY_ID, by_id=True)
    assert found == RealmUser(id=_DIRECTORY_ID, username="dora")


async def test_a_lookup_by_id_only_never_searches_the_usernames() -> None:
    with respx.mock as router:
        _token(router)
        _no_id(router, "alice")
        search = router.get(f"{_ADMIN_URL}/users").mock(
            return_value=httpx.Response(200, json=[{"id": _ALICE, "username": "alice"}])
        )
        async with _kc() as kc:
            with pytest.raises(RealmUserNotFoundError):
                await find_realm_user(kc, "alice", by_id=True)
    assert not search.called


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, KeycloakForbiddenError), (500, KeycloakAdminError)],
)
async def test_a_failed_read_by_id_is_an_error_not_a_missing_person(
    status: int, error: type[Exception]
) -> None:
    with respx.mock as router:
        _token(router)
        router.get(f"{_ADMIN_URL}/users/alice").mock(return_value=httpx.Response(status))
        async with _kc() as kc:
            with pytest.raises(error) as caught:
                await find_realm_user(kc, "alice")
    assert not isinstance(caught.value, RealmUserNotFoundError)


async def test_an_unknown_id_is_looked_up_as_a_username_too() -> None:
    with respx.mock as router:
        _token(router)
        router.get(f"{_ADMIN_URL}/users/{_ALICE}").mock(return_value=httpx.Response(404))
        router.get(f"{_ADMIN_URL}/users").mock(return_value=httpx.Response(200, json=[]))
        async with _kc() as kc:
            with pytest.raises(RealmUserNotFoundError):
                await find_realm_user(kc, _ALICE)


@pytest.mark.parametrize("user", ["", "   "])
async def test_an_empty_user_is_not_found(user: str) -> None:
    with respx.mock as router:
        _token(router)
        async with _kc() as kc:
            with pytest.raises(RealmUserNotFoundError):
                await find_realm_user(kc, user)


async def test_two_people_with_the_same_name_are_an_error() -> None:
    with respx.mock as router:
        _token(router)
        _no_id(router, "bob")
        router.get(f"{_ADMIN_URL}/users").mock(
            return_value=httpx.Response(
                200, json=[{"id": "a", "username": "bob"}, {"id": "b", "username": "BOB"}]
            )
        )
        async with _kc() as kc:
            with pytest.raises(KeycloakAdminError) as caught:
                await find_realm_user(kc, "bob")
    assert not isinstance(caught.value, RealmUserNotFoundError)


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, KeycloakForbiddenError), (500, KeycloakAdminError)],
)
async def test_a_failed_read_is_an_error_not_a_missing_person(
    status: int, error: type[Exception]
) -> None:
    with respx.mock as router:
        _token(router)
        _no_id(router, "alice")
        router.get(f"{_ADMIN_URL}/users").mock(return_value=httpx.Response(status))
        async with _kc() as kc:
            with pytest.raises(error) as caught:
                await find_realm_user(kc, "alice")
    assert not isinstance(caught.value, RealmUserNotFoundError)


async def test_an_answer_that_is_not_a_list_is_an_error() -> None:
    with respx.mock as router:
        _token(router)
        _no_id(router, "alice")
        router.get(f"{_ADMIN_URL}/users").mock(return_value=httpx.Response(200, json={}))
        async with _kc() as kc:
            with pytest.raises(KeycloakAdminError):
                await find_realm_user(kc, "alice")


# ---------------------------------------------------------------------------
# user_in_tenant
# ---------------------------------------------------------------------------


def _groups_route(router: respx.MockRouter, groups: list[dict[str, Any]]) -> respx.Route:
    def _answer(request: httpx.Request) -> httpx.Response:
        first = int(request.url.params.get("first", "0"))
        size = int(request.url.params.get("max", "100"))
        return httpx.Response(200, json=groups[first : first + size])

    return router.get(f"{_ADMIN_URL}/users/{_ALICE}/groups").mock(side_effect=_answer)


@pytest.mark.parametrize(
    "attributes",
    [
        {"meho_tenant": [f"{_TENANT}:main:Operators"]},
        {"tenant_id": [str(_TENANT)]},
        {"meho_tenant": ["not-a-uuid:x:Operators", f"{_TENANT}:main:Admins"]},
    ],
)
async def test_a_role_group_of_the_tenant_counts(attributes: dict[str, list[str]]) -> None:
    with respx.mock as router:
        _token(router)
        route = _groups_route(router, [_group("MEHO-Operators", **attributes)])
        async with _kc() as kc:
            assert await user_in_tenant(kc, _ALICE, _TENANT) is True
    assert route.calls.last.request.url.params["briefRepresentation"] == "false"


@pytest.mark.parametrize(
    "groups",
    [
        [],
        [_group("MEHO-other-Operators", meho_tenant=[f"{_OTHER_TENANT}:other:Operators"])],
        [_group("Plain")],
        [{"id": "g", "name": "odd", "attributes": None}],
    ],
)
async def test_no_role_group_of_the_tenant_is_false(groups: list[dict[str, Any]]) -> None:
    with respx.mock as router:
        _token(router)
        _groups_route(router, groups)
        async with _kc() as kc:
            assert await user_in_tenant(kc, _ALICE, _TENANT) is False


async def test_a_parent_group_of_the_tenant_counts() -> None:
    with respx.mock as router:
        _token(router)
        child = {**_group("Network"), "parentId": "g-mid"}
        _groups_route(router, [child])
        router.get(f"{_ADMIN_URL}/groups/g-mid").mock(
            return_value=httpx.Response(200, json={**_group("Mid"), "parentId": "g-top"})
        )
        router.get(f"{_ADMIN_URL}/groups/g-top").mock(
            return_value=httpx.Response(
                200, json=_group("MEHO-Operators", meho_tenant=[f"{_TENANT}:main:Operators"])
            )
        )
        async with _kc() as kc:
            assert await user_in_tenant(kc, _ALICE, _TENANT) is True


async def test_the_second_page_of_groups_is_read() -> None:
    groups = [_group(f"g{i}") for i in range(GROUP_PAGE_SIZE)]
    groups.append(_group("MEHO-Admins", meho_tenant=[f"{_TENANT}:main:Admins"]))
    with respx.mock as router:
        _token(router)
        route = _groups_route(router, groups)
        async with _kc() as kc:
            assert await user_in_tenant(kc, _ALICE, _TENANT) is True
    assert route.call_count == 2


async def test_too_many_pages_of_groups_is_an_error() -> None:
    page = [_group(f"g{i}") for i in range(GROUP_PAGE_SIZE)]
    with respx.mock as router:
        _token(router)
        route = router.get(f"{_ADMIN_URL}/users/{_ALICE}/groups").mock(
            return_value=httpx.Response(200, json=page)
        )
        async with _kc() as kc:
            with pytest.raises(KeycloakAdminError):
                await user_in_tenant(kc, _ALICE, _TENANT)
    assert route.call_count == MAX_GROUP_PAGES


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_a_failed_group_read_is_an_error_not_false(status: int) -> None:
    with respx.mock as router:
        _token(router)
        router.get(f"{_ADMIN_URL}/users/{_ALICE}/groups").mock(return_value=httpx.Response(status))
        async with _kc() as kc:
            with pytest.raises(KeycloakAdminError):
                await user_in_tenant(kc, _ALICE, _TENANT)


async def test_a_failed_parent_read_is_an_error_not_false() -> None:
    with respx.mock as router:
        _token(router)
        _groups_route(router, [{**_group("Network"), "parentId": "g-top"}])
        router.get(f"{_ADMIN_URL}/groups/g-top").mock(return_value=httpx.Response(500))
        async with _kc() as kc:
            with pytest.raises(KeycloakAdminError):
                await user_in_tenant(kc, _ALICE, _TENANT)
