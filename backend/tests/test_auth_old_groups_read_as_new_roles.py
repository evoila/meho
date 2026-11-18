# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO reads the old groups as the new roles.

* MEHO-SA-Operators is read as Operators: the ``operator`` role, no
  self-approval, no approval of a high-risk change.
* MEHO-Approvers is read as Readers: the ``read_only`` role, no ``approver``
  flag, no access to the approvals pages.
* A person in an old group still signs in, in both token forms (with the
  tenant list ``meho_tenants`` and without it).
* An Admin approves their own change at every risk level. An Operator
  approves their own token ``caution`` change only with a console click.
* A policy file with an old group kind loads and reads it as the new kind.
* A login with the ``approver`` claim and no old group keeps the approval
  right with routing on, and its push row keeps the flag.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from meho_backplane.auth.jwt import _operator_from_claims
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_approvals_access
from meho_backplane.operations.approval_queue import may_approve_high_risk
from meho_backplane.operations.approval_rights import ApproveRightReason, approve_right
from meho_backplane.policy.document import document_of, parse_policy_file
from meho_backplane.policy.routing import DEFAULT_RULE, today_kinds
from meho_backplane.settings import get_settings
from meho_backplane.web_push.recipients import login_facts_for
from tests.test_approval_group_rules import _OTHER, _REQUESTER, _person
from tests.test_approval_rights import _row, _token_call_row

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    for name in (
        "JWT_SELF_APPROVER_GROUP",
        "JWT_TENANTS_CLAIM_NAME",
        "JWT_ROLE_GROUPS",
        "POLICY_APPROVER_ROUTING",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login(claims: dict[str, Any]) -> Operator:
    base: dict[str, Any] = {"sub": "person-1"}
    base.update(claims)
    return _operator_from_claims(base, "raw.jwt.token", get_settings())


def _sa_operator(form: str) -> Operator:
    if form == "list":
        return _login({"meho_tenants": [f"{_TENANT}:lab-a:SA-Operators"]})
    return _login(
        {"tenant_id": str(_TENANT), "tenant_role": "operator", "groups": ["MEHO-SA-Operators"]}
    )


def _approver(form: str) -> Operator:
    if form == "list":
        return _login({"meho_tenants": [f"{_TENANT}:lab-a:Approvers"]})
    return _login(
        {
            "tenant_id": str(_TENANT),
            "tenant_role": "read_only",
            "approver": True,
            "groups": ["MEHO-Approvers"],
        }
    )


_FORMS = ["list", "flat"]


@pytest.mark.parametrize("form", _FORMS)
def test_a_person_in_sa_operators_is_an_operator(form: str) -> None:
    operator = _sa_operator(form)

    assert operator.tenant_role is TenantRole.OPERATOR
    assert operator.self_approver is False
    assert operator.approver is False
    assert operator.may_self_approve is False
    assert may_approve_high_risk(operator) is False


@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("level", ["dangerous", "destructive"])
def test_sa_operators_approve_no_high_risk_change(form: str, level: str) -> None:
    operator = _sa_operator(form).model_copy(update={"sub": _OTHER})

    own = approve_right(operator.model_copy(update={"sub": _REQUESTER}), _row(level))
    other = approve_right(operator, _row(level))

    assert own.may_approve is False
    assert own.reason == ApproveRightReason.OWN_CHANGE
    assert other.may_approve is False
    assert other.reason == ApproveRightReason.WRONG_GROUP


@pytest.mark.parametrize("form", _FORMS)
def test_sa_operators_approve_another_persons_low_risk_change(form: str) -> None:
    operator = _sa_operator(form)

    assert approve_right(operator, _row("caution")).may_approve is True


@pytest.mark.parametrize("form", _FORMS)
def test_a_person_in_approvers_is_a_reader(form: str) -> None:
    operator = _approver(form)

    assert operator.tenant_role is TenantRole.READ_ONLY
    assert operator.approver is False
    assert operator.self_approver is False
    assert approve_right(operator, _row("safe")).may_approve is False
    with pytest.raises(HTTPException) as refused:
        require_approvals_access(operator)
    assert refused.value.status_code == 403


def test_another_source_of_the_approver_claim_still_counts() -> None:
    """Only the old group is read as Readers: a login with the claim and no old group keeps it."""
    operator = _login(
        {
            "tenant_id": str(_TENANT),
            "tenant_role": "read_only",
            "approver": True,
            "groups": ["MEHO-Readers"],
        }
    )

    assert operator.approver is True


def _custom_approver() -> Operator:
    """A ``read_only`` login with the ``approver`` claim and no old group."""
    return _login(
        {
            "sub": _OTHER,
            "tenant_id": str(_TENANT),
            "tenant_role": "read_only",
            "approver": True,
            "groups": ["MEHO-Readers"],
        }
    )


def _routed(level: str, kinds: tuple[str, ...]) -> Any:
    """A waiting request with stored routing *kinds* and the default rule."""
    row = _row(level)
    row.approver_roles = list(kinds)
    row.approver_rule = DEFAULT_RULE
    return row


def test_with_routing_on_a_custom_approver_keeps_the_approval_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No routing entry narrows access: the default kinds let the custom approver approve."""
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()
    approver = _custom_approver()

    caution = approve_right(approver, _routed("caution", today_kinds("caution", "")))
    dangerous = approve_right(approver, _routed("dangerous", today_kinds("dangerous", "")))
    admins_only = approve_right(approver, _routed("caution", ("Admins",)))

    assert caution.may_approve is True
    assert dangerous.may_approve is False
    assert admins_only.may_approve is False


@pytest.mark.parametrize("form", _FORMS)
def test_with_routing_on_a_person_in_approvers_approves_nothing(
    form: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()
    reader = _approver(form).model_copy(update={"sub": _OTHER})

    right = approve_right(reader, _routed("caution", today_kinds("caution", "")))

    assert right.may_approve is False


@pytest.mark.parametrize("form", _FORMS)
def test_a_login_of_an_old_group_stores_no_approval_flags_for_push(form: str) -> None:
    for operator in (_sa_operator(form), _approver(form)):
        facts = login_facts_for(operator)
        assert facts.approver is False
        assert facts.self_approver is False


def test_a_custom_approver_login_stores_the_approver_flag_for_push() -> None:
    facts = login_facts_for(_custom_approver())

    assert facts.tenant_role == TenantRole.READ_ONLY.value
    assert facts.approver is True


def test_an_admin_approves_their_own_change_at_every_risk_level() -> None:
    admin = _person(_REQUESTER, role=TenantRole.TENANT_ADMIN)

    for level in ("safe", "caution", "dangerous", "destructive"):
        right = approve_right(admin, _row(level))
        assert right.may_approve is True, level
        assert right.self_approval is True, level


def test_an_operator_approves_their_own_token_caution_change_only_with_a_click() -> None:
    operator = _sa_operator("list").model_copy(update={"sub": _REQUESTER})

    on_the_console = approve_right(
        operator.model_copy(update={"web_session": True}), _token_call_row()
    )
    with_a_token = approve_right(operator, _token_call_row())
    own_without_token_wait = approve_right(
        operator.model_copy(update={"web_session": True}), _row("caution")
    )

    assert on_the_console.may_approve is True
    assert with_a_token.may_approve is False
    assert own_without_token_wait.may_approve is False


@pytest.mark.parametrize("form", _FORMS)
def test_a_person_in_an_old_group_still_signs_in(form: str) -> None:
    assert _sa_operator(form).tenant_id == _TENANT
    assert _approver(form).tenant_id == _TENANT


def test_a_policy_file_with_an_old_kind_is_read_as_the_new_kind() -> None:
    parsed = parse_policy_file(
        b"policy: v1\n"
        b"bindings:\n"
        b"  - match: {group: /MEHO-Operators/Network}\n"
        b"    kind: SA-Operators\n"
        b"    categories: [network]\n"
        b"routing:\n"
        b"  - match: {category: network}\n"
        b"    approvers: [SA-Operators, Admins]\n"
    )

    assert parsed.mistakes == []
    assert parsed.model is not None
    document = document_of(parsed.model)
    assert document["bindings"][0]["kind"] == "Operators"
    assert document["routing"][0]["approvers"] == ["Operators", "Admins"]


def test_the_shipped_separated_duties_file_names_no_old_kind() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "deploy" / "policy-profiles" / "separated-duties.yaml"
    )
    text = path.read_text()

    assert "SA-Operators" not in text
    assert "Approvers" not in text
