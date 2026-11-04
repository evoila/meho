# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``/api/v1/settings`` (the settings file, ``core.md`` §5).

Covers the acceptance criteria that run in-process:

* Round trip: export, wipe the tenant's settings, import, export again -- the
  ``spec`` part is byte-identical. (The kind integration test runs the same
  check across two installs.)
* A file with three mistakes (unknown key, unknown ``apiVersion``, a target
  with no host) yields three sentences in the preview and writes nothing.
* The preview diff names every change, including "not in file".
* The apply records a revision with the person who did it; ``history`` lists
  it. Importing the same file again gives an empty diff and no second revision.
* No token -> 401 on all four endpoints. Wrong role / other tenant -> 403.
* An export carries no JWT, password or PEM-key pattern; a file with a
  secret-shaped value is refused without showing the value.

Drives the production ``meho_backplane.main:app`` so the real middleware chain
(RequestContext -> Audit -> router) runs. DB is the autouse per-test SQLite +
``alembic upgrade head``.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from meho_backplane.auth.operator import TenantRole
from meho_backplane.conventions.schemas import ConventionKind
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    InstanceSettingsRevision,
    Target,
    Tenant,
    TenantConvention,
    TenantConventionHistory,
)
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.main import app

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_OTHER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

#: The CI check that greps an export for JWT, password and PEM-key patterns.
_EXPORT_SECRET_CHECK = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check-settings-export-secrets.sh"
)

_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBszCCAVmgAwIBAgIUQ9w7GQ0e2z0b9wq6z4mJ3C1qkGgwCgYIKoZIzj0EAwIw
-----END CERTIFICATE-----
"""


@pytest.fixture(autouse=True)
def _reset_policy_caches() -> Iterator[None]:
    reset_flight_recorder_config_cache_for_testing()
    yield
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _token(
    key: Any,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: uuid.UUID = _TENANT,
    platform_admin: bool | None = None,
    principal_kind: str | None = None,
    sub: str = "admin-person",
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(tenant_id),
        platform_admin=platform_admin,
        principal_kind=principal_kind,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _export(client: TestClient, token: str) -> str:
    resp = client.get("/api/v1/settings/export", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/yaml")
    return resp.text


def _import(client: TestClient, token: str, content: str, *, preview: bool = False) -> Any:
    return client.post(
        "/api/v1/settings/import",
        params={"preview": "true"} if preview else None,
        content=content.encode(),
        headers={**_auth(token), "Content-Type": "application/yaml"},
    )


def _spec_yaml(exported: str) -> str:
    """The ``spec`` part of an export, re-rendered for a byte comparison."""
    return yaml.safe_dump(yaml.safe_load(exported)["spec"], sort_keys=False)


async def _seed() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Tenant(
                id=_TENANT,
                slug="tenant-a",
                name="Tenant A",
                announce_gate_enabled=True,
                flight_recorder_enabled=True,
                flight_recorder_retention_days=14,
                mail_recipient_allowlist="@example.com",
            )
        )
        session.add(
            TenantConvention(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                slug="change-window",
                title="Change window",
                body="Writes only between 20:00 and 06:00.",
                kind=ConventionKind.OPERATIONAL.value,
                priority=5,
                created_by_sub="seed",
            )
        )
        await session.commit()
    await _insert_target(
        tenant_id=_TENANT,
        name="vc-01",
        product="vsphere",
        host="10.1.0.1",
        version="8.0",
        secret_ref=f"tenants/{_TENANT}/vc-01",
        tls_ca_pin=None,
    )
    await _insert_target(
        tenant_id=_TENANT,
        name="prom",
        product="prometheus",
        host="10.1.0.2",
        port=9090,
        secret_ref=None,
        extras={"scrape_path": "/metrics"},
    )


async def _wipe_tenant_settings() -> None:
    """Make tenant A look like a fresh install B (tenant row kept at defaults)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(delete(Target).where(Target.tenant_id == _TENANT))
        await session.execute(delete(TenantConventionHistory))
        await session.execute(delete(TenantConvention).where(TenantConvention.tenant_id == _TENANT))
        await session.execute(
            update(Tenant)
            .where(Tenant.id == _TENANT)
            .values(
                announce_gate_enabled=False,
                flight_recorder_enabled=False,
                flight_recorder_retention_days=None,
                flight_recorder_agent_readable=None,
                mail_recipient_allowlist=None,
            )
        )
        await session.commit()


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


