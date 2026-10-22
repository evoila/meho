# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Directory (AD/LDAP) groups give MEHO's tenant and role, without duplicate groups.

MEHO's built-in Keycloak (the subchart's realm file plus the bootstrap
Job's realm stage, as in ``test_keycloak_role_groups_e2e.py``) federates a
real OpenLDAP. The five top-level MEHO role groups keep carrying the
``tenant_id`` / ``tenant_role`` / ``approver`` attributes; the realm's User
Attribute mappers (``aggregate.attrs=false``) find them through the
person's groups and their parent groups
(``docs/decisions/login-role-groups.md``).

Two directory layouts, each with its own LDAP user federation (its own
users and groups subtree) in the same realm:

A. **Same names.** The directory has groups named exactly like MEHO's
   (``MEHO-Operators`` ...). One ``group-ldap-mapper`` (READ_ONLY, Groups
   Path ``/``, no mapped group attributes, no dropping, a filter on the five
   names) matches them to OUR groups by name: no duplicates after a group
   sync or a login, our attributes stay, and the unrelated ``staff`` group
   is not imported.
B. **Different names.** The directory has ``SG-MEHO-Ops`` and friends. One
   ``group-ldap-mapper`` per MEHO group, each selecting exactly one
   directory group, with Groups Path = our group, so the directory group
   lands as a subgroup (``/MEHO-Operators/SG-MEHO-Ops``) and its members get
   the parent's attributes.

In both, leaving the directory group takes the tenant away at the next
login (READ_ONLY reads membership from LDAP), and a refresh of an older
session loses it too. In B the ``groups`` claim names the directory group,
so MEHO's one-group check needs ``JWT_ROLE_GROUPS`` to list the SG-* names.

The login is the password grant on ``meho-cli`` (switched on by the
``baseline`` fixture); Keycloak checks the password against LDAP. Adding user
federation needs ``manage-realm``, which ``meho-admin`` lacks, so the
federation is set up with the master admin the customer-IdP test adds.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import HTTPException

from meho_backplane.auth.operator import TenantRole
from tests.integration.test_keycloak_customer_idp_e2e import (  # noqa: F401 -- fixtures
    _OurSite,
    network,
    site,
)
from tests.integration.test_keycloak_role_groups_e2e import (
    _LOGIN_CLIENT,
    _ROLE_GROUPS,
    _TENANT_ID,
    _allow_password_grant_on_cli,
    _claims,
    _password_login,
    _token,
    _user_groups,
    _verify,
)

if TYPE_CHECKING:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network

_LDAP_IMAGE = "osixia/openldap:1.5.0"
_LDAP_HOST = "openldap"  # the directory's name on the shared Docker network
_BASE_DN = "dc=example,dc=org"
_ADMIN_DN = f"cn=admin,{_BASE_DN}"
# groupOfNames needs at least one member; this one is nobody.
_PLACEHOLDER_DN = f"cn=placeholder,{_BASE_DN}"

_PEOPLE_A = f"ou=people-a,{_BASE_DN}"
_GROUPS_A = f"ou=groups-a,{_BASE_DN}"
_PEOPLE_B = f"ou=people-b,{_BASE_DN}"
_GROUPS_B = f"ou=groups-b,{_BASE_DN}"

_MEHO_GROUPS = list(_ROLE_GROUPS)
# B: directory group -> MEHO role group (its parent in Keycloak).
_RENAMED = {
    "SG-MEHO-Read": "MEHO-Readers",
    "SG-MEHO-Ops": "MEHO-Operators",
    "SG-MEHO-SAOps": "MEHO-SA-Operators",
    "SG-MEHO-Admin": "MEHO-Admins",
    "SG-MEHO-Approve": "MEHO-Approvers",
}
_DIRECTORY_GROUP = {meho: sg for sg, meho in _RENAMED.items()}
_TENANT_CLAIMS = ("tenant_id", "tenant_role", "approver")

_LDAP_COMPONENT = "org.keycloak.storage.UserStorageProvider"
_MAPPER_COMPONENT = "org.keycloak.storage.ldap.mappers.LDAPStorageMapper"


