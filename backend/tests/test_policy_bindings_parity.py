# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bindings without stored bindings change no login (``POLICY_APPROVER_ROUTING``).

A tenant without stored bindings uses the shipped bindings. They count
only for a login without a tenant list (``meho_tenants``): the ``groups``
claim is not per tenant, so for a login with a list the list entry of the
current tenant decides, as with the switch off. See
:mod:`meho_backplane.policy.bindings`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from meho_backplane.auth import current_tenant
from meho_backplane.auth import jwt as jwt_module
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.settings import get_settings

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-6048aaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-6048bbbbbbbb")
_TENANT_C = uuid.UUID("cccccccc-cccc-cccc-cccc-6048cccccccc")


def _entry(tenant: uuid.UUID, slug: str, kind: str) -> str:
    return f"{tenant}:{slug}:{kind}"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    for name in (
        "JWT_ROLE_GROUPS",
        "JWT_SELF_APPROVER_GROUP",
        "JWT_TENANTS_CLAIM_NAME",
        "POLICY_APPROVER_ROUTING",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _no_stored_bindings(_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """No tenant has stored bindings; a platform admin finds tenant C."""
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            delete(TenantPolicy).where(
                TenantPolicy.tenant_id.in_([_TENANT_A, _TENANT_B, _TENANT_C])
            )
        )

    async def _lookup(wanted: str) -> uuid.UUID | None:
        return _TENANT_C if wanted in ("lab-c", str(_TENANT_C)) else None

    monkeypatch.setattr(current_tenant, "_find_existing_tenant", _lookup)


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true" if on else "false")
    get_settings.cache_clear()


async def _login(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any], header: str | None
) -> Operator | str:
    """The operator of *claims* in the tenant *header* names, or the 401/403 detail."""

    async def _decoded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return claims

    monkeypatch.setattr(jwt_module, "_decode_with_kid_rotation", _decoded)
    try:
        return await jwt_module.verify_jwt_for_audience(
            "Bearer a.b.c", expected_audience="meho-backplane", requested_tenant=header
        )
    except HTTPException as exc:
        return f"{exc.status_code} {exc.detail}"


def _person(**extra: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "sub": "person-1",
        "tenant_id": str(_TENANT_A),
        "tenant_role": "operator",
    }
    claims.update(extra)
    return claims


def _listed(entries: list[str], **extra: Any) -> dict[str, Any]:
    return {"sub": "person-1", "meho_tenants": entries, **extra}


# ---------------------------------------------------------------------------
# A login with a tenant list: the groups of another tenant decide nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_a", "groups_a", "entry_b", "groups_b", "want"),
    [
        pytest.param(
            "SA-Operators",
            "/MEHO-SA-Operators",
            "Operators",
            "/MEHO-lab-b-Operators",
            (False, False),
            id="self-approval-group-of-tenant-a",
        ),
        pytest.param(
            "Approvers",
            "/MEHO-Approvers",
            "Readers",
            "/MEHO-lab-b-Readers",
            (False, False),
            id="approver-group-of-tenant-a",
        ),
        pytest.param(
            "Operators",
            "/MEHO-Operators",
            "SA-Operators",
            "/MEHO-lab-b-SA-Operators",
            (False, True),
            id="self-approval-group-of-tenant-b",
        ),
        pytest.param(
            "Readers",
            "/MEHO-Readers",
            "Approvers",
            "/MEHO-lab-b-Approvers",
            (True, False),
            id="approver-group-of-tenant-b",
        ),
    ],
)
async def test_a_group_of_another_tenant_decides_no_kind_in_this_tenant(
    monkeypatch: pytest.MonkeyPatch,
    entry_a: str,
    groups_a: str,
    entry_b: str,
    groups_b: str,
    want: tuple[bool, bool],
) -> None:
    """Switch on, full group paths on, no stored bindings in tenant B."""
    claims = _listed(
        [_entry(_TENANT_A, "lab-a", entry_a), _entry(_TENANT_B, "lab-b", entry_b)],
        groups=[groups_a, groups_b],
    )
    _switch(monkeypatch, on=False)
    off = await _login(monkeypatch, claims, "lab-b")
    _switch(monkeypatch, on=True)
    on = await _login(monkeypatch, claims, "lab-b")
    assert isinstance(off, Operator) and isinstance(on, Operator)
    assert on.tenant_id == _TENANT_B
    assert (on.approver, on.self_approver) == (off.approver, off.self_approver) == want
    assert on == off


