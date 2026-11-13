# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/settings/conventions-drift`` and its login.

The tenant wiki's CI job sends the team rules as a settings file that holds
only conventions. MEHO answers what differs from the tenant's operational
conventions: added, changed, and only in MEHO. Covers each kind of drift, that
the check saves nothing, the mistakes it reports, and that the
``conventions_drift_only`` login can do nothing else.

Drives the production app, so the real middleware chain runs. The DB is the
autouse per-test SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.conventions_drift_login import CONVENTIONS_DRIFT_PATH
from meho_backplane.auth.jwt import _extract_conventions_drift_only
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.conventions.schemas import ConventionKind
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    InstanceSettingsRevision,
    Tenant,
    TenantConvention,
    TenantConventionHistory,
)
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_OTHER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJhZG1pbiIsImV4cCI6MTk5OTk5OTk5OX0."
    "c2lnbmF0dXJlLXZhbHVlLWhlcmU"
)

_RULES: list[dict[str, Any]] = [
    {
        "slug": "change-window",
        "title": "Change window",
        "body": "Writes only between 20:00 and 06:00.",
        "kind": "operational",
        "priority": 5,
    },
    {
        "slug": "read-first",
        "title": "Read first",
        "body": "Read the target before you change it.",
        "kind": "operational",
        "priority": 1,
    },
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _file(conventions: list[dict[str, Any]] | None, **tenant: Any) -> str:
    block: dict[str, Any] = {**tenant}
    if conventions is not None:
        block["conventions"] = conventions
    return yaml.safe_dump(
        {"apiVersion": "meho.evoila.com/v1", "kind": "Settings", "spec": {"tenants": [block]}}
    )


def _token(key: Any, role: TenantRole = TenantRole.READ_ONLY, **claims: Any) -> str:
    return mint_token(
        key,
        sub="service-account-conventions-drift",
        tenant_role=role.value,
        tenant_id=str(_TENANT),
        principal_kind="service",
        extra_claims=claims or None,
    )


def _checker(key: Any) -> str:
    return _token(key, conventions_drift_only=True)


def _call(
    client: TestClient,
    method: str,
    path: str,
    *,
    content: str | None = None,
    role: TenantRole = TenantRole.READ_ONLY,
    **claims: Any,
) -> Any:
    key = make_rsa_keypair("kid-drift")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, role, **claims)
        return client.request(
            method,
            path,
            content=content.encode() if content is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
        )


def _drift(client: TestClient, content: str, **claims: Any) -> Any:
    claims = claims or {"conventions_drift_only": True}
    return _call(client, "POST", CONVENTIONS_DRIFT_PATH, content=content, **claims)


async def _seed(*rows: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
        for row in rows:
            session.add(
                TenantConvention(id=uuid.uuid4(), tenant_id=_TENANT, created_by_sub="seed", **row)
            )
        await session.commit()


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


# --- the three kinds of drift -------------------------------------------------


@pytest.mark.asyncio
async def test_rules_and_conventions_that_match_are_in_sync(client: TestClient) -> None:
    await _seed(*_RULES)
    resp = _drift(client, _file(_RULES))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "in_sync": True,
        "mistakes": [],
        "added": [],
        "changed": [],
        "only_in_meho": [],
    }


@pytest.mark.asyncio
async def test_a_convention_changed_by_hand_in_meho_is_changed(client: TestClient) -> None:
    await _seed({**_RULES[0], "body": "Writes at any time."}, _RULES[1])
    body = _drift(client, _file(_RULES)).json()
    assert body["in_sync"] is False
    assert body["ok"] is True
    assert body["changed"] == [
        {
            "slug": "change-window",
            "before": {"body": "Writes at any time."},
            "after": {"body": "Writes only between 20:00 and 06:00."},
        }
    ]
    assert body["added"] == [] and body["only_in_meho"] == []


@pytest.mark.asyncio
async def test_a_rule_missing_in_meho_is_added(client: TestClient) -> None:
    await _seed(_RULES[0])
    body = _drift(client, _file(_RULES)).json()
    assert body["in_sync"] is False
    assert body["added"] == [{"slug": "read-first", "value": _RULES[1]}]
    assert body["changed"] == [] and body["only_in_meho"] == []