# --- The directory ----------------------------------------------------------------


@dataclass
class _Ldap:
    container: DockerContainer
    admin_password: str

    def run(self, tool: str, ldif: str) -> str:
        """Feed *ldif* to ``ldapadd`` / ``ldapmodify`` inside the container."""
        command = (
            f"{tool} -x -H ldap://localhost -D '{_ADMIN_DN}' -w '{self.admin_password}' "
            f"<<'LDIF'\n{ldif.strip()}\nLDIF\n"
        )
        result = self.container.get_wrapped_container().exec_run(["sh", "-c", command])
        output = result.output.decode(errors="replace")
        assert result.exit_code == 0, f"{tool} failed ({result.exit_code}): {output}"
        return output

    def add_person(self, people_dn: str, uid: str, groups_dn: str, groups: list[str]) -> str:
        """An inetOrgPerson with a full profile, member of *groups*; return the password."""
        password = secrets.token_urlsafe(18)
        self.run(
            "ldapadd",
            f"""
dn: uid={uid},{people_dn}
objectClass: inetOrgPerson
objectClass: organizationalPerson
objectClass: person
uid: {uid}
cn: Test
givenName: Test
sn: {uid}
mail: {uid}@directory.example
userPassword: {password}
""",
        )
        for group in groups:
            self.set_member(groups_dn, group, f"uid={uid},{people_dn}", member=True)
        return password

    def set_member(self, groups_dn: str, group: str, member_dn: str, *, member: bool) -> None:
        self.run(
            "ldapmodify",
            f"""
dn: cn={group},{groups_dn}
changetype: modify
{"add" if member else "delete"}: member
member: {member_dn}
""",
        )


def _group_ldif(groups_dn: str, names: list[str]) -> str:
    return "\n".join(
        f"dn: cn={name},{groups_dn}\nobjectClass: groupOfNames\ncn: {name}\n"
        f"member: {_PLACEHOLDER_DN}\n"
        for name in names
    )


@pytest.fixture(scope="module")
def ldap(network: Network) -> Iterator[_Ldap]:  # noqa: F811 -- fixture
    from testcontainers.core.container import DockerContainer

    from tests._strategies import wait_for_log_message

    admin_password = secrets.token_urlsafe(18)
    container = (
        DockerContainer(_LDAP_IMAGE)
        .with_network(network)
        .with_network_aliases(_LDAP_HOST)
        .with_env("LDAP_ORGANISATION", "Example")
        .with_env("LDAP_DOMAIN", "example.org")
        .with_env("LDAP_ADMIN_PASSWORD", admin_password)
        .with_env("LDAP_TLS", "false")
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"OpenLDAP container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(container, "slapd starting", timeout=120)
        directory = _Ldap(container, admin_password)
        deadline = time.monotonic() + 60
        while True:
            try:
                directory.run(
                    "ldapadd",
                    "\n".join(
                        f"dn: {dn}\nobjectClass: organizationalUnit\nou: {dn[3:].split(',')[0]}\n"
                        for dn in (_PEOPLE_A, _GROUPS_A, _PEOPLE_B, _GROUPS_B)
                    ),
                )
                break
            except AssertionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)
        directory.run("ldapadd", _group_ldif(_GROUPS_A, [*_MEHO_GROUPS, "staff"]))
        directory.run("ldapadd", _group_ldif(_GROUPS_B, [*_RENAMED, "staff"]))
        yield directory
    finally:
        container.stop()


# --- Keycloak: groups and the federation ------------------------------------------


def _top_level_groups(kc: _OurSite) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{kc.admin_url}/groups",
        headers=kc.headers(),
        params={"briefRepresentation": "false", "max": "1000"},
    )
    resp.raise_for_status()
    return list(resp.json())


def _children(kc: _OurSite, group_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{kc.admin_url}/groups/{group_id}/children",
        headers=kc.headers(),
        params={"briefRepresentation": "false", "max": "1000"},
    )
    resp.raise_for_status()
    return list(resp.json())


