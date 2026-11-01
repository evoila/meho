# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the SA-Operators flag on the person (#3327).

``Operator.self_approver`` says the person is in the SA-Operators group of
the current tenant. ``Operator.may_self_approve`` is the one question the
approval checks ask. The flag is off by default and every unclear token
gives ``False``. A machine login never gets it.
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
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, JsonWebToken

from meho_backplane.auth import current_tenant
from meho_backplane.auth.current_tenant import select_current_tenant
from meho_backplane.auth.jwt import _operator_from_claims, clear_jwks_cache, verify_jwt
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.settings import get_settings

_ISSUER: str = "https://keycloak.test/realms/meho"
_AUDIENCE: str = "meho-backplane"
_DISCOVERY_URL: str = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URL: str = f"{_ISSUER}/protocol/openid-connect/certs"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars required by :class:`~meho_backplane.settings.Settings`."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("JWT_SELF_APPROVER_GROUP", raising=False)
    monkeypatch.delenv("JWT_TENANTS_CLAIM_NAME", raising=False)
    monkeypatch.delenv("JWT_ROLE_GROUPS", raising=False)
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


# ---------------------------------------------------------------------------
# Default
# ---------------------------------------------------------------------------


def test_flag_is_off_by_default() -> None:
    operator = Operator(sub="s", raw_jwt="t", tenant_id=_TENANT_A, tenant_role=TenantRole.OPERATOR)
    assert operator.self_approver is False
    assert operator.may_self_approve is False


def test_token_without_groups_or_tenants_gives_no_flag() -> None:
    operator = _operator(_claims())
    assert operator.self_approver is False
    assert operator.may_self_approve is False


# ---------------------------------------------------------------------------
# The ``groups`` claim (no ``meho_tenants`` claim)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group", ["MEHO-SA-Operators", "/MEHO-SA-Operators"])
def test_sa_operators_group_sets_the_flag(group: str) -> None:
    operator = _operator(_claims(groups=[group]))
    assert operator.self_approver is True
    assert operator.may_self_approve is True
    assert operator.tenant_role is TenantRole.OPERATOR


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param([], id="empty"),
        pytest.param(["MEHO-Operators"], id="operators"),
        pytest.param(["MEHO-Admins"], id="admins"),
        pytest.param(["MEHO-Approvers"], id="approvers"),
        pytest.param(["meho-sa-operators"], id="wrong-case"),
        pytest.param(["MEHO-SA-Operators-old"], id="longer-name"),
        pytest.param(["MEHO-lab-a-SA-Operators"], id="other-tenant-group"),
        pytest.param([42, None], id="not-strings"),
    ],
)
def test_other_groups_give_no_flag(groups: list[Any]) -> None:
    assert _operator(_claims(groups=groups)).self_approver is False


@pytest.mark.parametrize("raw", ["MEHO-SA-Operators", {"MEHO-SA-Operators": True}, True])
def test_malformed_groups_claim_gives_no_flag(raw: Any) -> None:
    with capture_logs() as logs:
        operator = _operator(_claims(groups=raw))
    assert operator.self_approver is False
    assert [e["event"] for e in logs] == ["malformed_groups_claim"]


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param(["MEHO-SA-Operators", 42], id="number-after"),
        pytest.param([None, "MEHO-SA-Operators"], id="none-before"),
        pytest.param(["MEHO-SA-Operators", ["MEHO-SA-Operators"]], id="nested-list"),
        pytest.param(["/MEHO-SA-Operators", {"name": "x"}], id="dict-after-path"),
    ],
)
def test_groups_list_with_a_bad_entry_gives_no_flag(groups: list[Any]) -> None:
    """One bad entry turns the whole ``groups`` claim off, even next to the group."""
    with capture_logs() as logs:
        operator = _operator(_claims(groups=groups))
    assert operator.self_approver is False
    assert operator.may_self_approve is False
    assert [(e["event"], e["reason"]) for e in logs] == [
        ("malformed_groups_claim", "entry_not_a_string")
    ]


