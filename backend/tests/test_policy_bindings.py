# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bindings in the policy file (``POLICY_APPROVER_ROUTING``).

A binding gives a login group, matched by its full path or its group
attribute, one of the five group kinds and a set of categories. A short
group name never matches. See :mod:`meho_backplane.policy.bindings`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from structlog.testing import capture_logs

from meho_backplane.auth import jwt as jwt_module
from meho_backplane.auth.operator import Operator, PrincipalKind, RoleGroup, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.policy import bindings as bindings_module
from meho_backplane.policy.bindings import (
    BindingEntry,
    BoundKindConflictError,
    bind,
    bind_current_tenant,
    bindings_from_document,
    shipped_binding_documents,
    shipped_bindings,
    token_groups,
)
from meho_backplane.policy.document import canonical_sha256, document_of, parse_policy_file
from meho_backplane.policy.service import _bindings_diff, export_policy
from meho_backplane.policy.validate import check_document
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006048")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("JWT_ROLE_GROUPS", raising=False)
    monkeypatch.delenv("JWT_SELF_APPROVER_GROUP", raising=False)
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "false")
    get_settings.cache_clear()


def _operator(**extra: Any) -> Operator:
    values: dict[str, Any] = {
        "sub": "person-1",
        "raw_jwt": "raw.jwt.token",
        "tenant_id": _TENANT,
        "tenant_role": TenantRole.OPERATOR,
    }
    values.update(extra)
    return Operator(**values)


def _entry(
    kind: RoleGroup,
    categories: set[str],
    *,
    group: str | None = None,
    attribute: str | None = None,
    principal: str = "person",
) -> BindingEntry:
    return BindingEntry(
        ref="tenant:bindings[0]",
        kind=kind,
        categories=frozenset(categories),
        group=group,
        attribute=attribute,
        principal=principal,
    )


_NETWORK = _entry(RoleGroup.SA_OPERATORS, {"network"}, group="/MEHO-Operators/Network")


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def _parse(text: str) -> list[str]:
    return parse_policy_file(text.encode()).mistakes


def test_a_file_with_bindings_parses_and_stores_them() -> None:
    parsed = parse_policy_file(
        b"policy: v1\n"
        b"bindings:\n"
        b"  - match: {group: /MEHO-Operators/Network}\n"
        b"    kind: SA-Operators\n"
        b"    categories: [network]\n"
        b"  - match: {attribute: tenant-a-storage}\n"
        b"    kind: Readers\n"
        b"    categories: ['*']\n"
        b"    principal: agent\n"
    )
    assert parsed.mistakes == []
    assert parsed.model is not None
    assert document_of(parsed.model)["bindings"] == [
        {
            "match": {"group": "/MEHO-Operators/Network"},
            "kind": "SA-Operators",
            "categories": ["network"],
        },
        {
            "match": {"attribute": "tenant-a-storage"},
            "kind": "Readers",
            "categories": ["*"],
            "principal": "agent",
        },
    ]


@pytest.mark.parametrize(
    ("binding", "sentence"),
    [
        (
            "{match: {group: MEHO-Operators}, kind: Operators, categories: [network]}",
            "`bindings[0].match.group` must be the full path of the group, for example "
            "/MEHO-Operators/Network; a short group name never matches.",
        ),
        (
            "{match: {group: /MEHO-Operators/}, kind: Operators, categories: [network]}",
            "`bindings[0].match.group` must be the full path of the group, for example "
            "/MEHO-Operators/Network; a short group name never matches.",
        ),
        (
            "{match: {}, kind: Operators, categories: [network]}",
            "`bindings[0].match`: needs exactly one of group (the full path) or attribute.",
        ),
        (
            "{match: {group: /A, attribute: a}, kind: Operators, categories: [network]}",
            "`bindings[0].match`: needs exactly one of group (the full path) or attribute.",
        ),
        (
            "{match: {group: /A}, kind: operators, categories: [network]}",
            "`bindings[0].kind` is not a group kind; use Readers, Operators, SA-Operators, "
            "Admins, Approvers.",
        ),
        (
            "{match: {group: /A}, kind: Operators, categories: ['*', network]}",
            '`bindings[0].categories`: "*" means every category; list it alone.',
        ),
        (
            "{match: {group: /A}, kind: Operators, categories: [network, network]}",
            "`bindings[0].categories`: lists a category twice.",
        ),
        (
            "{match: {group: /A}, kind: Operators, categories: []}",
            None,
        ),
        (
            "{match: {group: /A}, kind: Operators, categories: [n], principal: robot}",
            "`bindings[0].principal` is not a kind of login; use person, agent, service, runner.",
        ),
    ],
)
def test_each_binding_mistake_is_one_sentence(binding: str, sentence: str | None) -> None:
    mistakes = _parse(f"policy: v1\nbindings:\n  - {binding}\n")
    assert len(mistakes) == 1
    if sentence is not None:
        assert mistakes == [sentence]
    else:
        assert mistakes[0].startswith("`bindings[0].categories`")


