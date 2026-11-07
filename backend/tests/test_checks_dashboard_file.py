# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Export a Dashboard and its Sensors to a file, and import it elsewhere.

What these tests check:

* Export from tenant A, import into tenant B with a target map: the Sensors
  in B equal the Sensors in A, with B's target names.
* Importing the same file twice changes nothing the second time.
* A changed Sensor is updated in place; a member that is not in the file
  leaves the Dashboard but is not deleted.
* ``dry_run`` writes nothing and says what would change.
* The bad cases, each refused with a list and nothing written: an unmapped
  target, a target that is not found, an unknown or unsafe operation, a
  connector that does not fit its target (unless ``force``), a bad file.
* Roles and tenants: an operator may export but not import; another
  tenant's Dashboard is not found.
* The import writes an audit row that names what changed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors import (
    Connector,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    CheckDashboard,
    EndpointDescriptor,
    Sensor,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_PRODUCT = "fileprod"
_CONNECTOR = "fileprod-rest-9.0"
_OP = "fileprod.item.list"
_WRITE_OP = "fileprod.item.delete"
_EXPORT = "/api/v1/checks/dashboard-files/export"
_IMPORT = "/api/v1/checks/dashboard-files/import"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


class _Fake9(Connector):
    product = _PRODUCT
    impl_id = "fileprod-rest"
    version = "9.0"
    supported_version_range = ">=9.0,<10.0"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(connector_registry._REGISTRY_V2, (_PRODUCT, "9.0", "fileprod-rest"), _Fake9)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def key() -> Iterator[Any]:
    rsa = make_rsa_keypair("kid-dashboard-file")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(rsa))
        yield rsa


def _headers(
    key: Any, *, tenant_id: uuid.UUID, role: TenantRole = TenantRole.TENANT_ADMIN
) -> dict[str, str]:
    token = mint_token(key, sub="op-admin", tenant_role=role.value, tenant_id=str(tenant_id))
    return {"Authorization": f"Bearer {token}"}


async def _seed_tenants() -> None:
    async with get_sessionmaker()() as session:
        for tenant_id in (_TENANT_A, _TENANT_B):
            if await session.get(Tenant, tenant_id) is None:
                slug = str(tenant_id)[:8]
                session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        for op_id, safety in ((_OP, "safe"), (_WRITE_OP, "dangerous")):
            session.add(
                EndpointDescriptor(
                    product=_PRODUCT,
                    version="9.0",
                    impl_id="fileprod-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=f"/{op_id}",
                    parameter_schema={"type": "object", "properties": {}},
                    safety_level=safety,
                )
            )
        await session.commit()


async def _seed_target(name: str, tenant_id: uuid.UUID, *, probed: str = "9.0.2") -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=name,
                aliases=[],
                product=_PRODUCT,
                version=None,
                fingerprint={"version": probed},
                host="appliance.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=now,
                updated_at=now,
            )
        )


def _sensor_body(name: str, target: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "connector_id": _CONNECTOR,
        "op_id": _OP,
        "target": None if target is None else {"name": target},
        "params": {"limit": 5},
        "assertion": _ASSERTION,
        "cadence_kind": "interval",
        "interval_seconds": 120,
        "severity": "degraded",
        "for_seconds": 60,
        "retry_times": 2,
        "retry_backoff_seconds": 20,
    }
    body.update(extra)
    return body


def _build_source(client: TestClient, headers: dict[str, str]) -> None:
    """Tenant A: a Dashboard with two target Sensors and one without a target."""
    ids = []
    for body in (
        _sensor_body("items-a", "site-a-one"),
        _sensor_body(
            "items-b",
            "site-a-two",
            cadence_kind="cron",
            interval_seconds=None,
            cron_expr="*/5 * * * *",
            timezone="Europe/Vienna",
        ),
        _sensor_body("items-c", None, severity="critical", params={}),
    ):
        resp = client.post("/api/v1/sensors", json=body, headers=headers)
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    resp = client.post(
        "/api/v1/checks/dashboards",
        json={
            "name": "estate",
            "description": "standing health check",
            "sensor_ids": ids,
            "notify_email": "ops@example.com",
            "notify_min_state": "degraded",
            "investigator_prompt": "look at the item count first",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


async def _setup(client: TestClient, key: Any) -> dict[str, Any]:
    """Seed both tenants, build the Dashboard in A, return its exported file."""
    await _seed_tenants()
    await _seed_target("site-a-one", _TENANT_A)
    await _seed_target("site-a-two", _TENANT_A)
    await _seed_target("site-b-one", _TENANT_B)
    await _seed_target("site-b-two", _TENANT_B)
    _build_source(client, _headers(key, tenant_id=_TENANT_A))
    resp = client.get(
        _EXPORT, params={"name": "estate"}, headers=_headers(key, tenant_id=_TENANT_A)
    )
    assert resp.status_code == 200, resp.text
    exported: dict[str, Any] = resp.json()
    return exported


_MAP_B = {"site-a-one": "site-b-one", "site-a-two": "site-b-two"}

_COMPARED = (
    "connector_id",
    "op_id",
    "params",
    "assertion",
    "cadence_kind",
    "interval_seconds",
    "cron_expr",
    "timezone",
    "severity",
    "for_seconds",
    "retry_times",
    "retry_backoff_seconds",
)


async def _sensors(tenant_id: uuid.UUID) -> dict[str, Sensor]:
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(Sensor).where(Sensor.tenant_id == tenant_id))).all()
    return {row.name: row for row in rows}