def test_custom_group_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JWT_SELF_APPROVER_GROUP`` changes the group that counts."""
    monkeypatch.setenv("JWT_SELF_APPROVER_GROUP", "Ops-Self-Approve")
    get_settings.cache_clear()
    assert _operator(_claims(groups=["Ops-Self-Approve"])).self_approver is True
    assert _operator(_claims(groups=["MEHO-SA-Operators"])).self_approver is False


def test_blank_group_name_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SELF_APPROVER_GROUP", "  ")
    get_settings.cache_clear()
    assert get_settings().jwt_self_approver_group == "MEHO-SA-Operators"


# ---------------------------------------------------------------------------
# The ``meho_tenants`` claim (the current tenant's entry decides)
# ---------------------------------------------------------------------------


def test_current_tenant_sa_entry_sets_the_flag() -> None:
    operator = _operator(_claims(meho_tenants=[f"{_TENANT_A}:lab-a:SA-Operators"]))
    assert operator.self_approver is True


@pytest.mark.parametrize("group", ["Readers", "Operators", "Admins", "Approvers"])
def test_current_tenant_other_entry_gives_no_flag(group: str) -> None:
    operator = _operator(_claims(meho_tenants=[f"{_TENANT_A}:lab-a:{group}"]))
    assert operator.self_approver is False


def test_sa_entry_of_another_tenant_gives_no_flag() -> None:
    """SA-Operators of tenant B never counts while working in tenant A."""
    operator = _operator(
        _claims(
            meho_tenants=[f"{_TENANT_A}:lab-a:Operators", f"{_TENANT_B}:lab-b:SA-Operators"],
        )
    )
    assert operator.self_approver is False


def test_list_wins_over_the_groups_claim() -> None:
    """With a ``meho_tenants`` claim, only the list counts."""
    operator = _operator(
        _claims(groups=["MEHO-SA-Operators"], meho_tenants=[f"{_TENANT_A}:lab-a:Operators"])
    )
    assert operator.self_approver is False


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param([], id="empty-list"),
        pytest.param(["not-a-uuid:lab-a:SA-Operators"], id="bad-entry"),
        pytest.param(f"{_TENANT_A}:lab-a:SA-Operators", id="string-not-list"),
    ],
)
def test_malformed_tenants_claim_gives_no_flag(raw: Any) -> None:
    """A present but bad list never falls back to the ``groups`` claim."""
    operator = _operator(_claims(groups=["MEHO-SA-Operators"], meho_tenants=raw))
    assert operator.self_approver is False


_GOOD_SA_ENTRY = f"{_TENANT_A}:lab-a:SA-Operators"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param([_GOOD_SA_ENTRY, "not-a-uuid:lab-a:SA-Operators"], id="bad-uuid-after"),
        pytest.param([f"{_TENANT_A}:lab-a", _GOOD_SA_ENTRY], id="two-parts-before"),
        pytest.param([_GOOD_SA_ENTRY, f"{_TENANT_A}:Lab_A:Readers"], id="bad-tenant-name"),
        pytest.param([_GOOD_SA_ENTRY, f"{_TENANT_B}:lab-b:Bosses"], id="unknown-group"),
        pytest.param([_GOOD_SA_ENTRY, 42], id="number"),
        pytest.param([None, _GOOD_SA_ENTRY], id="none"),
    ],
)
def test_good_entry_next_to_a_bad_entry_gives_no_flag(raw: Any) -> None:
    """The whole list must be good. One bad entry turns the flag off.

    ``_extract_tenants`` keeps the good entries for ``Operator.tenants``,
    so the flag cannot trust that list alone (#5394 review).
    """
    with capture_logs() as logs:
        operator = _operator(_claims(meho_tenants=raw))
    assert operator.self_approver is False
    assert operator.may_self_approve is False
    assert operator.tenants_claim_malformed is True
    assert [e["event"] for e in logs] == ["malformed_tenant_entry"]
    assert [t.tenant_id for t in operator.tenants] == [_TENANT_A]


def test_two_entries_for_the_current_tenant_refuse_the_login() -> None:
    """Two role groups in one tenant are refused before the flag is read (#5239)."""
    with pytest.raises(HTTPException) as exc:
        _operator(
            _claims(
                meho_tenants=[f"{_TENANT_A}:lab-a:SA-Operators", f"{_TENANT_A}:lab-a:Readers"],
            )
        )
    assert exc.value.status_code == 401
    assert str(exc.value.detail).startswith("multiple_role_groups")


# ---------------------------------------------------------------------------
# The flag follows the current tenant of the request (#5239)
# ---------------------------------------------------------------------------

_ENTRY_A_OPERATORS = f"{_TENANT_A}:lab-a:Operators"
_ENTRY_B_SA = f"{_TENANT_B}:lab-b:SA-Operators"


async def test_switching_to_the_sa_tenant_turns_the_flag_on() -> None:
    """The flag is read again from the entry of the tenant the request names."""
    operator = _operator(_claims(meho_tenants=[_ENTRY_A_OPERATORS, _ENTRY_B_SA]))
    assert operator.tenant_id == _TENANT_A
    assert operator.self_approver is False

    in_b = await select_current_tenant(operator, "lab-b")
    assert in_b.tenant_id == _TENANT_B
    assert in_b.tenant_role is TenantRole.OPERATOR
    assert in_b.self_approver is True
    assert in_b.may_self_approve is True

    back_in_a = await select_current_tenant(in_b, str(_TENANT_A))
    assert back_in_a.self_approver is False
    assert back_in_a.may_self_approve is False


async def test_switching_tenants_keeps_the_flag_off_after_a_bad_entry() -> None:
    """A bad entry turns the flag off in every tenant, also after a switch."""
    operator = _operator(
        _claims(meho_tenants=[_ENTRY_A_OPERATORS, _ENTRY_B_SA, "not-a-uuid:lab-c:Readers"])
    )
    assert operator.tenants_claim_malformed is True
    in_b = await select_current_tenant(operator, "lab-b")
    assert in_b.tenant_id == _TENANT_B
    assert in_b.self_approver is False
    assert in_b.may_self_approve is False


async def test_platform_admin_in_a_tenant_without_a_group_gets_no_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform admin acts as ``tenant_admin`` there, never as an SA-Operator."""
    tenant_c = uuid.uuid4()

    async def _lookup(wanted: str) -> uuid.UUID | None:
        return tenant_c

    monkeypatch.setattr(current_tenant, "_find_existing_tenant", _lookup)
    operator = _operator(_claims(platform_admin=True, meho_tenants=[_ENTRY_B_SA]))
    assert operator.self_approver is True
    in_c = await select_current_tenant(operator, "team-c")
    assert in_c.tenant_id == tenant_c
    assert in_c.role_from_platform_admin is True
    assert in_c.self_approver is False
    assert in_c.may_self_approve is False


async def test_status_answer_without_a_chosen_tenant_gets_no_flag() -> None:
    """``tenant_chosen=False`` holds the first entry; it must not be used."""
    operator = _operator(
        _claims(meho_tenants=[f"{_TENANT_A}:lab-a:SA-Operators", _ENTRY_B_SA]),
    )
    unchosen = await select_current_tenant(operator, None, allow_unchosen=True)
    assert unchosen.tenant_chosen is False
    assert unchosen.self_approver is True
    assert unchosen.may_self_approve is False


# ---------------------------------------------------------------------------
# Machine logins never get the flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        (PrincipalKind.AGENT, {}),
        (PrincipalKind.SERVICE, {}),
        (PrincipalKind.RUNNER, {"runner_id": str(uuid.uuid4())}),
    ],
)
@pytest.mark.parametrize(
    "carrier",
    [
        pytest.param({"groups": ["MEHO-SA-Operators"]}, id="groups"),
        pytest.param({"meho_tenants": [f"{_TENANT_A}:lab-a:SA-Operators"]}, id="tenants"),
    ],
)
def test_machine_login_never_gets_the_flag(
    kind: PrincipalKind, extra: dict[str, Any], carrier: dict[str, Any]
) -> None:
    operator = _operator(_claims(principal_kind=kind.value, **extra, **carrier))
    assert operator.principal_kind is kind
    assert operator.self_approver is False
    assert operator.may_self_approve is False


@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE, PrincipalKind.RUNNER])
def test_helper_refuses_a_machine_even_with_the_field_set(kind: PrincipalKind) -> None:
    """The helper checks the principal kind itself, not only the field."""
    operator = Operator(
        sub="s",
        raw_jwt="t",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        self_approver=True,
    )
    assert operator.may_self_approve is False


# ---------------------------------------------------------------------------
# Separate from the role and the approver flag; frozen
# ---------------------------------------------------------------------------


def test_flag_changes_nothing_else() -> None:
    plain = _operator(_claims(groups=["MEHO-Operators"]))
    sa = _operator(_claims(groups=["MEHO-SA-Operators"]))
    assert sa.tenant_role is plain.tenant_role is TenantRole.OPERATOR
    assert sa.approver is plain.approver is False
    assert sa.model_dump(exclude={"self_approver"}) == plain.model_dump(exclude={"self_approver"})


def test_operator_stays_frozen() -> None:
    operator = _operator(_claims(groups=["MEHO-SA-Operators"]))
    with pytest.raises(ValidationError):
        operator.self_approver = False
    assert operator.self_approver is True


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


def test_verify_jwt_reads_the_flag_from_a_signed_token() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        key = JsonWebKey.generate_key("RSA", 2048, options={"kid": "kid-sa"}, is_private=True)
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(operator: Operator = Depends(verify_jwt)) -> dict[str, Any]:
        return {"may_self_approve": operator.may_self_approve}

    sa_token = _mint(key, {"groups": ["MEHO-SA-Operators"]})
    plain_token = _mint(key, {"groups": ["MEHO-Operators"]})
    with respx.mock as r:
        r.get(_DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json={"issuer": _ISSUER, "jwks_uri": _JWKS_URL})
        )
        r.get(_JWKS_URL).mock(
            return_value=httpx.Response(200, json={"keys": [key.as_dict(is_private=False)]})
        )
        with TestClient(app) as client:
            sa = client.get("/whoami", headers={"Authorization": f"Bearer {sa_token}"})
            plain = client.get("/whoami", headers={"Authorization": f"Bearer {plain_token}"})
    assert sa.status_code == 200, sa.text
    assert sa.json() == {"may_self_approve": True}
    assert plain.json() == {"may_self_approve": False}