def test_a_repeated_binding_is_a_mistake() -> None:
    mistakes = _parse(
        "policy: v1\nbindings:\n"
        "  - {match: {group: /A}, kind: Operators, categories: [network]}\n"
        "  - {match: {group: /A}, kind: Readers, categories: [storage]}\n"
    )
    assert mistakes == ["`bindings[1].match` repeats `bindings[0]`; list each group once."]


def test_the_order_of_the_bindings_does_not_count() -> None:
    one = parse_policy_file(
        b"policy: v1\nbindings:\n"
        b"  - {match: {group: /A}, kind: Operators, categories: [network]}\n"
        b"  - {match: {group: /B}, kind: Operators, categories: [storage]}\n"
    ).model
    two = parse_policy_file(
        b"policy: v1\nbindings:\n"
        b"  - {match: {group: /B}, kind: Operators, categories: [storage]}\n"
        b"  - {match: {group: /A}, kind: Operators, categories: [network]}\n"
    ).model
    assert one is not None and two is not None
    assert canonical_sha256(document_of(one)) == canonical_sha256(document_of(two))


# ---------------------------------------------------------------------------
# The import checks
# ---------------------------------------------------------------------------


def _check(bindings: list[dict[str, Any]], scope: Any = "tenant") -> list[str]:
    document = {"classifications": [], "bindings": bindings}
    return check_document(document, scope=scope, install_document=None, ops=[])[0]


def _binding(kind: str, **extra: Any) -> dict[str, Any]:
    return {"match": {"group": "/MEHO-Bots"}, "kind": kind, "categories": ["*"], **extra}


@pytest.mark.parametrize("principal", ["agent", "service"])
@pytest.mark.parametrize("kind", ["Operators", "SA-Operators", "Admins", "Approvers"])
def test_an_agent_or_service_with_a_kind_that_may_approve_is_refused(
    principal: str, kind: str
) -> None:
    assert _check([_binding(kind, principal=principal)]) == [
        "`bindings[0]` gives an agent or a service a group kind that may approve. Only a "
        "person approves, with a click in the web interface or a fresh sign-in."
    ]


@pytest.mark.parametrize("principal", ["agent", "service"])
def test_an_agent_or_service_may_be_a_reader(principal: str) -> None:
    assert _check([_binding("Readers", principal=principal)]) == []


@pytest.mark.parametrize("kind", ["Readers", "Operators", "Admins"])
def test_a_runner_gets_no_binding(kind: str) -> None:
    assert _check([_binding(kind, principal="runner")]) == [
        "`bindings[0]`: a runner cannot get a role. A runner only runs what MEHO has "
        "already decided."
    ]


def test_an_unknown_category_is_refused() -> None:
    mistakes = _check([{**_binding("Operators"), "categories": ["network", "team-x"]}])
    assert len(mistakes) == 1
    assert mistakes[0].startswith("`bindings[0].categories` `team-x` is not in the taxonomy (")


def test_a_category_of_the_tenant_file_is_known() -> None:
    document = {
        "taxonomy": {"categories": ["team-x"]},
        "classifications": [],
        "bindings": [{**_binding("Operators"), "categories": ["team-x"]}],
    }
    assert check_document(document, scope="tenant", install_document=None, ops=[])[0] == []


def test_the_install_file_holds_no_bindings() -> None:
    assert _check([_binding("Operators")], scope="install") == [
        "`bindings` belongs to a tenant's policy file. Each tenant binds its own login groups."
    ]


# ---------------------------------------------------------------------------
# Matching a login
# ---------------------------------------------------------------------------


def test_a_full_path_gives_the_kind_and_the_categories() -> None:
    operator = bind(_operator(), {"groups": ["/MEHO-Operators/Network"]}, [_NETWORK])
    assert operator.bound_kind is RoleGroup.SA_OPERATORS
    assert operator.bound_categories == frozenset({"network"})
    assert operator.self_approver is True
    assert operator.tenant_role is TenantRole.OPERATOR