def _meho_groups_by_name(kc: _OurSite) -> dict[str, dict[str, Any]]:
    """Our top-level MEHO-* groups; fails on a duplicate name."""
    groups = [g for g in _top_level_groups(kc) if g["name"].startswith("MEHO-")]
    names = [g["name"] for g in groups]
    assert sorted(names) == sorted(set(names)), f"duplicate top-level groups: {names}"
    return {g["name"]: g for g in groups}


@pytest.fixture(scope="module")
def baseline(site: _OurSite) -> dict[str, dict[str, list[str]]]:  # noqa: F811 -- fixture
    """Our top-level groups and their attributes before any federation exists.

    Also switches on the password grant on ``meho-cli`` for the logins.
    """
    _allow_password_grant_on_cli(site)
    groups = _meho_groups_by_name(site)
    assert set(_MEHO_GROUPS) <= set(groups)
    return {name: group["attributes"] for name, group in groups.items()}


def _assert_our_groups_intact(
    kc: _OurSite, baseline: dict[str, dict[str, list[str]]], *, foreign: set[str]
) -> dict[str, dict[str, Any]]:
    """Each MEHO group once at the top, attributes unchanged, no *foreign* group on top."""
    top = _top_level_groups(kc)
    names = [g["name"] for g in top]
    for name in baseline:
        assert names.count(name) == 1, f"{name} appears {names.count(name)} times: {names}"
    assert not foreign & set(names), f"imported at the top: {foreign & set(names)}"
    groups = {g["name"]: g for g in top if g["name"] in baseline}
    for name, attributes in baseline.items():
        assert groups[name]["attributes"] == attributes, name
    return groups


def _realm_id(kc: _OurSite) -> str:
    resp = httpx.get(kc.admin_url, headers=kc.master_headers())
    resp.raise_for_status()
    return str(resp.json()["id"])


def _add_component(kc: _OurSite, component: dict[str, Any]) -> str:
    resp = httpx.post(f"{kc.admin_url}/components", headers=kc.master_headers(), json=component)
    assert resp.status_code == 201, resp.text
    return resp.headers["location"].rsplit("/", 1)[-1]


def _ldap_provider_config(directory: _Ldap, people_dn: str) -> dict[str, list[str]]:
    """The LDAP user federation, read-only, for an OpenLDAP ("Other") directory."""
    config = {
        "enabled": "true",
        "priority": "0",
        "vendor": "other",
        "connectionUrl": f"ldap://{_LDAP_HOST}:389",
        "authType": "simple",
        "bindDn": _ADMIN_DN,
        "bindCredential": directory.admin_password,
        "usersDn": people_dn,
        "usernameLDAPAttribute": "uid",
        "rdnLDAPAttribute": "uid",
        "uuidLDAPAttribute": "entryUUID",
        "userObjectClasses": "inetOrgPerson, organizationalPerson",
        "searchScope": "1",
        "editMode": "READ_ONLY",
        "importEnabled": "true",
        "syncRegistrations": "false",
        "trustEmail": "true",
        "pagination": "false",
        "fullSyncPeriod": "-1",
        "changedSyncPeriod": "-1",
        # Keycloak's user cache keeps a READ_ONLY person's LDAP groups: with
        # DEFAULT, a removal in the directory is not seen until the cache
        # entry is evicted (test_a_cache_policy_decides_when_a_removal_counts).
        "cachePolicy": "NO_CACHE",
        "useTruststoreSpi": "never",
        "connectionPooling": "false",
        "validatePasswordPolicy": "false",
    }
    return {key: [value] for key, value in config.items()}


def _group_mapper_config(groups_dn: str, ldap_filter: str, path: str) -> dict[str, list[str]]:
    config = {
        "groups.dn": groups_dn,
        "group.name.ldap.attribute": "cn",
        "group.object.classes": "groupOfNames",
        "preserve.group.inheritance": "false",
        "ignore.missing.groups": "false",
        "membership.ldap.attribute": "member",
        "membership.attribute.type": "DN",
        "membership.user.ldap.attribute": "uid",
        "groups.ldap.filter": ldap_filter,
        "mode": "READ_ONLY",
        "user.roles.retrieve.strategy": "LOAD_GROUPS_BY_MEMBER_ATTRIBUTE",
        "memberof.ldap.attribute": "memberOf",
        "mapped.group.attributes": "",
        "drop.non.existing.groups.during.sync": "false",
        "groups.path": path,
    }
    return {key: [value] for key, value in config.items()}