# ---------------------------------------------------------------------------
# 401 / 403
# ---------------------------------------------------------------------------


def test_every_endpoint_needs_a_token(client: TestClient) -> None:
    assert client.get("/api/v1/settings/export").status_code == 401
    assert client.get("/api/v1/settings/history").status_code == 401
    assert client.post("/api/v1/settings/import", content=b"x").status_code == 401
    assert client.post("/api/v1/settings/import?preview=true", content=b"x").status_code == 401


@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
def test_other_roles_are_refused(client: TestClient, role: TenantRole) -> None:
    key = make_rsa_keypair("kid-role")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, role=role)
        assert client.get("/api/v1/settings/export", headers=_auth(token)).status_code == 403
        assert _import(client, token, "x", preview=True).status_code == 403
        assert client.get("/api/v1/settings/history", headers=_auth(token)).status_code == 403


def test_tenant_admin_cannot_import_another_tenant(client: TestClient) -> None:
    content = (
        "apiVersion: meho.evoila.com/v1\nkind: Settings\n"
        f"spec:\n  tenants:\n    - id: {_OTHER}\n      policies: {{announce_gate_enabled: true}}\n"
    )
    key = make_rsa_keypair("kid-other")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        assert _import(client, _token(key), content, preview=True).status_code == 403
        assert _import(client, _token(key), content).status_code == 403
        assert (
            _import(client, _token(key, platform_admin=True), content, preview=True).status_code
            == 200
        )


def test_machine_principal_cannot_import(client: TestClient) -> None:
    key = make_rsa_keypair("kid-agent")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, principal_kind="agent")
        resp = _import(client, token, "apiVersion: meho.evoila.com/v1", preview=True)
    assert resp.status_code == 403
    assert "human_principal_required" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_shape_and_no_secrets(client: TestClient) -> None:
    await _seed()
    key = make_rsa_keypair("kid-export")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        exported = _export(client, _token(key))
    doc = yaml.safe_load(exported)
    assert doc["apiVersion"] == "meho.evoila.com/v1"
    assert doc["kind"] == "Settings"
    assert set(doc["metadata"]) == {"exportedAt", "exportedFrom"}
    assert doc["metadata"]["exportedFrom"]["migrationHead"] == "0136"
    (tenant,) = doc["spec"]["tenants"]
    assert tenant["id"] == str(_TENANT)
    assert tenant["policies"] == {
        "announce_gate_enabled": True,
        "flight_recorder": {"enabled": True, "retention_days": 14, "agent_readable": None},
        "mail_recipient_allowlist": "@example.com",
    }
    assert [c["slug"] for c in tenant["conventions"]] == ["change-window"]
    targets = {t["name"]: t for t in tenant["targets"]}
    # Derivable secret_ref is left out; an explicit "no credential" stays.
    assert "secret_ref" not in targets["vc-01"]
    assert targets["prom"]["secret_ref"] is None
    assert targets["vc-01"]["version"] == "8.0"
    for server_managed in (
        "id",
        "tenant_id",
        "fingerprint",
        "created_at",
        "updated_at",
        "deleted_at",
    ):
        assert server_managed not in targets["vc-01"]
    check = subprocess.run(
        ["bash", str(_EXPORT_SECRET_CHECK), "-"],
        input=exported,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr


# ---------------------------------------------------------------------------
# Round trip, revision, history, idempotence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_spec_is_byte_identical(client: TestClient) -> None:
    await _seed()
    key = make_rsa_keypair("kid-rt")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key)
        first = _export(client, token)
        await _wipe_tenant_settings()

        applied = _import(client, token, first)
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["ok"] is True, body
        assert body["applied"] is True
        assert body["revision_id"]

        second = _export(client, token)
    assert _spec_yaml(second) == _spec_yaml(first)