@pytest.mark.asyncio
async def test_a_convention_only_in_meho_is_reported(client: TestClient) -> None:
    await _seed(*_RULES)
    body = _drift(client, _file(_RULES[:1])).json()
    assert body["in_sync"] is False
    assert body["only_in_meho"] == [{"slug": "read-first"}]
    assert body["added"] == [] and body["changed"] == []


@pytest.mark.asyncio
async def test_an_empty_list_reports_every_operational_convention(client: TestClient) -> None:
    await _seed(*_RULES)
    body = _drift(client, _file([])).json()
    assert body["ok"] is True
    assert body["only_in_meho"] == [{"slug": "change-window"}, {"slug": "read-first"}]


@pytest.mark.asyncio
async def test_other_kinds_of_convention_are_not_compared(client: TestClient) -> None:
    await _seed(
        *_RULES,
        {
            "slug": "how-to-page",
            "title": "How to",
            "body": "Steps.",
            "kind": ConventionKind.WORKFLOW.value,
        },
    )
    body = _drift(client, _file(_RULES)).json()
    assert body["in_sync"] is True


@pytest.mark.asyncio
async def test_a_field_the_file_leaves_out_is_not_compared(client: TestClient) -> None:
    await _seed({**_RULES[0], "priority": 9, "title": "Old title"})
    rule = {k: v for k, v in _RULES[0].items() if k != "title"}
    body = _drift(client, _file([rule])).json()
    assert body["changed"] == [
        {"slug": "change-window", "before": {"priority": 9}, "after": {"priority": 5}}
    ]


# --- it saves nothing -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_check_saves_nothing_and_writes_one_read_audit_row(client: TestClient) -> None:
    await _seed({**_RULES[0], "body": "Writes at any time."})
    history_before = len(await _rows(TenantConventionHistory))
    resp = _drift(client, _file(_RULES))
    assert resp.status_code == 200, resp.text
    (row,) = [r for r in await _rows(TenantConvention) if r.tenant_id == _TENANT]
    assert row.body == "Writes at any time."
    assert len(await _rows(TenantConventionHistory)) == history_before
    assert await _rows(InstanceSettingsRevision) == []
    async with get_sessionmaker()() as session:
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.path == CONVENTIONS_DRIFT_PATH)))
            .scalars()
            .all()
        )
    (audit,) = audits
    assert audit.payload["op_id"] == "settings.conventions_drift"
    assert audit.payload["op_class"] == "read"
    assert audit.payload["in_sync"] is False
    assert audit.payload["added_count"] == 1
    assert audit.payload["changed_count"] == 1
    assert "Writes only" not in str(audit.payload)


# --- mistakes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (_file(_RULES, policies={"announce_gate_enabled": True}), "spec.tenants[0].policies"),
        (_file(_RULES, targets=[]), "spec.tenants[0].targets"),
        (_file(None), "spec.tenants[0].conventions` is required"),
        (_file([{**_RULES[0], "kind": "reference"}]), "conventions[0].kind` must be"),
        (
            _file([{k: v for k, v in _RULES[0].items() if k != "kind"}]),
            "conventions[0].kind` must be",
        ),
        (
            yaml.safe_dump(
                {
                    "apiVersion": "meho.evoila.com/v1",
                    "kind": "Settings",
                    "spec": {"tenants": [{"conventions": []}, {"id": str(_OTHER)}]},
                }
            ),
            "exactly one tenant block",
        ),
        (_file([{**_RULES[0], "unknown": 1}]), "unknown"),
        ("apiVersion: meho.evoila.com/v2\nkind: Settings\nspec: {tenants: []}\n", "apiVersion"),
    ],
)
def test_a_bad_file_is_a_mistake_and_not_in_sync(
    client: TestClient, content: str, expected: str
) -> None:
    resp = _drift(client, content)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["in_sync"] is False
    assert any(expected in m for m in body["mistakes"]), body["mistakes"]
    assert body["added"] == body["changed"] == body["only_in_meho"] == []


@pytest.mark.asyncio
async def test_a_kind_change_is_a_mistake(client: TestClient) -> None:
    await _seed({**_RULES[0], "kind": ConventionKind.REFERENCE.value})
    body = _drift(client, _file(_RULES[:1])).json()
    assert body["ok"] is False
    assert any("kind` cannot change" in m for m in body["mistakes"])


