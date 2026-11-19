# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A service login reads only the audit rows of its own calls, on every REST route.

The routes under ``/api/v1/audit`` run against real rows
(:mod:`tests._audit_own_rows_seed`): rows of a service login and rows of a
person in one tenant. Each kind of service login that reaches these routes
(agent, service, a client-credentials login with no ``principal_kind``
claim) gets only its own rows: in lists, in filters such as ``has_trace``, in counts, in single-row
reads and in the session tree. A row or session of another login answers
like one that does not exist. A person keeps every row of the tenant. A
runner never reaches these routes; the unit test of ``own_rows_sub`` covers
its kind.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.api.v1.audit_counts import router as audit_counts_router
from meho_backplane.audit_query import own_rows_sub
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._audit_own_rows_seed import PERSON_SUB, SERVICE_SUB, WINDOW, WORK_REF, Seeded, seed
from ._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT = UUID("55555555-5555-5555-5555-555555555555")
_KEY = make_rsa_keypair("kid-own-rows")

#: Claims of each kind of service login. The last one is a client-credentials
#: token with no ``principal_kind`` claim: MEHO reads it as a service login.
_SERVICE_LOGINS: dict[str, dict[str, Any]] = {
    "agent": {"principal_kind": "agent"},
    "service": {"principal_kind": "service"},
    "client_credentials": {"extra_claims": {"azp": "addon-client"}},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(audit_router)
    app.include_router(audit_counts_router)
    yield TestClient(app)


@pytest.fixture
async def seeded() -> Seeded:
    return await seed(_TENANT)


def _token(login: str | None, *, role: TenantRole = TenantRole.OPERATOR) -> str:
    """A token of the service login *login*, or of the person when ``None``."""
    if login is None:
        return mint_token(_KEY, sub=PERSON_SUB, tenant_role=role.value, tenant_id=str(_TENANT))
    return mint_token(
        _KEY,
        sub=SERVICE_SUB,
        tenant_role=role.value,
        tenant_id=str(_TENANT),
        **_SERVICE_LOGINS[login],
    )


def _call(
    client: TestClient,
    method: str,
    path: str,
    *,
    login: str | None,
    role: TenantRole = TenantRole.OPERATOR,
    json: dict[str, Any] | None = None,
) -> Any:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEY))
        return client.request(
            method,
            path,
            json=json,
            headers={"Authorization": f"Bearer {_token(login, role=role)}"},
        )


def _ids(resp: Any) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()["rows"]}


_LOGINS = list(_SERVICE_LOGINS)


def test_every_machine_kind_reads_own_rows_and_a_person_reads_all() -> None:
    def op(kind: PrincipalKind) -> Operator:
        return Operator(
            sub="sub-1",
            raw_jwt="not-a-real-jwt",
            tenant_id=_TENANT,
            tenant_role=TenantRole.TENANT_ADMIN,
            principal_kind=kind,
        )

    assert own_rows_sub(op(PrincipalKind.USER)) is None
    for kind in (PrincipalKind.AGENT, PrincipalKind.SERVICE, PrincipalKind.RUNNER):
        assert own_rows_sub(op(kind)) == "sub-1"


@pytest.mark.parametrize("login", _LOGINS)
async def test_query_lists_only_the_rows_of_the_service_login(
    client: TestClient, seeded: Seeded, login: str
) -> None:
    rows = _ids(_call(client, "POST", "/api/v1/audit/query", login=login, json=WINDOW))
    mixed_own = rows - seeded.service.all_ids
    assert seeded.service.all_ids <= rows
    assert all(_sub_of(client, rid) == SERVICE_SUB for rid in mixed_own)
    assert not rows & seeded.person.all_ids
    assert str(seeded.person_child_of_service) not in rows


@pytest.mark.parametrize("login", _LOGINS)
async def test_has_trace_never_shows_a_traced_call_of_another_login(
    client: TestClient, seeded: Seeded, login: str
) -> None:
    traced = _ids(
        _call(
            client, "POST", "/api/v1/audit/query", login=login, json={**WINDOW, "has_trace": True}
        )
    )
    assert traced == {str(seeded.service.traced)}

    untraced = _ids(
        _call(
            client, "POST", "/api/v1/audit/query", login=login, json={**WINDOW, "has_trace": False}
        )
    )
    assert str(seeded.service.traced) not in untraced
    assert not untraced & seeded.person.all_ids


@pytest.mark.parametrize("login", _LOGINS)
async def test_filters_on_another_login_return_nothing(
    client: TestClient, seeded: Seeded, login: str
) -> None:
    for body in (
        {**WINDOW, "principal": PERSON_SUB},
        {**WINDOW, "audit_id": str(seeded.person.traced)},
        {**WINDOW, "agent_session_id": str(seeded.person_session)},
        {**WINDOW, "parent_audit_id": str(seeded.person.session_root)},
    ):
        assert _ids(_call(client, "POST", "/api/v1/audit/query", login=login, json=body)) == set()