@pytest.mark.asyncio
async def test_apply_records_revision_and_second_import_is_a_no_op(client: TestClient) -> None:
    await _seed()
    key = make_rsa_keypair("kid-rev")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, sub="dana")
        exported = _export(client, token)
        doc = yaml.safe_load(exported)
        tenant = doc["spec"]["tenants"][0]
        tenant["policies"]["flight_recorder"]["retention_days"] = 30
        tenant["targets"].append({"name": "nsx-01", "product": "nsx", "host": "10.1.0.3"})
        changed = yaml.safe_dump(doc, sort_keys=False)

        first = _import(client, token, changed)
        assert first.status_code == 200, first.text
        assert first.json()["applied"] is True

        again_preview = _import(client, token, changed, preview=True).json()
        again = _import(client, token, changed).json()
        history = client.get("/api/v1/settings/history", headers=_auth(token))

    assert again_preview["diff"] is not None
    for section in again_preview["diff"].values():
        assert section["added"] == [] and section["changed"] == []
    assert again["applied"] is False and again["revision_id"] is None

    revisions = await _rows(InstanceSettingsRevision)
    assert len(revisions) == 1
    (revision,) = revisions
    assert revision.actor_sub == "dana"
    assert revision.summary["targets"] == {"added": 1, "changed": 0}
    assert revision.summary["policies"] == {"added": 0, "changed": 1}
    audit = [row for row in await _rows(AuditLog) if row.id == revision.audit_id]
    assert len(audit) == 1 and audit[0].payload.get("op_class") == "write"

    assert history.status_code == 200, history.text
    (entry,) = history.json()
    assert entry["id"] == str(revision.id)
    assert entry["actor_sub"] == "dana"
    assert [a["key"] for a in entry["diff"]["targets"]["added"]] == ["nsx-01"]
    assert entry["diff"]["policies"]["changed"] == [
        {
            "tenant": str(_TENANT),
            "key": "flight_recorder.retention_days",
            "before": 14,
            "after": 30,
        }
    ]


@pytest.mark.asyncio
async def test_convention_history_rows_share_the_import_request(client: TestClient) -> None:
    await _seed()
    content = f"""
apiVersion: meho.evoila.com/v1
kind: Settings
spec:
  tenants:
    - id: {_TENANT}
      conventions:
        - slug: change-window
          body: Writes only between 22:00 and 05:00.
        - slug: naming
          title: Naming
          kind: reference
          body: Hosts are named site-role-NN.
"""
    key = make_rsa_keypair("kid-conv")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _import(client, _token(key), content)
    assert resp.status_code == 200, resp.text
    assert len(await _rows(InstanceSettingsRevision)) == 1
    # Migration-seeded default conventions carry history rows with no change_kind.
    history = [h for h in await _rows(TenantConventionHistory) if h.change_kind is not None]
    assert sorted(h.change_kind for h in history) == ["created", "updated"]
    assert len({h.request_id for h in history}) == 1
    assert history[0].request_id is not None
    assert {h.actor_sub for h in history} == {"admin-person"}