def _sync_groups(kc: _OurSite, provider_id: str, mapper_id: str) -> dict[str, Any]:
    resp = httpx.post(
        f"{kc.admin_url}/user-storage/{provider_id}/mappers/{mapper_id}/sync",
        headers=kc.master_headers(),
        params={"direction": "fedToKeycloak"},
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _sync_users(kc: _OurSite, provider_id: str) -> dict[str, Any]:
    resp = httpx.post(
        f"{kc.admin_url}/user-storage/{provider_id}/sync",
        headers=kc.master_headers(),
        params={"action": "triggerFullSync"},
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


@dataclass
class _Federation:
    provider_id: str
    mapper_ids: dict[str, str]  # mapper name -> component id


@pytest.fixture(scope="module")
def same_names(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    baseline: dict[str, dict[str, list[str]]],
) -> _Federation:
    """A: directory groups named like ours, one mapper onto the top level."""
    provider_id = _add_component(
        site,
        {
            "name": "directory-same-names",
            "providerId": "ldap",
            "providerType": _LDAP_COMPONENT,
            "parentId": _realm_id(site),
            "config": _ldap_provider_config(ldap, _PEOPLE_A),
        },
    )
    ldap_filter = "(|" + "".join(f"(cn={name})" for name in _MEHO_GROUPS) + ")"
    mapper_id = _add_component(
        site,
        {
            "name": "meho-role-groups",
            "providerId": "group-ldap-mapper",
            "providerType": _MAPPER_COMPONENT,
            "parentId": provider_id,
            "config": _group_mapper_config(_GROUPS_A, ldap_filter, "/"),
        },
    )
    return _Federation(provider_id, {"meho-role-groups": mapper_id})


@pytest.fixture(scope="module")
def renamed(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    baseline: dict[str, dict[str, list[str]]],
) -> _Federation:
    """B: directory groups with other names, one mapper each, under our group."""
    provider_id = _add_component(
        site,
        {
            "name": "directory-renamed",
            "providerId": "ldap",
            "providerType": _LDAP_COMPONENT,
            "parentId": _realm_id(site),
            "config": _ldap_provider_config(ldap, _PEOPLE_B),
        },
    )
    mapper_ids = {}
    for directory_group, meho_group in _RENAMED.items():
        name = f"{directory_group}-under-{meho_group}"
        mapper_ids[name] = _add_component(
            site,
            {
                "name": name,
                "providerId": "group-ldap-mapper",
                "providerType": _MAPPER_COMPONENT,
                "parentId": provider_id,
                "config": _group_mapper_config(
                    _GROUPS_B, f"(cn={directory_group})", f"/{meho_group}"
                ),
            },
        )
    return _Federation(provider_id, mapper_ids)


async def _assert_refused(
    kc: _OurSite, access_token: str, detail: str, monkeypatch: pytest.MonkeyPatch
) -> str:
    with pytest.raises(HTTPException) as refused:
        await _verify(kc, access_token, monkeypatch)
    assert refused.value.status_code == 401
    assert str(refused.value.detail).startswith(detail), refused.value.detail
    return str(refused.value.detail)


def _refresh(kc: _OurSite, refresh_token: str) -> httpx.Response:
    return _token(
        kc,
        {
            "grant_type": "refresh_token",
            "client_id": _LOGIN_CLIENT,
            "refresh_token": refresh_token,
        },
    )


# --- A. Same names -------------------------------------------------------------------


def test_a_group_sync_matches_our_groups_by_name(
    site: _OurSite,  # noqa: F811 -- fixture
    same_names: _Federation,
    baseline: dict[str, dict[str, list[str]]],
) -> None:
    result = _sync_groups(site, same_names.provider_id, same_names.mapper_ids["meho-role-groups"])
    print(f"\n[A] group sync: {result}")
    assert result.get("added", 0) == 0, result
    _assert_our_groups_intact(site, baseline, foreign={"staff"})
    # A second sync changes nothing either.
    _sync_groups(site, same_names.provider_id, same_names.mapper_ids["meho-role-groups"])
    print(f"[A] user sync: {_sync_users(site, same_names.provider_id)}")
    _assert_our_groups_intact(site, baseline, foreign={"staff"})


@pytest.mark.parametrize("group", _MEHO_GROUPS)
async def test_a_directory_member_gets_the_groups_tenant_and_role(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    same_names: _Federation,
    baseline: dict[str, dict[str, list[str]]],
    group: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    want_role, want_approver = _ROLE_GROUPS[group]
    username = f"a-{group.lower()}"
    password = ldap.add_person(_PEOPLE_A, username, _GROUPS_A, [group, "staff"])

    access_token = _password_login(site, username, password)["access_token"]
    claims = _claims(access_token)
    print(f"\n[A] {username}: groups claim {claims.get('groups')}")
    assert claims.get("tenant_id") == _TENANT_ID
    assert claims.get("tenant_role") == want_role.value
    if want_approver:
        assert claims.get("approver") is True
    else:
        assert "approver" not in claims
    assert claims.get("groups") == [group]
    assert _user_groups(site, username) == {group}
    _assert_our_groups_intact(site, baseline, foreign={"staff"})

    operator = await _verify(site, access_token, monkeypatch)
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is want_role
    assert operator.approver is want_approver
    assert operator.platform_admin is False


async def test_a_two_directory_groups_are_refused(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    same_names: _Federation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "a-two-groups"
    password = ldap.add_person(_PEOPLE_A, username, _GROUPS_A, ["MEHO-Operators", "MEHO-Admins"])
    access_token = _password_login(site, username, password)["access_token"]
    assert sorted(_claims(access_token)["groups"]) == ["MEHO-Admins", "MEHO-Operators"]
    await _assert_refused(site, access_token, "multiple_role_groups", monkeypatch)


async def test_a_leaving_the_directory_group_removes_the_tenant(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    same_names: _Federation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "a-leaver"
    password = ldap.add_person(_PEOPLE_A, username, _GROUPS_A, ["MEHO-Operators"])
    before = _password_login(site, username, password)
    operator = await _verify(site, before["access_token"], monkeypatch)
    assert operator.tenant_role is TenantRole.OPERATOR

    ldap.set_member(_GROUPS_A, "MEHO-Operators", f"uid={username},{_PEOPLE_A}", member=False)

    # Refresh of the session from before the change.
    refreshed = _refresh(site, before["refresh_token"])
    print(f"\n[A] refresh after LDAP removal: {refreshed.status_code}")
    assert refreshed.status_code == 200, refreshed.text
    refreshed_claims = _claims(refreshed.json()["access_token"])
    print(f"[A] refreshed claims: { {c: refreshed_claims.get(c) for c in _TENANT_CLAIMS} }")
    for claim in _TENANT_CLAIMS:
        assert claim not in refreshed_claims, claim
    await _assert_refused(
        site, refreshed.json()["access_token"], "missing_tenant_claim", monkeypatch
    )

    after = _password_login(site, username, password)
    claims = _claims(after["access_token"])
    for claim in _TENANT_CLAIMS:
        assert claim not in claims, claim
    assert _user_groups(site, username) == set()
    await _assert_refused(site, after["access_token"], "missing_tenant_claim", monkeypatch)


def _set_cache_policy(kc: _OurSite, provider_id: str, **config: str) -> None:
    url = f"{kc.admin_url}/components/{provider_id}"
    component = httpx.get(url, headers=kc.master_headers()).json()
    for key in ("cachePolicy", "maxLifespan"):
        component["config"].pop(key, None)
    component["config"].update({key: [value] for key, value in config.items()})
    # bindCredential comes back masked; Keycloak keeps the stored one.
    httpx.put(url, headers=kc.master_headers(), json=component).raise_for_status()


def _tenant_after_login(kc: _OurSite, username: str, password: str) -> str | None:
    tenant = _claims(_password_login(kc, username, password)["access_token"]).get("tenant_id")
    return None if tenant is None else str(tenant)


def test_a_cache_policy_decides_when_a_removal_counts(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    same_names: _Federation,
) -> None:
    """With Keycloak's user cache on, the LDAP groups of a login are cached too."""
    provider = same_names.provider_id
    try:
        # DEFAULT: the old membership stays until the cache entry goes.
        _set_cache_policy(site, provider, cachePolicy="DEFAULT")
        username = "a-cached"
        password = ldap.add_person(_PEOPLE_A, username, _GROUPS_A, ["MEHO-Operators"])
        assert _tenant_after_login(site, username, password) == _TENANT_ID
        ldap.set_member(_GROUPS_A, "MEHO-Operators", f"uid={username},{_PEOPLE_A}", member=False)
        assert _tenant_after_login(site, username, password) == _TENANT_ID, "cached"
        assert _user_groups(site, username) == {"MEHO-Operators"}
        httpx.post(
            f"{site.admin_url}/clear-user-cache", headers=site.master_headers()
        ).raise_for_status()
        assert _tenant_after_login(site, username, password) is None

        # MAX_LIFESPAN: the old membership stays at most that long.
        _set_cache_policy(site, provider, cachePolicy="MAX_LIFESPAN", maxLifespan="4000")
        username = "a-lifespan"
        password = ldap.add_person(_PEOPLE_A, username, _GROUPS_A, ["MEHO-Operators"])
        assert _tenant_after_login(site, username, password) == _TENANT_ID
        ldap.set_member(_GROUPS_A, "MEHO-Operators", f"uid={username},{_PEOPLE_A}", member=False)
        stale = _tenant_after_login(site, username, password)
        print(f"\n[A] MAX_LIFESPAN 4s, login right after the removal: tenant_id={stale}")
        time.sleep(5)
        assert _tenant_after_login(site, username, password) is None
    finally:
        _set_cache_policy(site, provider, cachePolicy="NO_CACHE")


# --- B. Different names ------------------------------------------------------------


def test_b_group_sync_puts_directory_groups_under_ours(
    site: _OurSite,  # noqa: F811 -- fixture
    renamed: _Federation,
    baseline: dict[str, dict[str, list[str]]],
) -> None:
    for name, mapper_id in renamed.mapper_ids.items():
        print(f"\n[B] group sync {name}: {_sync_groups(site, renamed.provider_id, mapper_id)}")
    print(f"[B] user sync: {_sync_users(site, renamed.provider_id)}")

    groups = _assert_our_groups_intact(site, baseline, foreign={*_RENAMED, "staff"})
    for directory_group, meho_group in _RENAMED.items():
        children = _children(site, groups[meho_group]["id"])
        assert [c["name"] for c in children] == [directory_group], meho_group
        assert children[0]["path"] == f"/{meho_group}/{directory_group}"
        assert not children[0].get("attributes"), children[0]

    # Again: still one subgroup each.
    for mapper_id in renamed.mapper_ids.values():
        _sync_groups(site, renamed.provider_id, mapper_id)
    groups = _assert_our_groups_intact(site, baseline, foreign={*_RENAMED, "staff"})
    for directory_group, meho_group in _RENAMED.items():
        assert [c["name"] for c in _children(site, groups[meho_group]["id"])] == [directory_group]


@pytest.mark.parametrize("group", _MEHO_GROUPS)
async def test_b_directory_member_gets_the_parent_groups_tenant_and_role(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    renamed: _Federation,
    baseline: dict[str, dict[str, list[str]]],
    group: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    want_role, want_approver = _ROLE_GROUPS[group]
    directory_group = _DIRECTORY_GROUP[group]
    username = f"b-{directory_group.lower()}"
    password = ldap.add_person(_PEOPLE_B, username, _GROUPS_B, [directory_group, "staff"])

    access_token = _password_login(site, username, password)["access_token"]
    claims = _claims(access_token)
    print(f"\n[B] {username}: groups claim {claims.get('groups')}")
    assert claims.get("tenant_id") == _TENANT_ID
    assert claims.get("tenant_role") == want_role.value
    if want_approver:
        assert claims.get("approver") is True
    else:
        assert "approver" not in claims
    # full.path=false: the subgroup's own name, not its parent's.
    assert claims.get("groups") == [directory_group]
    assert _user_groups(site, username) == {directory_group}
    _assert_our_groups_intact(site, baseline, foreign={*_RENAMED, "staff"})

    operator = await _verify(site, access_token, monkeypatch)
    assert str(operator.tenant_id) == _TENANT_ID
    assert operator.tenant_role is want_role
    assert operator.approver is want_approver
    assert operator.platform_admin is False


async def test_b_two_directory_groups_need_the_sg_names_in_jwt_role_groups(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    renamed: _Federation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "b-two-groups"
    password = ldap.add_person(_PEOPLE_B, username, _GROUPS_B, ["SG-MEHO-Ops", "SG-MEHO-Admin"])
    access_token = _password_login(site, username, password)["access_token"]
    claims = _claims(access_token)
    print(
        f"\n[B] two groups: groups claim {claims.get('groups')}, "
        f"tenant_role {claims.get('tenant_role')}"
    )
    assert sorted(claims["groups"]) == ["SG-MEHO-Admin", "SG-MEHO-Ops"]
    assert claims.get("tenant_role") in {"operator", "tenant_admin"}

    # Default JWT_ROLE_GROUPS (the five MEHO-* names): the claim names none
    # of them, so the one-group check does not see the conflict.
    monkeypatch.delenv("JWT_ROLE_GROUPS", raising=False)
    operator = await _verify(site, access_token, monkeypatch)
    print(f"[B] two groups, default JWT_ROLE_GROUPS: accepted as {operator.tenant_role.value}")
    assert operator.tenant_role.value == claims["tenant_role"]

    # With the directory names listed too, MEHO refuses it.
    monkeypatch.setenv("JWT_ROLE_GROUPS", ",".join([*_MEHO_GROUPS, *_RENAMED]))
    detail = await _assert_refused(site, access_token, "multiple_role_groups", monkeypatch)
    print(f"[B] two groups, JWT_ROLE_GROUPS with SG-*: {detail}")
    assert "SG-MEHO-Ops" in detail
    assert "SG-MEHO-Admin" in detail

    # A member of one SG group still passes with the longer list.
    single = ldap.add_person(_PEOPLE_B, "b-one-group", _GROUPS_B, ["SG-MEHO-Ops"])
    one_token = _password_login(site, "b-one-group", single)["access_token"]
    assert (await _verify(site, one_token, monkeypatch)).tenant_role is TenantRole.OPERATOR


async def test_b_leaving_the_directory_group_removes_the_tenant(
    site: _OurSite,  # noqa: F811 -- fixture
    ldap: _Ldap,
    renamed: _Federation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "b-leaver"
    password = ldap.add_person(_PEOPLE_B, username, _GROUPS_B, ["SG-MEHO-Ops"])
    before = _password_login(site, username, password)
    operator = await _verify(site, before["access_token"], monkeypatch)
    assert operator.tenant_role is TenantRole.OPERATOR

    ldap.set_member(_GROUPS_B, "SG-MEHO-Ops", f"uid={username},{_PEOPLE_B}", member=False)

    refreshed = _refresh(site, before["refresh_token"])
    print(f"\n[B] refresh after LDAP removal: {refreshed.status_code}")
    assert refreshed.status_code == 200, refreshed.text
    refreshed_claims = _claims(refreshed.json()["access_token"])
    for claim in _TENANT_CLAIMS:
        assert claim not in refreshed_claims, claim
    await _assert_refused(
        site, refreshed.json()["access_token"], "missing_tenant_claim", monkeypatch
    )

    after = _password_login(site, username, password)
    claims = _claims(after["access_token"])
    for claim in _TENANT_CLAIMS:
        assert claim not in claims, claim
    assert _user_groups(site, username) == set()
    await _assert_refused(site, after["access_token"], "missing_tenant_claim", monkeypatch)