@pytest.mark.parametrize(
    "groups",
    [
        ["Network"],
        ["/Network"],
        ["/Contractors/Network"],
        ["/Contractors/MEHO-Operators/Network"],
        ["/meho-operators/network"],
        ["MEHO-Operators/Network"],
    ],
)
def test_the_same_short_name_in_another_path_gets_no_kind_and_no_category(
    groups: list[str],
) -> None:
    before = _operator()
    after = bind(before, {"groups": groups}, [_NETWORK])
    assert after.bound_kind is None
    assert after.bound_categories == frozenset()
    assert after == before


def test_a_group_attribute_matches_exactly() -> None:
    entry = _entry(RoleGroup.OPERATORS, {"storage"}, attribute="tenant-a-storage")
    hit = bind(_operator(), {"meho_groups": ["tenant-a-storage", "other"]}, [entry])
    assert hit.bound_kind is RoleGroup.OPERATORS
    assert hit.bound_categories == frozenset({"storage"})
    for value in (["Tenant-A-Storage"], ["tenant-a"], "tenant-a-storagex", [None], 7):
        assert bind(_operator(), {"meho_groups": value}, [entry]).bound_kind is None


def test_a_single_attribute_value_counts() -> None:
    entry = _entry(RoleGroup.OPERATORS, {"storage"}, attribute="tenant-a-storage")
    assert bind(_operator(), {"meho_groups": "tenant-a-storage"}, [entry]).bound_kind is not None


def test_a_group_attribute_is_not_a_group_path() -> None:
    entry = _entry(RoleGroup.OPERATORS, {"storage"}, attribute="/MEHO-Operators/Storage")
    assert bind(_operator(), {"groups": ["/MEHO-Operators/Storage"]}, [entry]).bound_kind is None


def test_token_groups_keeps_only_full_paths() -> None:
    paths, attributes = token_groups({"groups": ["/A/B", "B", "/C"], "meho_groups": ["x", ""]})
    assert paths == frozenset({"/A/B", "/C"})
    assert attributes == frozenset({"x"})
    assert token_groups({}) == (frozenset(), frozenset())


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"groups": "/MEHO-Operators/Network"}, id="groups-one-text"),
        pytest.param({"groups": {"/MEHO-Operators/Network": True}}, id="groups-map"),
        pytest.param({"groups": True}, id="groups-true"),
        pytest.param({"groups": ["/MEHO-Operators/Network", 42]}, id="groups-number-after"),
        pytest.param({"groups": [None, "/MEHO-Operators/Network"]}, id="groups-none-before"),
        pytest.param({"groups": ["/MEHO-Operators/Network", ["/X"]]}, id="groups-nested"),
        pytest.param({"meho_groups": ["net", 4]}, id="attribute-number"),
        pytest.param({"meho_groups": {"net": True}}, id="attribute-map"),
    ],
)
def test_a_groups_claim_that_is_not_a_list_of_texts_matches_no_binding(
    claims: dict[str, Any],
) -> None:
    """Anything unclear gives no kind, the same rule as for self-approval today."""
    net = _entry(RoleGroup.OPERATORS, {"network"}, attribute="net")
    full = {"groups": ["/MEHO-Operators/Network"], "meho_groups": ["net"], **claims}
    with capture_logs() as logs:
        assert token_groups(full) == (frozenset(), frozenset())
        before = _operator(self_approver=True)
        assert bind(before, full, [_NETWORK, net]) == before
    assert "policy_bindings_malformed_groups_claim" in [e["event"] for e in logs]


def test_categories_add_up_and_star_means_every_category() -> None:
    storage = _entry(RoleGroup.SA_OPERATORS, {"storage"}, group="/MEHO-Operators/Storage")
    claims = {"groups": ["/MEHO-Operators/Network", "/MEHO-Operators/Storage"]}
    assert bind(_operator(), claims, [_NETWORK, storage]).bound_categories == frozenset(
        {"network", "storage"}
    )
    every = _entry(RoleGroup.SA_OPERATORS, {"*"}, group="/MEHO-Operators/Storage")
    assert bind(_operator(), claims, [_NETWORK, every]).bound_categories == frozenset({"*"})