# ---------------------------------------------------------------------------
# Every login shape: switch on without stored bindings == switch off
# ---------------------------------------------------------------------------

_A_READERS = _entry(_TENANT_A, "lab-a", "Readers")
_A_OPERATORS = _entry(_TENANT_A, "lab-a", "Operators")
_A_SA = _entry(_TENANT_A, "lab-a", "SA-Operators")
_B_OPERATORS = _entry(_TENANT_B, "lab-b", "Operators")
_B_SA = _entry(_TENANT_B, "lab-b", "SA-Operators")

#: The login shapes of ``test_auth_current_tenant.py`` and
#: ``test_auth_self_approver.py``, each with the tenant header of the
#: request, plus the same groups as full paths.
_SHAPES: list[Any] = [
    # test_auth_current_tenant.py
    pytest.param(_person(), None, id="no-list-no-header"),
    pytest.param(_person(), str(_TENANT_A), id="no-list-own-tenant"),
    pytest.param(_person(), str(_TENANT_B), id="no-list-other-tenant"),
    pytest.param(_person(), "lab-a", id="no-list-name"),
    pytest.param(_listed([_A_READERS]), None, id="one-entry"),
    pytest.param(_listed([_B_OPERATORS]), "LAB-B", id="one-entry-by-name"),
    pytest.param(_listed([_B_OPERATORS]), "lab-a", id="one-entry-other-tenant"),
    pytest.param(_listed([_A_READERS], **_person()), None, id="list-and-flat-claims"),
    pytest.param(_listed([_A_READERS, _B_OPERATORS]), None, id="two-entries-no-header"),
    pytest.param(_listed([_A_READERS, _B_OPERATORS]), "lab-a", id="two-entries-a"),
    pytest.param(_listed([_A_READERS, _B_OPERATORS]), str(_TENANT_B), id="two-entries-b"),
    pytest.param(_listed([_A_READERS, _B_OPERATORS]), str(_TENANT_C), id="two-entries-c"),
    pytest.param(_listed([_A_READERS, _A_OPERATORS]), None, id="two-entries-one-tenant"),
    pytest.param(
        _listed([_A_READERS], groups=["MEHO-Admins", "MEHO-Operators"]),
        None,
        id="list-skips-groups",
    ),
    pytest.param(_person(groups=["MEHO-Admins", "MEHO-Operators"]), None, id="two-role-groups"),
    pytest.param(
        _listed([_A_READERS], platform_admin=True), "lab-c", id="platform-admin-other-tenant"
    ),
    pytest.param(_person(platform_admin=True), str(_TENANT_C), id="platform-admin-no-list"),
    pytest.param(
        _listed([_A_SA, _B_OPERATORS], platform_admin=True), "lab-a", id="platform-admin-own-group"
    ),
    # test_auth_self_approver.py
    pytest.param(_person(groups=[]), None, id="groups-empty"),
    *[
        pytest.param(_person(groups=[group]), None, id=f"groups-{group}")
        for group in (
            "MEHO-SA-Operators",
            "/MEHO-SA-Operators",
            "meho-sa-operators",
            "/Meho-SA-operators",
            "MEHO-Operators",
            "/MEHO-Operators",
            "MEHO-SA-Operators-old",
            "MEHO-lab-a-SA-Operators",
        )
    ],
    pytest.param(_person(groups=["/MEHO-Readers"], tenant_role="read_only"), None, id="readers"),
    pytest.param(
        _person(groups=["/MEHO-Approvers"], tenant_role="read_only", approver=True),
        None,
        id="approvers",
    ),
    pytest.param(
        _person(groups=["MEHO-Approvers"], tenant_role="read_only", approver=True),
        None,
        id="approvers-short",
    ),
    pytest.param(_person(groups=["/MEHO-Admins"], tenant_role="tenant_admin"), None, id="admins"),
    pytest.param(_person(groups=["meho-admins"], tenant_role="tenant_admin"), None, id="admins-lc"),
    pytest.param(_person(groups=[42, None]), None, id="groups-not-texts"),
    pytest.param(_person(groups="/MEHO-SA-Operators"), None, id="groups-one-text"),
    pytest.param(_person(groups={"/MEHO-SA-Operators": True}), None, id="groups-map"),
    pytest.param(_person(groups=True), None, id="groups-true"),
    pytest.param(_person(groups=["/MEHO-SA-Operators", 42]), None, id="groups-bad-entry"),
    pytest.param(_person(groups=["/MEHO-SA-Operators", {"n": 1}]), None, id="groups-dict-entry"),
    pytest.param(_listed([_A_SA], groups=["/MEHO-SA-Operators"]), None, id="list-sa"),
    *[
        pytest.param(
            _listed([_entry(_TENANT_A, "lab-a", kind)], groups=[f"/MEHO-{kind}"]),
            None,
            id=f"list-{kind}",
        )
        for kind in ("Readers", "Operators", "Admins", "Approvers")
    ],
    pytest.param(
        _listed([_A_OPERATORS, _B_SA], groups=["/MEHO-Operators", "/MEHO-lab-b-SA-Operators"]),
        None,
        id="sa-entry-of-another-tenant",
    ),
    pytest.param(
        _listed([_A_OPERATORS], groups=["/MEHO-SA-Operators"]), None, id="list-wins-over-groups"
    ),
    pytest.param(
        _person(meho_tenants="not-a-list", groups=["/MEHO-SA-Operators"]),
        None,
        id="tenants-claim-not-a-list",
    ),
    pytest.param(
        _listed([_A_SA, "bad-entry"], groups=["/MEHO-SA-Operators"]),
        None,
        id="good-entry-next-to-a-bad-one",
    ),
    pytest.param(
        _listed([_A_OPERATORS, _B_SA], groups=["/MEHO-Operators"]), "lab-b", id="switch-to-sa"
    ),
    pytest.param(
        _listed([_A_SA, "bad-entry", _B_SA], groups=["/MEHO-SA-Operators"]),
        "lab-b",
        id="switch-after-a-bad-entry",
    ),
    pytest.param(
        _listed([_A_READERS, _B_SA], groups=["/MEHO-SA-Operators"]),
        None,
        id="status-answer-unchosen",
    ),
    *[
        pytest.param(
            _person(principal_kind=kind, groups=["/MEHO-SA-Operators"]), None, id=f"machine-{kind}"
        )
        for kind in ("agent", "service")
    ],
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("claims", "header"), _SHAPES)
async def test_without_stored_bindings_the_switch_changes_no_login(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any], header: str | None
) -> None:
    """The same answer, the same role and the same approve flags as with the switch off.

    The one difference: a login without a tenant list that matches a
    shipped binding notes that binding's kind and categories
    (``bound_kind``, ``bound_categories``). No code decides by them yet.
    """
    _switch(monkeypatch, on=False)
    off = await _login(monkeypatch, claims, header)
    _switch(monkeypatch, on=True)
    on = await _login(monkeypatch, claims, header)
    if isinstance(off, str) or isinstance(on, str):
        assert on == off
        return
    noted = {"bound_kind", "bound_categories"}
    assert on.model_dump(exclude=noted) == off.model_dump(exclude=noted)
    assert on.may_self_approve is off.may_self_approve
    if "meho_tenants" in claims:
        assert on == off


@pytest.mark.asyncio
async def test_a_custom_self_approval_group_next_to_an_operator_group_changes_no_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SELF_APPROVER_GROUP", "Ops-Self-Approve")
    claims = _person(groups=["/MEHO-Operators", "/Ops-Self-Approve"])
    _switch(monkeypatch, on=False)
    off = await _login(monkeypatch, claims, None)
    _switch(monkeypatch, on=True)
    on = await _login(monkeypatch, claims, None)
    assert isinstance(off, Operator) and isinstance(on, Operator)
    assert off.self_approver is True
    assert on.model_dump(exclude={"bound_kind", "bound_categories"}) == off.model_dump(
        exclude={"bound_kind", "bound_categories"}
    )
