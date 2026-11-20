# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approver routing with the old group kinds ``SA-Operators`` and ``Approvers``.

An old kind in a routing entry, in a policy file or stored on a waiting
request, matches only the members of that old login group, by the group in
the token. It never matches the whole new role.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.auth.current_tenant import with_membership
from meho_backplane.auth.jwt import _operator_from_claims
from meho_backplane.auth.operator import Operator, RoleGroup, TenantMembership
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.policy.routing import (
    DEFAULT_RULE,
    in_kinds,
    narrow,
    route,
    routes_from_documents,
)
from meho_backplane.policy.routing_floor import wider_findings
from meho_backplane.settings import get_settings
from tests.test_approval_group_rules import _OTHER
from tests.test_approval_rights import _row

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OTHER_TENANT = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    for name in ("JWT_SELF_APPROVER_GROUP", "JWT_TENANTS_CLAIM_NAME", "JWT_ROLE_GROUPS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login(claims: dict[str, Any]) -> Operator:
    return _operator_from_claims({"sub": _OTHER, **claims}, "raw.jwt.token", get_settings())


def _flat(role: str, group: str) -> Operator:
    return _login({"tenant_id": str(_TENANT), "tenant_role": role, "groups": [group]})


def _listed(group: str) -> Operator:
    return _login({"meho_tenants": [f"{_TENANT}:lab-a:{group}"]})


#: People by how they sign in.
_PEOPLE: dict[str, Any] = {
    "operator, tenant list": lambda: _listed("Operators"),
    "operator, groups claim": lambda: _flat("operator", "MEHO-Operators"),
    "sa member, tenant list": lambda: _listed("SA-Operators"),
    "sa member, groups claim": lambda: _flat("operator", "MEHO-SA-Operators"),
    "sa member, groups claim with a slash": lambda: _flat("operator", "/meho-sa-operators"),
    "approvers member, tenant list": lambda: _listed("Approvers"),
    "approvers member, groups claim": lambda: _flat("read_only", "MEHO-Approvers"),
    "admin, tenant list": lambda: _listed("Admins"),
    "reader, tenant list": lambda: _listed("Readers"),
}


def _waiting(level: str, kinds: list[str], rule: str = "tenant:routing[0]") -> ApprovalRequest:
    row = _row(level)
    row.approver_roles = kinds
    row.approver_rule = rule
    return row


def _may(person: str, level: str, kinds: list[str]) -> bool:
    return approve_right(_PEOPLE[person](), _waiting(level, kinds)).may_approve


@pytest.mark.parametrize("person", ["operator, tenant list", "operator, groups claim"])
def test_a_plain_operator_cannot_approve_where_the_entry_names_sa_operators(person: str) -> None:
    assert _may(person, "caution", ["SA-Operators", "Admins"]) is False


@pytest.mark.parametrize(
    "person",
    [
        "sa member, tenant list",
        "sa member, groups claim",
        "sa member, groups claim with a slash",
    ],
)
def test_a_member_of_the_old_group_still_approves_there(person: str) -> None:
    assert _may(person, "caution", ["SA-Operators", "Admins"]) is True


@pytest.mark.parametrize("person", ["sa member, tenant list", "sa member, groups claim"])
def test_an_old_kind_gives_no_high_risk_right(person: str) -> None:
    assert _may(person, "dangerous", ["SA-Operators", "Admins"]) is False


@pytest.mark.parametrize(
    "person", ["approvers member, tenant list", "approvers member, groups claim"]
)
def test_the_old_approvers_kind_lets_nobody_approve(person: str) -> None:
    assert _may(person, "caution", ["Approvers"]) is False
    assert _may("operator, tenant list", "caution", ["Approvers"]) is False


def test_a_sub_group_of_the_old_group_does_not_match() -> None:
    operator = _flat("operator", "/MEHO-SA-Operators/team-a")

    assert operator.old_group is None
    assert in_kinds(operator, ("SA-Operators",)) is False


def test_the_old_group_of_another_tenant_does_not_match() -> None:
    operator = _login(
        {
            "meho_tenants": [
                f"{_TENANT}:lab-a:Operators",
                f"{_OTHER_TENANT}:lab-b:SA-Operators",
            ]
        }
    )

    assert in_kinds(operator, ("SA-Operators",)) is False
    other = with_membership(
        operator,
        TenantMembership(tenant_id=_OTHER_TENANT, name="lab-b", group=RoleGroup.SA_OPERATORS),
    )
    assert in_kinds(other, ("SA-Operators",)) is True


def test_a_machine_login_has_no_old_group() -> None:
    agent = _login(
        {
            "tenant_id": str(_TENANT),
            "tenant_role": "operator",
            "principal_kind": "agent",
            "groups": ["MEHO-SA-Operators"],
        }
    )

    assert agent.old_group is None


@pytest.mark.parametrize(
    ("stored", "plain_operator", "sa_member"),
    [
        # The default kinds MEHO stored before: every Operator still approves.
        (["Readers", "Operators", "SA-Operators", "Admins", "Approvers"], True, True),
        (["Operators", "SA-Operators", "Admins", "Approvers"], True, True),
        # An entry that named SA-Operators: only the members of the old group.
        (["SA-Operators", "Admins"], False, True),
        (["SA-Operators"], False, True),
        (["Admins"], False, False),
    ],
)
def test_a_waiting_request_keeps_its_stored_kinds(
    stored: list[str], plain_operator: bool, sa_member: bool
) -> None:
    assert _may("operator, tenant list", "caution", stored) is plain_operator
    assert _may("sa member, groups claim", "caution", stored) is sa_member
    assert _may("admin, tenant list", "caution", stored) is ("Admins" in stored)


def _route(document: dict[str, Any], level: str, *, install: dict[str, Any] | None = None) -> Any:
    return route(
        routes_from_documents(install, document),
        category="network",
        install_category="network",
        connector_id="pfsense-ssh-1",
        level=level,
        op_id="demo.change",
    )


def test_a_routing_entry_stores_the_old_kind_as_written() -> None:
    document = {
        "routing": [{"match": {"category": "network"}, "approvers": ["SA-Operators", "Admins"]}]
    }

    assert _route(document, "caution").kinds == ("SA-Operators", "Admins")
    assert _route(document, "dangerous").kinds == ("Admins",)


def test_the_old_approvers_kind_is_never_stored() -> None:
    document = {
        "routing": [{"match": {"category": "network"}, "approvers": ["Approvers", "Admins"]}]
    }

    assert _route(document, "caution").kinds == ("Admins",)


@pytest.mark.parametrize(
    ("install", "tenant", "expected"),
    [
        (["Operators", "Admins"], ["SA-Operators"], ("SA-Operators",)),
        (["SA-Operators", "Admins"], ["Operators", "Admins"], ("SA-Operators", "Admins")),
        (["SA-Operators"], ["Admins"], ()),
    ],
)
def test_the_install_floor_and_a_tenant_entry_take_the_narrower_kind(
    install: list[str], tenant: list[str], expected: tuple[str, ...]
) -> None:
    def doc(kinds: list[str]) -> dict[str, Any]:
        return {"routing": [{"match": {"category": "network"}, "approvers": kinds}]}

    assert _route(doc(tenant), "caution", install=doc(install)).kinds == expected


def test_an_old_kind_is_narrower_than_its_new_kind() -> None:
    assert narrow(("Operators", "Admins"), ("SA-Operators",)) == ("SA-Operators",)
    assert narrow(("SA-Operators",), ("Operators",)) == ("SA-Operators",)
    assert narrow(("Readers",), ("Approvers",)) == ("Approvers",)
    assert narrow(("Approvers",), ("Operators",)) == ()


def test_a_tenant_entry_with_the_old_kind_does_not_widen_the_install_floor() -> None:
    def entries(scope: str, kinds: list[str]) -> Any:
        document = {"routing": [{"match": {"category": "network"}, "approvers": kinds}]}
        if scope == "install":
            return routes_from_documents(document, None)
        return routes_from_documents(None, document)

    narrower = wider_findings(
        entries("tenant", ["SA-Operators"]), entries("install", ["Operators"])
    )
    wider = wider_findings(entries("tenant", ["Operators"]), entries("install", ["SA-Operators"]))

    assert narrower == []
    assert len(wider) == 1


#: The routing section of the "separated duties" file as it shipped with
#: ``SA-Operators``. A site may have it stored.
_OLD_SEPARATED_DUTIES: dict[str, Any] = {
    "routing": [
        {"match": {"category": "identity"}, "approvers": ["Admins"]},
        {"match": {"category": "secrets"}, "approvers": ["Admins"]},
        {"match": {"category": "network"}, "approvers": ["SA-Operators", "Admins"]},
        {"match": {"category": "storage"}, "approvers": ["SA-Operators", "Admins"]},
    ]
}

_SHIPPED = (
    Path(__file__).resolve().parents[2] / "deploy" / "policy-profiles" / "separated-duties.yaml"
)

#: Who may approve another person's change, by category and risk level,
#: with the old stored file. The same answers as before, but for the two
#: rights the owner took away from MEHO-SA-Operators: a ``dangerous`` or
#: ``destructive`` change of another person needs an Admin.
_EXPECTED: dict[tuple[str, str], set[str]] = {
    ("identity", "caution"): {"admin"},
    ("identity", "dangerous"): {"admin"},
    ("secrets", "caution"): {"admin"},
    ("secrets", "dangerous"): {"admin"},
    ("network", "caution"): {"sa member", "admin"},
    ("network", "dangerous"): {"admin"},
    ("storage", "caution"): {"sa member", "admin"},
    ("storage", "dangerous"): {"admin"},
    ("compute", "caution"): {"operator", "sa member", "admin"},
    ("compute", "dangerous"): {"admin"},
}

_KINDS_OF_PEOPLE = {
    "operator": "operator, groups claim",
    "sa member": "sa member, groups claim",
    "approvers member": "approvers member, groups claim",
    "reader": "reader, tenant list",
    "admin": "admin, tenant list",
}


def _who_approves(document: dict[str, Any], category: str, level: str) -> set[str]:
    found = route(
        routes_from_documents(None, document),
        category=category,
        install_category=category,
        connector_id="demo",
        level=level,
        op_id="demo.change",
    )
    row = _waiting(level, list(found.kinds), found.rule or DEFAULT_RULE)
    return {
        name
        for name, person in _KINDS_OF_PEOPLE.items()
        if approve_right(_PEOPLE[person](), row).may_approve
    }


@pytest.mark.parametrize(("category", "level"), list(_EXPECTED))
def test_the_old_separated_duties_file_gives_the_same_answers(category: str, level: str) -> None:
    assert _who_approves(_OLD_SEPARATED_DUTIES, category, level) == _EXPECTED[(category, level)]


@pytest.mark.parametrize(("category", "level"), list(_EXPECTED))
def test_the_shipped_separated_duties_file_gives_no_one_a_new_right(
    category: str, level: str
) -> None:
    shipped = yaml.safe_load(_SHIPPED.read_text())

    assert _who_approves(shipped, category, level) <= _EXPECTED[(category, level)]