def test_a_secret_in_the_file_is_refused_without_echo(client: TestClient) -> None:
    resp = _drift(client, _file([{**_RULES[0], "body": f"Token: {_JWT}"}]))
    body = resp.json()
    assert body["ok"] is False
    assert any("spec.tenants[0].conventions[0].body" in m for m in body["mistakes"])
    assert _JWT.split(".")[1] not in resp.text


@pytest.mark.asyncio
async def test_a_stored_secret_stops_the_check_without_echo(client: TestClient) -> None:
    await _seed({**_RULES[0], "body": f"Token: {_JWT}"})
    resp = _drift(client, _file(_RULES[:1]))
    body = resp.json()
    assert body["ok"] is False
    assert body["mistakes"][0].startswith("MEHO already stores a value that looks like a secret")
    assert body["changed"] == []
    assert _JWT.split(".")[1] not in resp.text


def test_another_tenant_is_403(client: TestClient) -> None:
    resp = _drift(client, _file(_RULES, id=str(_OTHER)))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_own_tenant_id_may_be_named(client: TestClient) -> None:
    await _seed(*_RULES)
    assert _drift(client, _file(_RULES, id=str(_TENANT))).json()["in_sync"] is True


# --- who may call -------------------------------------------------------------------


def test_no_token_is_401(client: TestClient) -> None:
    assert client.post(CONVENTIONS_DRIFT_PATH, content=b"x").status_code == 401


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
def test_a_login_without_the_claim_below_tenant_admin_is_refused(
    client: TestClient, role: TenantRole
) -> None:
    resp = _drift(client, _file(_RULES), conventions_drift_only=False, role=role)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient_role"


@pytest.mark.asyncio
async def test_a_tenant_admin_may_run_the_check(client: TestClient) -> None:
    await _seed(*_RULES)
    resp = _call(
        client, "POST", CONVENTIONS_DRIFT_PATH, content=_file(_RULES), role=TenantRole.TENANT_ADMIN
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["in_sync"] is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/settings/import?preview=true"),
        ("POST", "/api/v1/settings/import"),
        ("GET", "/api/v1/settings/export"),
        ("GET", "/api/v1/settings/history"),
        ("GET", "/api/v1/conventions"),
        ("POST", "/api/v1/kb/import"),
        ("GET", "/api/v1/conventions/change-window"),
    ],
)
@pytest.mark.parametrize("role", [TenantRole.READ_ONLY, TenantRole.TENANT_ADMIN])
def test_the_drift_login_is_refused_on_every_other_route(
    client: TestClient, method: str, path: str, role: TenantRole
) -> None:
    resp = _call(
        client, method, path, content=_file(_RULES), role=role, conventions_drift_only=True
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "conventions_drift_only_scope_violation"


@pytest.mark.parametrize("value", [1, "yes", ["true"], {"on": True}])
def test_a_claim_meho_cannot_read_keeps_the_login_limited(client: TestClient, value: Any) -> None:
    resp = _call(
        client,
        "GET",
        "/api/v1/settings/export",
        role=TenantRole.TENANT_ADMIN,
        conventions_drift_only=value,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "conventions_drift_only_scope_violation"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({}, False),
        ({"conventions_drift_only": False}, False),
        ({"conventions_drift_only": "FALSE"}, False),
        ({"conventions_drift_only": True}, True),
        ({"conventions_drift_only": " true "}, True),
        ({"conventions_drift_only": 0}, True),
        ({"conventions_drift_only": None}, False),
    ],
)
def test_the_claim_is_read(claims: dict[str, Any], expected: bool) -> None:
    assert _extract_conventions_drift_only(claims) is expected


@pytest.mark.asyncio
async def test_the_drift_login_has_no_mcp_surface() -> None:
    operator = Operator(
        sub="service-account-conventions-drift",
        raw_jwt="x",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.READ_ONLY,
        conventions_drift_only=True,
    )
    with pytest.raises(HTTPException) as caught:
        await verify_mcp_jwt_and_bind(operator)
    assert caught.value.status_code == 403
    assert caught.value.detail == "conventions_drift_only_scope_violation"


def test_the_route_path_is_the_one_the_cage_allows() -> None:
    assert "post" in app.openapi()["paths"][CONVENTIONS_DRIFT_PATH]