# ---------------------------------------------------------------------------
# Preview: mistakes and diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_mistakes_three_sentences_and_nothing_written(client: TestClient) -> None:
    await _seed()
    content = f"""
apiVersion: meho.evoila.com/v9
kind: Settings
spec:
  tenants:
    - id: {_TENANT}
      colour: blue
      targets:
        - name: no-host
          product: vsphere
"""
    key = make_rsa_keypair("kid-3")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key)
        preview = _import(client, token, content, preview=True)
        applied = _import(client, token, content)
    assert preview.status_code == 200
    body = preview.json()
    assert body["ok"] is False
    assert body["diff"] is None
    assert len(body["mistakes"]) == 3, body["mistakes"]
    joined = "\n".join(body["mistakes"])
    assert "`apiVersion`" in joined
    assert "`spec.tenants[0].colour` is not a known key." in joined
    assert "`spec.tenants[0].targets[0].host` is required." in joined

    assert applied.status_code == 422
    assert applied.json()["mistakes"] == body["mistakes"]
    assert {t.name for t in await _rows(Target)} == {"vc-01", "prom"}
    assert await _rows(InstanceSettingsRevision) == []
    settings_rows = [row for row in await _rows(AuditLog) if row.path == "/api/v1/settings/import"]
    assert len(settings_rows) == 2
    assert all(row.payload.get("op_class") == "read" for row in settings_rows)


@pytest.mark.asyncio
async def test_preview_diff_names_added_changed_and_not_in_file(client: TestClient) -> None:
    await _seed()
    content = f"""
apiVersion: meho.evoila.com/v1
kind: Settings
spec:
  tenants:
    - id: {_TENANT}
      policies:
        mail_recipient_allowlist: null
      targets:
        - name: vc-01
          product: vsphere
          host: 10.1.0.11
        - name: k8s
          product: kubernetes
          host: 10.1.0.20
"""
    key = make_rsa_keypair("kid-diff")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _import(client, _token(key), content, preview=True)
    assert resp.status_code == 200, resp.text
    diff = resp.json()["diff"]
    assert diff["policies"]["changed"] == [
        {
            "tenant": str(_TENANT),
            "key": "mail_recipient_allowlist",
            "before": "@example.com",
            "after": None,
        }
    ]
    assert [a["key"] for a in diff["targets"]["added"]] == ["k8s"]
    (changed,) = diff["targets"]["changed"]
    assert changed["key"] == "vc-01"
    assert changed["before"] == {"host": "10.1.0.1"}
    assert changed["after"] == {"host": "10.1.0.11"}
    assert diff["targets"]["not_in_file"] == [{"tenant": str(_TENANT), "key": "prom"}]
    assert diff["conventions"]["not_in_file"] == [{"tenant": str(_TENANT), "key": "change-window"}]
    # Preview writes nothing.
    assert {t.name for t in await _rows(Target)} == {"vc-01", "prom"}


def test_tenant_id_may_be_left_out_in_a_one_tenant_file(client: TestClient) -> None:
    content = (
        "apiVersion: meho.evoila.com/v1\nkind: Settings\n"
        "spec:\n  tenants:\n    - policies: {announce_gate_enabled: true}\n"
    )
    key = make_rsa_keypair("kid-noid")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _import(client, _token(key), content, preview=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["diff"]["policies"]["changed"][0]["key"] == "announce_gate_enabled"


# ---------------------------------------------------------------------------
# Secret-shaped values
# ---------------------------------------------------------------------------

_JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJhZG1pbiIsImV4cCI6MTk5OTk5OTk5OX0."
    "c2lnbmF0dXJlLXZhbHVlLWhlcmU"
)


@pytest.mark.parametrize(
    "notes",
    [
        _JWT,
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----",
        "<keep>",
        "********",
    ],
)
def test_secret_shaped_value_is_refused_without_echo(client: TestClient, notes: str) -> None:
    content = yaml.safe_dump(
        {
            "apiVersion": "meho.evoila.com/v1",
            "kind": "Settings",
            "spec": {
                "tenants": [
                    {
                        "id": str(_TENANT),
                        "targets": [
                            {"name": "x", "product": "ssh", "host": "10.0.0.5", "notes": notes}
                        ],
                    }
                ]
            },
        }
    )
    key = make_rsa_keypair("kid-secret")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _import(client, _token(key), content)
    assert resp.status_code == 422
    body = resp.json()
    assert any("spec.tenants[0].targets[0].notes" in m for m in body["mistakes"]), body
    raw = resp.text
    assert notes not in raw
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in raw
    assert "eyJzdWIiOiJhZG1pbiIsImV4cCI6MTk5OTk5OTk5OX0" not in raw


