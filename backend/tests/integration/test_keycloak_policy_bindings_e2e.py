# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The token claims that bindings match, on the Keycloak MEHO ships.

A binding of the policy file matches a login group by its full path (the
``groups`` claim with **Full group path** on) or by its group attribute
(the ``meho_groups`` claim of the ``meho-groups`` mapper).
``docs-site/install/keycloak-realm.md`` ("Team groups for the policy file")
says how to set both up. This test does exactly that on the realm of the
Keycloak subchart (the same site as ``test_keycloak_role_groups_e2e.py``)
and checks that:

1. a person only in the sub-group ``/MEHO-Operators/Network`` gets the
   claims the docs describe: the full path in ``groups``, the group
   attribute in ``meho_groups``, and the tenant and role of the parent
   group;
2. MEHO reads them as :func:`~meho_backplane.policy.bindings.token_groups`
   expects, and a binding by full path and one by attribute both give
   their kind.

It skips when Docker is missing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import RoleGroup, TenantRole
from meho_backplane.policy.bindings import bind, bindings_from_document, token_groups
from tests.integration import test_keycloak_role_groups_e2e as role_groups

# The site of the role groups test: the subchart's realm and the bootstrap stage.
site = role_groups.site

_ATTRIBUTE = "tenant-a-network"


def _cli_client(site: role_groups._Site) -> dict[str, Any]:
    clients = httpx.get(
        f"{site.admin_url}/clients",
        headers=site.headers(),
        params={"clientId": role_groups._LOGIN_CLIENT},
    ).json()
    return dict(clients[0])


def _set_up_team_groups(site: role_groups._Site) -> str:
    """The docs' steps: full group paths, the ``meho-groups`` mapper, a team group.

    Returns the id of the new sub-group ``/MEHO-Operators/Network``.
    """
    client = _cli_client(site)
    mappers_url = f"{site.admin_url}/clients/{client['id']}/protocol-mappers/models"
    mappers = httpx.get(mappers_url, headers=site.headers()).json()
    groups_claim = next(m for m in mappers if m["name"] == "groups-claim")
    groups_claim["config"]["full.path"] = "true"
    httpx.put(
        f"{mappers_url}/{groups_claim['id']}", headers=site.headers(), json=groups_claim
    ).raise_for_status()
    httpx.post(
        mappers_url,
        headers=site.headers(),
        json={
            "name": "meho-groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                "user.attribute": "meho_group",
                "claim.name": "meho_groups",
                "jsonType.label": "String",
                "multivalued": "true",
                "aggregate.attrs": "true",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "userinfo.token.claim": "false",
            },
        },
    ).raise_for_status()

    parent = role_groups._group_id(site, "MEHO-Operators")
    httpx.post(
        f"{site.admin_url}/groups/{parent}/children",
        headers=site.headers(),
        json={"name": "Network", "attributes": {"meho_group": [_ATTRIBUTE]}},
    ).raise_for_status()
    children = httpx.get(
        f"{site.admin_url}/groups/{parent}/children", headers=site.headers()
    ).json()
    return str(next(g["id"] for g in children if g["name"] == "Network"))


async def test_a_person_only_in_a_sub_group_gets_the_claims_the_docs_describe(
    site: role_groups._Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    network = _set_up_team_groups(site)
    username = "network-member"
    _, password = role_groups._add_person(site, username, "MEHO-Readers")
    user_id = role_groups._user_id(site, username)
    readers = role_groups._group_id(site, "MEHO-Readers")
    httpx.delete(
        f"{site.admin_url}/users/{user_id}/groups/{readers}", headers=site.headers()
    ).raise_for_status()
    httpx.put(
        f"{site.admin_url}/users/{user_id}/groups/{network}", headers=site.headers()
    ).raise_for_status()
    assert role_groups._user_groups(site, username) == {"Network"}

    access_token = role_groups._password_login(site, username, password)["access_token"]
    claims = role_groups._claims(access_token)
    assert claims.get("groups") == ["/MEHO-Operators/Network"]
    assert claims.get("meho_groups") == [_ATTRIBUTE]
    # The tenant and the role come from the parent group.
    assert claims.get("tenant_id") == role_groups._TENANT_ID
    assert claims.get("tenant_role") == "operator"
    assert claims.get("meho_tenants") == [f"{role_groups._TENANT_ID}:main:Operators"]

    assert token_groups(claims) == (
        frozenset({"/MEHO-Operators/Network"}),
        frozenset({_ATTRIBUTE}),
    )
    operator = await role_groups._verify(site, access_token, monkeypatch)
    assert operator.tenant_role is TenantRole.OPERATOR
    for match in ({"group": "/MEHO-Operators/Network"}, {"attribute": _ATTRIBUTE}):
        entries = bindings_from_document(
            {"bindings": [{"match": match, "kind": "SA-Operators", "categories": ["network"]}]}
        )
        bound = bind(operator, claims, entries)
        assert bound.bound_kind is RoleGroup.SA_OPERATORS, match
        assert bound.bound_categories == frozenset({"network"})
    short = {"match": {"group": "/Network"}, "kind": "SA-Operators", "categories": ["*"]}
    assert bind(operator, claims, bindings_from_document({"bindings": [short]})).bound_kind is None