@pytest.mark.parametrize("login", _LOGINS)
async def test_by_work_ref_lists_only_own_rows(
    client: TestClient, seeded: Seeded, login: str
) -> None:
    rows = _ids(_call(client, "GET", f"/api/v1/audit/by-work-ref/{WORK_REF}", login=login))
    assert seeded.service.all_ids <= rows
    assert not rows & seeded.person.all_ids


@pytest.mark.parametrize("login", _LOGINS)
async def test_class_counts_count_only_own_rows(
    client: TestClient, seeded: Seeded, login: str
) -> None:
    resp = _call(client, "POST", "/api/v1/audit/class-counts", login=login, json=WINDOW)
    assert resp.status_code == 200, resp.text
    # Five rows of its own calls plus its row in the mixed session.
    assert resp.json()["total"] == 6

    person = _call(client, "POST", "/api/v1/audit/class-counts", login=None, json=WINDOW)
    assert person.json()["total"] == 13


@pytest.mark.parametrize("login", _LOGINS)
async def test_show_and_trace_of_another_login_answer_like_a_missing_row(
    client: TestClient, seeded: Seeded, login: str
) -> None:
    missing = uuid.uuid4()
    for path in ("/api/v1/audit/show/{}", "/api/v1/audit/{}/trace"):
        foreign = _call(client, "GET", path.format(seeded.person.traced), login=login)
        absent = _call(client, "GET", path.format(missing), login=login)
        assert foreign.status_code == absent.status_code == 404
        assert foreign.json() == absent.json()

        own = _call(client, "GET", path.format(seeded.service.traced), login=login)
        assert own.status_code == 200, own.text


#: An agent acts with at most the operator role, so it never reaches replay.
@pytest.mark.parametrize("login", ["service", "client_credentials"])
async def test_replay_of_another_session_answers_like_a_missing_session(
    client: TestClient, seeded: Seeded, login: str
) -> None:
    """A tenant_admin service login replays only a session of its own calls."""
    admin = TenantRole.TENANT_ADMIN

    def replay(session_id: uuid.UUID) -> dict[str, Any]:
        resp = _call(
            client, "GET", f"/api/v1/audit/sessions/{session_id}/replay", login=login, role=admin
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        body.pop("session_id")
        return body

    absent = replay(uuid.uuid4())
    assert absent["root"] == [] and absent["row_count"] == 0
    assert replay(seeded.person_session) == absent
    mixed = replay(seeded.mixed_session)
    assert mixed["row_count"] == 1
    assert mixed["root"][0]["principal_sub"] == SERVICE_SUB
    # Only its own MCP calls with no session are counted.
    assert absent["excluded_null_session_count"] == 3

    own = replay(seeded.service_session)
    assert own["row_count"] == 2
    root = own["root"][0]
    assert root["id"] == str(seeded.service.session_root)
    # The person's row below the service login's row is not in the tree.
    assert [c["id"] for c in root["children"]] == [str(seeded.service.session_child)]


async def test_a_person_keeps_every_row_of_the_tenant(client: TestClient, seeded: Seeded) -> None:
    rows = _ids(_call(client, "POST", "/api/v1/audit/query", login=None, json=WINDOW))
    assert seeded.service.all_ids | seeded.person.all_ids <= rows

    traced = _ids(
        _call(client, "POST", "/api/v1/audit/query", login=None, json={**WINDOW, "has_trace": True})
    )
    assert traced == {str(seeded.service.traced), str(seeded.person.traced)}

    shown = _call(client, "GET", f"/api/v1/audit/show/{seeded.service.traced}", login=None)
    assert shown.status_code == 200

    resp = _call(
        client,
        "GET",
        f"/api/v1/audit/sessions/{seeded.mixed_session}/replay",
        login=None,
        role=TenantRole.TENANT_ADMIN,
    )
    assert resp.json()["row_count"] == 2
    assert resp.json()["excluded_null_session_count"] == 6

    service_tree = _call(
        client,
        "GET",
        f"/api/v1/audit/sessions/{seeded.service_session}/replay",
        login=None,
        role=TenantRole.TENANT_ADMIN,
    ).json()
    children = {c["id"] for c in service_tree["root"][0]["children"]}
    assert str(seeded.person_child_of_service) in children


def _sub_of(client: TestClient, audit_id: str) -> str:
    resp = _call(client, "GET", f"/api/v1/audit/show/{audit_id}", login=None)
    assert resp.status_code == 200, resp.text
    sub: str = resp.json()["principal_sub"]
    return sub