def test_two_kinds_in_one_tenant_refuse_the_login() -> None:
    plain = _entry(RoleGroup.OPERATORS, {"storage"}, group="/MEHO-Operators/Storage")
    claims = {"groups": ["/MEHO-Operators/Network", "/MEHO-Operators/Storage"]}
    with pytest.raises(BoundKindConflictError) as caught:
        bind(_operator(), claims, [_NETWORK, plain])
    assert str(caught.value).startswith(
        "multiple_bound_kinds: this login matches bindings with 2 group kinds "
        "(Operators, SA-Operators) in this tenant."
    )


@pytest.mark.parametrize("role", [TenantRole.READ_ONLY, TenantRole.OPERATOR])
def test_kinds_of_two_base_levels_refuse_the_login(role: TenantRole) -> None:
    readers = _entry(RoleGroup.READERS, {"*"}, group="/MEHO-Readers/Storage")
    claims = {"groups": ["/MEHO-Operators/Network", "/MEHO-Readers/Storage"]}
    with pytest.raises(BoundKindConflictError) as caught:
        bind(_operator(tenant_role=role), claims, [_NETWORK, readers])
    assert caught.value.kinds == (RoleGroup.READERS, RoleGroup.SA_OPERATORS)


def test_a_kind_of_another_base_level_is_left_out() -> None:
    admins = _entry(RoleGroup.ADMINS, {"*"}, group="/MEHO-Operators/Network")
    before = _operator()
    after = bind(before, {"groups": ["/MEHO-Operators/Network"]}, [admins])
    assert after == before
    assert after.tenant_role is TenantRole.OPERATOR


def test_an_approver_binding_gives_the_approver_flag_and_a_reader_binding_takes_it() -> None:
    claims = {"groups": ["/Audit"]}
    reader = _operator(tenant_role=TenantRole.READ_ONLY)
    approvers = _entry(RoleGroup.APPROVERS, {"network"}, group="/Audit")
    assert bind(reader, claims, [approvers]).approver is True
    flagged = _operator(tenant_role=TenantRole.READ_ONLY, approver=True)
    readers = _entry(RoleGroup.READERS, {"network"}, group="/Audit")
    assert bind(flagged, claims, [readers]).approver is False


def test_an_operators_binding_takes_the_self_approval_flag() -> None:
    operator = _operator(self_approver=True)
    plain = _entry(RoleGroup.OPERATORS, {"network"}, group="/MEHO-Operators/Network")
    after = bind(operator, {"groups": ["/MEHO-Operators/Network"]}, [plain])
    assert after.self_approver is False
    assert after.may_self_approve is False


def test_a_bad_tenants_claim_keeps_the_self_approval_flag_off() -> None:
    operator = _operator(tenants_claim_malformed=True)
    after = bind(operator, {"groups": ["/MEHO-Operators/Network"]}, [_NETWORK])
    assert after.bound_kind is RoleGroup.SA_OPERATORS
    assert after.self_approver is False


@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
def test_a_person_binding_never_matches_a_machine(kind: PrincipalKind) -> None:
    machine = _operator(principal_kind=kind)
    assert bind(machine, {"groups": ["/MEHO-Operators/Network"]}, [_NETWORK]) == machine


def test_an_agent_binding_matches_only_an_agent() -> None:
    entry = _entry(RoleGroup.READERS, {"*"}, group="/Bots", principal="agent")
    agent = _operator(principal_kind=PrincipalKind.AGENT, tenant_role=TenantRole.READ_ONLY)
    hit = bind(agent, {"groups": ["/Bots"]}, [entry])
    assert hit.bound_kind is RoleGroup.READERS
    assert hit.approver is False and hit.self_approver is False
    person = _operator(tenant_role=TenantRole.READ_ONLY)
    assert bind(person, {"groups": ["/Bots"]}, [entry]).bound_kind is None


def test_a_status_answer_without_a_tenant_is_not_bound() -> None:
    operator = _operator(tenant_chosen=False)
    assert bind(operator, {"groups": ["/MEHO-Operators/Network"]}, [_NETWORK]) == operator


# ---------------------------------------------------------------------------
# The shipped bindings
# ---------------------------------------------------------------------------


def test_the_shipped_bindings_come_from_the_two_settings() -> None:
    assert shipped_binding_documents(get_settings()) == [
        {"match": {"group": f"/MEHO-{kind}"}, "kind": kind, "categories": ["*"]}
        for kind in ("Readers", "Operators", "SA-Operators", "Admins", "Approvers")
    ]