def test_ca_certificate_pin_is_not_secret_shaped() -> None:
    from meho_backplane.instance_settings.schema import find_secret_shaped_values

    assert find_secret_shaped_values({"tls_ca_pin": _CA_PEM}) == []


def _file_with_target(**target: Any) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "meho.evoila.com/v1",
            "kind": "Settings",
            "spec": {"tenants": [{"id": str(_TENANT), "targets": [target]}]},
        }
    )


@pytest.mark.parametrize("preview", [True, False])
def test_plain_password_in_extras_is_refused(client: TestClient, preview: bool) -> None:
    content = _file_with_target(
        name="x", product="ssh", host="10.0.0.5", extras={"password": "hunter2-plain"}
    )
    key = make_rsa_keypair("kid-extras-pw")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _import(client, _token(key), content, preview=preview)
    assert resp.status_code == (200 if preview else 422)
    body = resp.json()
    assert body["ok"] is False
    assert body["mistakes"] == [
        "`spec.tenants[0].targets[0].extras.password` is named like a credential; the "
        "settings file never carries secrets, so store it in OpenBao and refer to it "
        "with `secret_ref`."
    ]
    assert "hunter2-plain" not in resp.text


def test_jwt_in_secret_ref_stops_before_the_scope_check(client: TestClient) -> None:
    """A rejected secret reaches no later check, so no later message can repeat it."""
    content = _file_with_target(name="x", product="ssh", host="10.0.0.5", secret_ref=_JWT)
    key = make_rsa_keypair("kid-ref-jwt")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _import(client, _token(key), content, preview=True)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["mistakes"]) == 1, body
    assert body["mistakes"][0].startswith("`spec.tenants[0].targets[0].secret_ref` has a value")
    assert body["diff"] is None
    assert _JWT not in resp.text
    assert _JWT.split(".")[1] not in resp.text


def test_secret_shaped_key_name_is_redacted(client: TestClient) -> None:
    content = _file_with_target(name="x", product="ssh", host="10.0.0.5", extras={_JWT: "a"})
    key = make_rsa_keypair("kid-key-jwt")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _import(client, _token(key), content, preview=True)
    body = resp.json()
    assert body["mistakes"] == [
        "`spec.tenants[0].targets[0].extras.<redacted>` has a key name that looks like a "
        "JWT; the settings file never carries secrets, so store it in OpenBao and refer "
        "to it with `secret_ref`."
    ]
    assert _JWT.split(".")[1] not in resp.text


def test_invalid_date_is_a_mistake_not_a_server_error(client: TestClient) -> None:
    content = (
        "apiVersion: meho.evoila.com/v1\nkind: Settings\n"
        "metadata:\n  exportedAt: 2026-99-99\nspec:\n  tenants: []\n"
    )
    key = make_rsa_keypair("kid-bad-date")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        preview = _import(client, _token(key), content, preview=True)
        applied = _import(client, _token(key), content)
    assert preview.status_code == 200
    assert preview.json()["mistakes"] == [
        "Line 4: the value looks like a date but is not a real date."
    ]
    assert applied.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("notes", _JWT, "spec.tenants[0].targets[0].notes"),
        ("extras", {"api_key": "plain-text-key"}, "spec.tenants[0].targets[0].extras.api_key"),
    ],
)
async def test_export_refuses_stored_secrets(
    client: TestClient, field: str, value: Any, path: str
) -> None:
    await _insert_target(tenant_id=_TENANT, name="leaky", product="ssh", **{field: value})
    key = make_rsa_keypair("kid-export-leak")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get("/api/v1/settings/export", headers=_auth(_token(key)))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["kind"] == "settings_export_refused"
    assert len(detail["mistakes"]) == 1
    assert f"`{path}`" in detail["mistakes"][0]
    assert _JWT.split(".")[1] not in resp.text
    assert "plain-text-key" not in resp.text
