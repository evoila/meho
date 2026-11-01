# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for reading the person's tenant list into ``Operator.tenants`` (#5236).

The ``meho_tenants`` claim lists every tenant the person is in, as
``<tenant-uuid>:<tenant>:<RoleGroup>`` entries. MEHO keeps the list on the
:class:`~meho_backplane.auth.operator.Operator`. How the current tenant is
picked from it (#5239) is tested in ``test_auth_current_tenant.py``.
"""

from __future__ import annotations

import time
import uuid
import warnings
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

from meho_backplane.auth.jwt import (
    _extract_tenants,
    _operator_from_claims,
    clear_jwks_cache,
    verify_jwt,
)
from meho_backplane.auth.operator import (
    GroupGrant,
    Operator,
    PrincipalKind,
    RoleGroup,
    TenantMembership,
    TenantRole,
    group_grant,
)
from meho_backplane.settings import get_settings

_ISSUER: str = "https://keycloak.test/realms/meho"
_AUDIENCE: str = "meho-backplane"
_DISCOVERY_URL: str = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL: str = f"{_ISSUER}/protocol/openid-connect/certs"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_TENANT_C = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars required by :class:`~meho_backplane.settings.Settings`."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("JWT_TENANTS_CLAIM_NAME", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _claims(**extra: Any) -> dict[str, Any]:
    """Claims of a person in tenant A with the ``operator`` role."""
    claims: dict[str, Any] = {
        "sub": "person-1",
        "tenant_id": str(_TENANT_A),
        "tenant_role": "operator",
    }
    claims.update(extra)
    return claims


def _operator(claims: dict[str, Any]) -> Operator:
    return _operator_from_claims(claims, "raw.jwt.token", get_settings())


def _tenants(raw: Any) -> tuple[TenantMembership, ...]:
    return _operator(_claims(meho_tenants=raw)).tenants


# ---------------------------------------------------------------------------
# The group table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        (RoleGroup.READERS, GroupGrant(TenantRole.READ_ONLY, approver=False, self_approval=False)),
        (RoleGroup.OPERATORS, GroupGrant(TenantRole.OPERATOR, approver=False, self_approval=False)),
        (
            RoleGroup.SA_OPERATORS,
            GroupGrant(TenantRole.OPERATOR, approver=False, self_approval=True),
        ),
        (
            RoleGroup.ADMINS,
            GroupGrant(TenantRole.TENANT_ADMIN, approver=False, self_approval=False),
        ),
        (RoleGroup.APPROVERS, GroupGrant(TenantRole.READ_ONLY, approver=True, self_approval=False)),
    ],
)
def test_group_grant_matches_the_table(group: RoleGroup, expected: GroupGrant) -> None:
    """Each of the five group kinds gives the role and flags of the table."""
    assert group_grant(group) == expected


def test_group_table_covers_every_group_kind() -> None:
    """No group kind is missing from the table."""
    for group in RoleGroup:
        assert isinstance(group_grant(group), GroupGrant)


# ---------------------------------------------------------------------------
# Reading the claim
# ---------------------------------------------------------------------------


def test_no_claim_gives_empty_tenants() -> None:
    """A token without ``meho_tenants`` → ``()`` (today's tokens)."""
    assert _operator(_claims()).tenants == ()


def test_empty_list_gives_empty_tenants() -> None:
    assert _tenants([]) == ()


def test_one_entry() -> None:
    assert _tenants([f"{_TENANT_A}:lab-a:Operators"]) == (
        TenantMembership(tenant_id=_TENANT_A, name="lab-a", group=RoleGroup.OPERATORS),
    )


def test_three_entries_keep_token_order() -> None:
    tenants = _tenants(
        [
            f"{_TENANT_B}:lab-b:Readers",
            f"{_TENANT_A}:lab-a:SA-Operators",
            f"{_TENANT_C}:team3:Approvers",
        ]
    )
    assert tenants == (
        TenantMembership(tenant_id=_TENANT_B, name="lab-b", group=RoleGroup.READERS),
        TenantMembership(tenant_id=_TENANT_A, name="lab-a", group=RoleGroup.SA_OPERATORS),
        TenantMembership(tenant_id=_TENANT_C, name="team3", group=RoleGroup.APPROVERS),
    )


@pytest.mark.parametrize(
    "bad_entry",
    [
        pytest.param("not-a-uuid:lab-b:Readers", id="bad-uuid"),
        pytest.param(f"{_TENANT_B}:lab-b:Owners", id="unknown-group-kind"),
        pytest.param(f"{_TENANT_B}:lab-b:readers", id="group-kind-wrong-case"),
        pytest.param(f"{_TENANT_B}:Lab_B:Readers", id="bad-tenant-name"),
        pytest.param(f"{_TENANT_B}::Readers", id="empty-tenant-name"),
        pytest.param(f"{_TENANT_B}:lab-b", id="two-parts"),
        pytest.param(f"{_TENANT_B}:lab-b:Readers:extra", id="four-parts"),
        pytest.param(42, id="not-a-string"),
    ],
)
def test_bad_entry_is_left_out_and_logged(bad_entry: Any) -> None:
    """A bad entry is dropped and logged; the good entries stay."""
    good = f"{_TENANT_A}:lab-a:Admins"
    with capture_logs() as logs:
        tenants = _tenants([good, bad_entry])
    assert tenants == (TenantMembership(tenant_id=_TENANT_A, name="lab-a", group=RoleGroup.ADMINS),)
    events = [e for e in logs if e["event"] == "malformed_tenant_entry"]
    assert len(events) == 1
    assert events[0]["entry"] == bad_entry
    assert events[0]["claim_name"] == "meho_tenants"


def test_string_instead_of_list_gives_empty_tenants() -> None:
    """A single string (mapper not set to multivalued) → ``()`` + a log line."""
    with capture_logs() as logs:
        tenants = _tenants(f"{_TENANT_A}:lab-a:Operators")
    assert tenants == ()
    assert [e["event"] for e in logs] == ["malformed_tenants_claim"]
    assert logs[0]["reason"] == "not_a_list"


def test_two_entries_for_one_tenant_are_both_kept() -> None:
    """The reader keeps both; the one-group-per-tenant rule is a later step."""
    claims = _claims(meho_tenants=[f"{_TENANT_A}:lab-a:Readers", f"{_TENANT_A}:lab-a:Admins"])
    tenants = _extract_tenants(claims, get_settings(), PrincipalKind.USER)
    assert [t.group for t in tenants] == [RoleGroup.READERS, RoleGroup.ADMINS]


def test_custom_claim_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_TENANTS_CLAIM_NAME`` changes the claim that is read."""
    monkeypatch.setenv("JWT_TENANTS_CLAIM_NAME", "my_tenants")
    get_settings.cache_clear()
    entry = f"{_TENANT_A}:lab-a:Readers"
    assert _operator(_claims(meho_tenants=[entry])).tenants == ()
    assert len(_operator(_claims(my_tenants=[entry])).tenants) == 1


# ---------------------------------------------------------------------------
# Machine logins never get a list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        (PrincipalKind.AGENT, {}),
        (PrincipalKind.SERVICE, {}),
        (PrincipalKind.RUNNER, {"runner_id": str(uuid.uuid4())}),
    ],
)
def test_machine_login_gets_no_tenants(kind: PrincipalKind, extra: dict[str, Any]) -> None:
    claims = _claims(
        principal_kind=kind.value,
        meho_tenants=[f"{_TENANT_A}:lab-a:Admins", f"{_TENANT_B}:lab-b:Admins"],
        **extra,
    )
    operator = _operator(claims)
    assert operator.principal_kind is kind
    assert operator.tenants == ()


# ---------------------------------------------------------------------------
# No change in behaviour
# ---------------------------------------------------------------------------


def test_list_replaces_the_flat_claims() -> None:
    """With a list, the start tenant, role and approver flag come from its first entry (#5239)."""
    with_list = _operator(
        _claims(
            approver=True,
            meho_tenants=[f"{_TENANT_B}:lab-b:Admins", f"{_TENANT_C}:team3:Approvers"],
        )
    )
    assert with_list.tenant_id == _TENANT_B
    assert with_list.tenant_role == TenantRole.TENANT_ADMIN
    assert with_list.approver is False


def test_operator_stays_frozen_and_hides_raw_jwt() -> None:
    operator = _operator(_claims(meho_tenants=[f"{_TENANT_A}:lab-a:Operators"]))
    with pytest.raises(ValidationError):
        operator.tenants = ()
    with pytest.raises(ValidationError):
        operator.tenants[0].name = "other"
    assert "raw.jwt.token" not in repr(operator)
    assert "lab-a" in repr(operator)


def test_operator_default_tenants_is_empty() -> None:
    operator = Operator(sub="s", raw_jwt="t", tenant_id=_TENANT_A, tenant_role=TenantRole.OPERATOR)
    assert operator.tenants == ()


# ---------------------------------------------------------------------------
# End to end through verify_jwt
# ---------------------------------------------------------------------------


def _mint(key: Any, payload_extra: dict[str, Any]) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jwt = JsonWebToken(["RS256"])
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "iat": now,
            "exp": now + 3600,
            "nbf": now,
            **_claims(**payload_extra),
        }
        header = {"alg": "RS256", "kid": key.as_dict()["kid"], "typ": "JWT"}
        token: bytes | str = jwt.encode(header, payload, key)
        return token.decode("ascii") if isinstance(token, bytes) else token


def test_verify_jwt_reads_tenants_from_a_signed_token() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        key = JsonWebKey.generate_key("RSA", 2048, options={"kid": "kid-tenants"}, is_private=True)
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(operator: Operator = Depends(verify_jwt)) -> dict[str, Any]:
        return {
            "tenant_id": str(operator.tenant_id),
            "tenants": [[str(t.tenant_id), t.name, t.group.value] for t in operator.tenants],
        }

    token = _mint(
        key, {"meho_tenants": [f"{_TENANT_A}:lab-a:Operators", f"{_TENANT_B}:lab-b:Readers"]}
    )
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(
            return_value=httpx.Response(200, json={"keys": [key.as_dict(is_private=False)]})
        )
        with TestClient(app) as client:
            resp = client.get(
                "/whoami", headers={"Authorization": f"Bearer {token}", "Meho-Tenant": "lab-b"}
            )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": str(_TENANT_B),
        "tenants": [
            [str(_TENANT_A), "lab-a", "Operators"],
            [str(_TENANT_B), "lab-b", "Readers"],
        ],
    }