def test_the_shipped_bindings_follow_renamed_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_ROLE_GROUPS", "Corp-Readers,/Corp-Admins,Corp-Other")
    monkeypatch.setenv("JWT_SELF_APPROVER_GROUP", "Corp-Self")
    get_settings.cache_clear()
    assert shipped_binding_documents(get_settings()) == [
        {"match": {"group": "/Corp-Readers"}, "kind": "Readers", "categories": ["*"]},
        {"match": {"group": "/Corp-Admins"}, "kind": "Admins", "categories": ["*"]},
        {"match": {"group": "/Corp-Self"}, "kind": "SA-Operators", "categories": ["*"]},
    ]


def test_a_shipped_operator_group_and_the_self_approval_group_give_sa_operators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today such a person is an operator who may approve their own changes."""
    monkeypatch.setenv("JWT_SELF_APPROVER_GROUP", "Ops-Self-Approve")
    get_settings.cache_clear()
    claims = {"groups": ["/MEHO-Operators", "/Ops-Self-Approve"]}
    after = bind(_operator(), claims, shipped_bindings(get_settings()))
    assert after.bound_kind is RoleGroup.SA_OPERATORS
    assert after.self_approver is True
    stored = [
        _entry(RoleGroup.OPERATORS, {"*"}, group="/MEHO-Operators"),
        _entry(RoleGroup.SA_OPERATORS, {"*"}, group="/Ops-Self-Approve"),
    ]
    with pytest.raises(BoundKindConflictError):
        bind(_operator(), claims, stored)


def test_a_bad_stored_binding_is_a_value_error() -> None:
    for document in (
        {"bindings": "x"},
        {"bindings": [{"match": {"group": "Short"}, "kind": "Operators", "categories": []}]},
        {"bindings": [{"match": {"group": "/A"}, "kind": "Boss", "categories": []}]},
        {"bindings": [{"match": {"group": "/A"}, "kind": "Operators"}]},
        ["not", "a", "map"],
    ):
        with pytest.raises(ValueError):
            bindings_from_document(document)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# After the tenant is chosen (database)
# ---------------------------------------------------------------------------


async def _store(document: dict[str, Any] | None) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(delete(TenantPolicy).where(TenantPolicy.tenant_id == _TENANT))
        if document is not None:
            session.add(TenantPolicy(tenant_id=_TENANT, document=document, set_by_sub="admin-a"))


_STORED = {
    "classifications": [],
    "bindings": [
        {
            "match": {"group": "/MEHO-Operators/Network"},
            "kind": "SA-Operators",
            "categories": ["network"],
        }
    ],
}


@pytest.mark.asyncio
async def test_with_the_switch_off_nothing_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_off(monkeypatch)

    async def _no_read(operator: Operator) -> Any:
        raise AssertionError("read the database with the switch off")

    monkeypatch.setattr(bindings_module, "_tenant_document", _no_read)
    operator = _operator()
    assert await bind_current_tenant(operator, {"groups": ["/MEHO-Operators/Network"]}) is operator


def test_the_token_parser_reads_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_read(operator: Operator) -> Any:
        raise AssertionError("the token parser read the database")

    monkeypatch.setattr(bindings_module, "_tenant_document", _no_read)
    claims = {
        "sub": "person-1",
        "tenant_id": str(_TENANT),
        "tenant_role": "operator",
        "groups": ["/MEHO-Operators/Network"],
    }
    operator = jwt_module._operator_from_claims(claims, "raw.jwt.token", get_settings())
    assert operator.bound_kind is None


@pytest.mark.asyncio
async def test_the_stored_bindings_of_the_tenant_count() -> None:
    await _store(_STORED)
    try:
        hit = await bind_current_tenant(_operator(), {"groups": ["/MEHO-Operators/Network"]})
        assert hit.bound_kind is RoleGroup.SA_OPERATORS
        assert hit.bound_categories == frozenset({"network"})
        miss = await bind_current_tenant(_operator(), {"groups": ["Network"]})
        assert miss.bound_kind is None
        other_tenant = _operator(tenant_id=uuid.uuid4())
        assert (
            await bind_current_tenant(other_tenant, {"groups": ["/MEHO-Operators/Network"]})
        ).bound_kind is None
    finally:
        await _store(None)


@pytest.mark.asyncio
async def test_without_stored_bindings_the_shipped_ones_count() -> None:
    await _store(None)
    claims = {"groups": ["/MEHO-SA-Operators"]}
    hit = await bind_current_tenant(_operator(), claims)
    assert hit.bound_kind is RoleGroup.SA_OPERATORS
    assert hit.bound_categories == frozenset({"*"})
    assert hit.self_approver is True
    short = await bind_current_tenant(_operator(), {"groups": ["MEHO-SA-Operators"]})
    assert short.bound_kind is None


@pytest.mark.asyncio
async def test_unreadable_bindings_give_the_lowest_kind() -> None:
    await _store({"bindings": [{"match": {"group": "Short"}, "kind": "X", "categories": []}]})
    try:
        operator = _operator(self_approver=True)
        after = await bind_current_tenant(operator, {"groups": ["/MEHO-SA-Operators"]})
        assert after.bound_kind is RoleGroup.OPERATORS
        assert after.bound_categories == frozenset()
        assert after.self_approver is False
    finally:
        await _store(None)


@pytest.mark.asyncio
async def test_two_kinds_give_a_401(monkeypatch: pytest.MonkeyPatch) -> None:
    await _store(
        {
            "bindings": [
                *_STORED["bindings"],
                {"match": {"attribute": "net"}, "kind": "Operators", "categories": ["*"]},
            ]
        }
    )
    claims = {
        "sub": "person-1",
        "tenant_id": str(_TENANT),
        "tenant_role": "operator",
        "groups": ["/MEHO-Operators/Network"],
        "meho_groups": ["net"],
    }

    async def _decoded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return claims

    monkeypatch.setattr(jwt_module, "_decode_with_kid_rotation", _decoded)
    try:
        with pytest.raises(HTTPException) as caught:
            await jwt_module.verify_jwt_for_audience("Bearer a.b.c", expected_audience="meho")
        assert caught.value.status_code == 401
        assert str(caught.value.detail).startswith("multiple_bound_kinds:")
        _switch_off(monkeypatch)
        operator = await jwt_module.verify_jwt_for_audience("Bearer a.b.c", expected_audience="m")
        assert operator.bound_kind is None
    finally:
        await _store(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["read_only", "operator"])
async def test_subgroups_of_two_base_levels_give_a_401(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    await _store(
        {
            "bindings": [
                *_STORED["bindings"],
                {
                    "match": {"group": "/MEHO-Readers/Storage"},
                    "kind": "Readers",
                    "categories": ["*"],
                },
            ]
        }
    )
    claims = {
        "sub": "person-1",
        "tenant_id": str(_TENANT),
        "tenant_role": role,
        "groups": ["/MEHO-Operators/Network", "/MEHO-Readers/Storage"],
    }

    async def _decoded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return claims

    monkeypatch.setattr(jwt_module, "_decode_with_kid_rotation", _decoded)
    try:
        with pytest.raises(HTTPException) as caught:
            await jwt_module.verify_jwt_for_audience("Bearer a.b.c", expected_audience="meho")
        assert caught.value.status_code == 401
        assert str(caught.value.detail).startswith(
            "multiple_bound_kinds: this login matches bindings with 2 group kinds "
            "(Readers, SA-Operators)"
        )
    finally:
        await _store(None)


# ---------------------------------------------------------------------------
# Diff and export
# ---------------------------------------------------------------------------


def test_the_diff_names_added_changed_and_removed_bindings() -> None:
    a = {"match": {"group": "/A"}, "kind": "Operators", "categories": ["network"]}
    b = {"match": {"group": "/B"}, "kind": "Operators", "categories": ["network"]}
    a2 = {**a, "categories": ["storage"]}
    c = {"match": {"group": "/C"}, "kind": "Readers", "categories": ["*"], "principal": "agent"}
    assert _bindings_diff([a, b], [a2, c]) == {
        "added": [{"entry": c}],
        "changed": [{"before": a, "after": a2}],
        "removed": [{"entry": b}],
    }


@pytest.mark.asyncio
async def test_export_shows_the_bindings_in_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    await _store(None)
    async with get_sessionmaker()() as session:
        shipped = await export_policy(session, scope="tenant", tenant_id=_TENANT)
        install = await export_policy(session, scope="install", tenant_id=None)
    assert "group: /MEHO-SA-Operators" in shipped
    assert "bindings" not in install
    reimported = parse_policy_file(shipped.encode())
    assert reimported.mistakes == []
    _switch_off(monkeypatch)
    async with get_sessionmaker()() as session:
        assert "bindings" not in await export_policy(session, scope="tenant", tenant_id=_TENANT)
    await _store(_STORED)
    try:
        async with get_sessionmaker()() as session:
            stored = await export_policy(session, scope="tenant", tenant_id=_TENANT)
        assert "group: /MEHO-Operators/Network" in stored
        assert "/MEHO-SA-Operators" not in stored
    finally:
        await _store(None)