async def _counts() -> tuple[int, int]:
    async with get_sessionmaker()() as session:
        sensors = await session.scalar(select(func.count()).select_from(Sensor))
        dashboards = await session.scalar(select(func.count()).select_from(CheckDashboard))
    return int(sensors or 0), int(dashboards or 0)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_holds_settings_and_placeholders(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    assert exported["format"] == "meho-dashboard/1"
    assert exported["dashboard"] == {
        "name": "estate",
        "description": "standing health check",
        "notify_min_state": "degraded",
        "investigator_prompt": "look at the item count first",
    }
    by_name = {s["name"]: s for s in exported["sensors"]}
    assert sorted(by_name) == ["items-a", "items-b", "items-c"]
    assert by_name["items-a"]["target"] == "site-a-one"
    assert by_name["items-b"]["target"] == "site-a-two"
    assert by_name["items-c"]["target"] is None
    assert by_name["items-b"]["cron_expr"] == "*/5 * * * *"
    # No ids, no state and no mail address in the file.
    text = str(exported)
    assert "ops@example.com" not in text
    assert "last_state" not in text and "'id'" not in text


@pytest.mark.asyncio
async def test_export_unknown_or_other_tenant_is_not_found(client: TestClient, key: Any) -> None:
    await _setup(client, key)
    missing = client.get(
        _EXPORT, params={"name": "nope"}, headers=_headers(key, tenant_id=_TENANT_A)
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "dashboard_not_found"
    other = client.get(
        _EXPORT, params={"name": "estate"}, headers=_headers(key, tenant_id=_TENANT_B)
    )
    assert other.status_code == 404


@pytest.mark.asyncio
async def test_operator_may_export_but_not_import(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    operator = _headers(key, tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    assert client.get(_EXPORT, params={"name": "estate"}, headers=operator).status_code == 200
    resp = client.post(_IMPORT, json={"file": exported, "target_map": _MAP_B}, headers=operator)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_from_a_import_into_b_gives_equal_sensors(
    client: TestClient, key: Any
) -> None:
    exported = await _setup(client, key)
    headers_b = _headers(key, tenant_id=_TENANT_B)
    resp = client.post(_IMPORT, json={"file": exported, "target_map": _MAP_B}, headers=headers_b)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["changed"] is True
    assert result["dashboard_action"] == "create"
    assert {s["name"]: s["action"] for s in result["sensors"]} == {
        "items-a": "create",
        "items-b": "create",
        "items-c": "create",
    }
    assert result["members_added"] == ["items-a", "items-b", "items-c"]

    in_a, in_b = await _sensors(_TENANT_A), await _sensors(_TENANT_B)
    assert sorted(in_b) == sorted(in_a)
    for name, row_a in in_a.items():
        row_b = in_b[name]
        for field in _COMPARED:
            assert getattr(row_b, field) == getattr(row_a, field), (name, field)
        assert row_b.identity_sub == "__sensor__"
    assert in_b["items-a"].target == {"name": "site-b-one"}
    assert in_b["items-b"].target == {"name": "site-b-two"}
    assert in_b["items-c"].target is None

    # Exporting from B gives the same file, with B's target names.
    again = client.get(_EXPORT, params={"name": "estate"}, headers=headers_b).json()
    renamed = {v: k for k, v in _MAP_B.items()}
    for sensor in again["sensors"]:
        if sensor["target"] is not None:
            sensor["target"] = renamed[sensor["target"]]
    assert again == exported

    dash = client.get(
        f"/api/v1/checks/dashboards/{result['dashboard_id']}", headers=headers_b
    ).json()
    assert dash["notify_email"] is None
    assert sorted(m["name"] for m in dash["members"]) == ["items-a", "items-b", "items-c"]


@pytest.mark.asyncio
async def test_import_twice_changes_nothing_the_second_time(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    headers_b = _headers(key, tenant_id=_TENANT_B)
    body = {"file": exported, "target_map": _MAP_B}
    first = client.post(_IMPORT, json=body, headers=headers_b)
    assert first.status_code == 200, first.text
    before = {
        name: (row.updated_at, row.next_fire_at)
        for name, row in (await _sensors(_TENANT_B)).items()
    }
    counts = await _counts()

    second = client.post(_IMPORT, json=body, headers=headers_b)
    assert second.status_code == 200, second.text
    result = second.json()
    assert result["changed"] is False
    assert result["dashboard_action"] == "unchanged"
    assert result["dashboard_id"] == first.json()["dashboard_id"]
    assert {s["action"] for s in result["sensors"]} == {"unchanged"}
    assert result["members_added"] == [] and result["members_removed"] == []
    assert await _counts() == counts
    after = {
        name: (row.updated_at, row.next_fire_at)
        for name, row in (await _sensors(_TENANT_B)).items()
    }
    assert after == before


@pytest.mark.asyncio
async def test_import_into_the_same_tenant_changes_nothing(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    identity = {"site-a-one": "site-a-one", "site-a-two": "site-a-two"}
    resp = client.post(
        _IMPORT,
        json={"file": exported, "target_map": identity},
        headers=_headers(key, tenant_id=_TENANT_A),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["changed"] is False


@pytest.mark.asyncio
async def test_import_updates_changed_sensor_and_drops_extra_member(
    client: TestClient, key: Any
) -> None:
    exported = await _setup(client, key)
    headers_b = _headers(key, tenant_id=_TENANT_B)
    first = client.post(_IMPORT, json={"file": exported, "target_map": _MAP_B}, headers=headers_b)
    assert first.status_code == 200, first.text
    dashboard_id = first.json()["dashboard_id"]
    extra = client.post("/api/v1/sensors", json=_sensor_body("local-only", None), headers=headers_b)
    assert extra.status_code == 201, extra.text
    patched = client.patch(
        f"/api/v1/checks/dashboards/{dashboard_id}",
        json={"add_sensor_ids": [extra.json()["id"]], "notify_email": "site-b@example.com"},
        headers=headers_b,
    )
    assert patched.status_code == 200, patched.text
    old_id = (await _sensors(_TENANT_B))["items-a"].id

    changed = dict(exported)
    changed["sensors"] = [
        {**s, "interval_seconds": 300, "severity": "critical"} if s["name"] == "items-a" else s
        for s in exported["sensors"]
    ]
    changed["dashboard"] = {**exported["dashboard"], "description": "new text"}
    resp = client.post(_IMPORT, json={"file": changed, "target_map": _MAP_B}, headers=headers_b)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    by_name = {s["name"]: s for s in result["sensors"]}
    assert by_name["items-a"]["action"] == "update"
    assert sorted(by_name["items-a"]["changed_fields"]) == ["interval_seconds", "severity"]
    assert by_name["items-b"]["action"] == "unchanged"
    assert result["dashboard_action"] == "update"
    assert result["dashboard_changed_fields"] == ["description"]
    assert result["members_removed"] == ["local-only"]

    rows = await _sensors(_TENANT_B)
    assert rows["items-a"].id == old_id
    assert rows["items-a"].interval_seconds == 300
    assert "local-only" in rows  # left the Dashboard, not deleted
    dash = client.get(f"/api/v1/checks/dashboards/{dashboard_id}", headers=headers_b).json()
    assert sorted(m["name"] for m in dash["members"]) == ["items-a", "items-b", "items-c"]
    assert dash["description"] == "new text"
    assert dash["notify_email"] == "site-b@example.com"  # the site's own mail setting stays


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    counts = await _counts()
    resp = client.post(
        _IMPORT,
        json={"file": exported, "target_map": _MAP_B, "dry_run": True},
        headers=_headers(key, tenant_id=_TENANT_B),
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["dry_run"] is True
    assert result["changed"] is True
    assert result["dashboard_action"] == "create"
    assert result["dashboard_id"] is None
    assert {s["action"] for s in result["sensors"]} == {"create"}
    assert await _counts() == counts
    assert await _sensors(_TENANT_B) == {}


# ---------------------------------------------------------------------------
# Refusals: a list of every problem, nothing written
# ---------------------------------------------------------------------------


def _problems(resp: Any) -> list[tuple[str, str]]:
    assert resp.status_code == 422, resp.text
    return [(entry["type"], entry["msg"]) for entry in resp.json()["detail"]]


@pytest.mark.asyncio
async def test_unmapped_target_is_named(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    counts = await _counts()
    resp = client.post(
        _IMPORT,
        json={"file": exported, "target_map": {"site-a-one": "site-b-one"}},
        headers=_headers(key, tenant_id=_TENANT_B),
    )
    problems = _problems(resp)
    assert [kind for kind, _ in problems] == ["dashboard_import_unmapped_target"]
    assert "'site-a-two' is not mapped" in problems[0][1]
    assert "--map-target site-a-two=" in problems[0][1]
    assert await _counts() == counts


@pytest.mark.asyncio
async def test_missing_target_and_unknown_and_unsafe_operations_are_listed(
    client: TestClient, key: Any
) -> None:
    exported = await _setup(client, key)
    broken = dict(exported)
    broken["sensors"] = [
        {**exported["sensors"][0], "op_id": "fileprod.item.gone"},
        {**exported["sensors"][1], "op_id": _WRITE_OP},
        exported["sensors"][2],
    ]
    counts = await _counts()
    resp = client.post(
        _IMPORT,
        json={"file": broken, "target_map": {**_MAP_B, "site-a-two": "no-such-target"}},
        headers=_headers(key, tenant_id=_TENANT_B),
    )
    kinds = sorted(kind for kind, _ in _problems(resp))
    assert kinds == [
        "dashboard_import_target_not_found",
        "dashboard_import_unknown_operation",
        "dashboard_import_unsafe_operation",
    ]
    messages = " ".join(msg for _, msg in _problems(resp))
    assert "no-such-target" in messages
    assert "fileprod.item.gone" in messages
    assert await _counts() == counts


@pytest.mark.asyncio
async def test_connector_that_does_not_fit_needs_force(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    await _seed_target("old-appliance", _TENANT_B, probed="8.0.1")
    target_map = {**_MAP_B, "site-a-one": "old-appliance"}
    headers_b = _headers(key, tenant_id=_TENANT_B)
    refused = client.post(
        _IMPORT, json={"file": exported, "target_map": target_map}, headers=headers_b
    )
    problems = _problems(refused)
    assert [kind for kind, _ in problems] == ["dashboard_import_connector_mismatch"]
    assert "items-a" in problems[0][1]
    assert await _sensors(_TENANT_B) == {}

    forced = client.post(
        _IMPORT,
        json={"file": exported, "target_map": target_map, "force": True},
        headers=headers_b,
    )
    assert forced.status_code == 200, forced.text
    assert (await _sensors(_TENANT_B))["items-a"].target == {"name": "old-appliance"}


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"format": "meho-dashboard/2"}, id="unknown-format"),
        pytest.param({"extra": 1}, id="unknown-field"),
        pytest.param({"sensors": "twice"}, id="same-sensor-twice"),
        pytest.param({"sensors": "bad-cadence"}, id="bad-cadence"),
    ],
)
@pytest.mark.asyncio
async def test_bad_file_is_refused(client: TestClient, key: Any, change: dict[str, Any]) -> None:
    exported = await _setup(client, key)
    bad = dict(exported)
    if change.get("sensors") == "twice":
        bad["sensors"] = [exported["sensors"][2], exported["sensors"][2]]
    elif change.get("sensors") == "bad-cadence":
        bad["sensors"] = [{**exported["sensors"][2], "interval_seconds": None}]
    else:
        bad.update(change)
    resp = client.post(
        _IMPORT,
        json={"file": bad, "target_map": _MAP_B},
        headers=_headers(key, tenant_id=_TENANT_B),
    )
    assert resp.status_code == 422, resp.text
    assert await _sensors(_TENANT_B) == {}


@pytest.mark.asyncio
async def test_import_into_another_tenant_needs_platform_admin(
    client: TestClient, key: Any
) -> None:
    exported = await _setup(client, key)
    resp = client.post(
        _IMPORT,
        json={"file": exported, "target_map": _MAP_B, "tenant_id": str(_TENANT_B)},
        headers=_headers(key, tenant_id=_TENANT_A),
    )
    assert resp.status_code == 403
    assert await _sensors(_TENANT_B) == {}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_writes_an_audit_row(client: TestClient, key: Any) -> None:
    exported = await _setup(client, key)
    resp = client.post(
        _IMPORT,
        json={"file": exported, "target_map": _MAP_B},
        headers=_headers(key, tenant_id=_TENANT_B),
    )
    assert resp.status_code == 200, resp.text
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.path == _IMPORT, AuditLog.method == "POST")
            )
        ).all()
    assert len(rows) == 1
    payload = dict(rows[0].payload or {})
    assert payload["op_id"] == "dashboard.import"
    assert payload["dashboard_name"] == "estate"
    assert payload["dashboard_id"] == resp.json()["dashboard_id"]
    assert payload["dry_run"] is False
    assert payload["target_map"] == _MAP_B
    assert payload["import"]["sensors"] == {
        "items-a": "create",
        "items-b": "create",
        "items-c": "create",
    }
    assert rows[0].tenant_id == _TENANT_B
