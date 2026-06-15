# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for G0.3-T4: audit_log.target_id via contextvar.

Coverage matrix:

* :func:`resolve_target` binds ``target_id`` into structlog contextvars on
  exact-name success, alias-match success, and does **not** bind on 404.
* :func:`_resolve_target_id` in :mod:`meho_backplane.audit` correctly parses
  the contextvar (happy path, None slot, malformed value).
* ``GET /api/v1/targets/{name}`` (describe) writes ``audit_log.target_id``
  equal to the resolved target's UUID.
* ``POST /api/v1/targets`` (create) writes ``audit_log.target_id`` equal to
  the newly created target's UUID.
* ``GET /api/v1/targets`` (list) writes ``audit_log.target_id = NULL``
  (no resolve_target call, slot stays ``None``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import respx
import structlog
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from meho_backplane.audit import _resolve_target_id
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.targets.resolver import (
    TargetNotFoundError,
    resolve_target,
)

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_DEFAULT_TENANT_UUID = uuid.UUID(DEFAULT_TENANT_ID)


# ---------------------------------------------------------------------------
# Per-test isolated audit DB
# ---------------------------------------------------------------------------


@pytest.fixture
def _audit_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db_path = tmp_path / "audit_t4.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def isolated_engine(
    _audit_db_url: str,
) -> AsyncIterator[AsyncEngine]:
    reset_engine_for_testing()
    eng = create_engine_for_url(_audit_db_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng
    try:
        yield eng
    finally:
        await dispose_engine()
        reset_engine_for_testing()


async def _fetch_audit_rows(eng: AsyncEngine) -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Unit tests — resolve_target contextvar binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_exact_match_binds_target_id(
    isolated_engine: AsyncEngine,
) -> None:
    """Exact-name match binds ``target_id`` into structlog contextvars."""
    t = await _insert_target(name="alpha")

    structlog.contextvars.clear_contextvars()
    sm = get_sessionmaker()
    async with sm() as session:
        returned = await resolve_target(session, _DEFAULT_TENANT_UUID, "alpha")

    assert returned.id == t.id
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("target_id") == str(t.id)


@pytest.mark.asyncio
async def test_resolve_target_alias_match_binds_target_id(
    isolated_engine: AsyncEngine,
) -> None:
    """Alias-element-equality match also binds ``target_id``."""
    t = await _insert_target(name="beta", aliases=["b", "beta-alias"])

    structlog.contextvars.clear_contextvars()
    sm = get_sessionmaker()
    async with sm() as session:
        returned = await resolve_target(session, _DEFAULT_TENANT_UUID, "beta-alias")

    assert returned.id == t.id
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("target_id") == str(t.id)


@pytest.mark.asyncio
async def test_resolve_target_not_found_does_not_bind_target_id(
    isolated_engine: AsyncEngine,
) -> None:
    """TargetNotFoundError is raised without mutating ``target_id`` contextvar."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id=None)

    sm = get_sessionmaker()
    async with sm() as session:
        with pytest.raises(TargetNotFoundError):
            await resolve_target(session, _DEFAULT_TENANT_UUID, "no-such-target")

    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("target_id") is None


# ---------------------------------------------------------------------------
# Unit tests — _resolve_target_id helper
# ---------------------------------------------------------------------------


def test_resolve_target_id_returns_none_when_slot_is_none() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id=None)
    assert _resolve_target_id() is None


def test_resolve_target_id_returns_none_when_key_absent() -> None:
    structlog.contextvars.clear_contextvars()
    assert _resolve_target_id() is None


def test_resolve_target_id_parses_valid_uuid_string() -> None:
    tid = uuid.uuid4()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id=str(tid))
    assert _resolve_target_id() == tid


def test_resolve_target_id_returns_none_for_malformed_string() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id="not-a-uuid")
    assert _resolve_target_id() is None


# ---------------------------------------------------------------------------
# Integration tests — audit_log.target_id populated via middleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_target_writes_audit_row_with_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/targets/{name} → audit_log.target_id == resolved target UUID."""
    t = await _insert_target(name="gamma")
    key = make_rsa_keypair("kid-T4-describe")
    token = mint_token(
        key,
        sub="op-t4",
        tenant_role="operator",
    )
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.get(
            f"/api/v1/targets/{t.name}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id == t.id


@pytest.mark.asyncio
async def test_list_targets_writes_audit_row_with_null_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/targets → audit_log.target_id is NULL (no resolve_target)."""
    await _insert_target(name="delta")
    key = make_rsa_keypair("kid-T4-list")
    token = mint_token(key, sub="op-t4-list", tenant_role="operator")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id is None


@pytest.mark.asyncio
async def test_create_target_writes_audit_row_with_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/v1/targets → audit_log.target_id == newly created target UUID."""
    key = make_rsa_keypair("kid-T4-create")
    token = mint_token(key, sub="op-t4-create", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "epsilon",
                "product": "rke2",
                "host": "10.0.0.5",
                "auth_model": "shared_service_account",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    created_id = uuid.UUID(response.json()["id"])

    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id == created_id


@pytest.mark.asyncio
async def test_describe_nonexistent_target_audit_row_has_null_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/targets/no-such → 404, audit_log.target_id is NULL."""
    key = make_rsa_keypair("kid-T4-404")
    token = mint_token(key, sub="op-t4-404", tenant_role="operator")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.get(
            "/api/v1/targets/no-such-target",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id is None


# ---------------------------------------------------------------------------
# T1 (#1780) — verify_tls change folds into the audit_log payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_verify_tls_false_writes_tls_audit_payload(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH ``{"verify_tls": false}`` → audit_log.payload records the change.

    T1 (#1780). Disabling TLS verification is a security-relevant
    config change that must leave a durable, queryable trail. The route
    binds ``audit_*`` contextvars that
    :func:`~meho_backplane.audit._resolve_audit_payload` folds into the
    request's audit row, so the payload carries
    ``tls_verification_disabled`` + ``target_id`` + before/after. The
    soft-FK ``audit_log.target_id`` **column** is also populated (the
    resolver bind), distinct from the ``target_id`` payload key.
    """
    t = await _insert_target(name="zeta", verify_tls=True)
    key = make_rsa_keypair("kid-T1-patch-tls")
    token = mint_token(key, sub="adm-t1-tls", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"verify_tls": False},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["verify_tls"] is False

    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["tls_verification_disabled"] is True
    assert payload["target_id"] == str(t.id)
    assert payload["verify_tls_before"] is True
    assert payload["verify_tls_after"] is False
    # The soft-FK column is populated too (resolve_target bind).
    assert rows[0].target_id == t.id


@pytest.mark.asyncio
async def test_patch_without_verify_tls_binds_no_tls_audit_keys(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATCH that does not touch ``verify_tls`` binds **no** TLS audit keys.

    T1 (#1780). The audit fold-in is gated on the field actually being
    sent (``exclude_unset``), so an unrelated PATCH (here: ``notes``)
    leaves the TLS keys out of the payload entirely — no audit noise on
    the common path.
    """
    t = await _insert_target(name="eta", verify_tls=False)
    key = make_rsa_keypair("kid-T1-patch-notls")
    token = mint_token(key, sub="adm-t1-notls", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"notes": "ticket-42"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert "tls_verification_disabled" not in payload
    assert "verify_tls_before" not in payload
    assert "verify_tls_after" not in payload


@pytest.mark.asyncio
async def test_create_target_verify_tls_false_writes_tls_audit_payload(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST with ``verify_tls=false`` → audit_log.payload records the opt-out.

    T1 (#1780). A target *created* with TLS verification off is audited
    the same way a PATCH that disables it is, with ``before=True`` (the
    secure default the column would otherwise carry).
    """
    key = make_rsa_keypair("kid-T1-create-tls")
    token = mint_token(key, sub="adm-t1-create-tls", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "theta",
                "product": "rke2",
                "host": "10.0.0.7",
                "verify_tls": False,
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    created_id = uuid.UUID(response.json()["id"])

    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["tls_verification_disabled"] is True
    assert payload["target_id"] == str(created_id)
    assert payload["verify_tls_before"] is True
    assert payload["verify_tls_after"] is False


# ---------------------------------------------------------------------------
# T5 (#1784) — tls_ca_pin set/change/clear folds into the audit_log payload
# ---------------------------------------------------------------------------


def _audit_ca_pem() -> str:
    """A valid self-signed CA PEM for the CA-pin audit tests."""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "audit-test-ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


@pytest.mark.asyncio
async def test_create_target_with_ca_pin_writes_ca_pin_audit_payload(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST with ``tls_ca_pin`` → audit payload records the pin (digest only)."""
    from meho_backplane.api.v1.targets import _ca_pin_digest

    pem = _audit_ca_pem()
    key = make_rsa_keypair("kid-T5-create")
    token = mint_token(key, sub="adm-t5-create", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={"name": "kappa", "product": "vmware-rest", "host": "vrli.lab", "tls_ca_pin": pem},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    created_id = uuid.UUID(response.json()["id"])
    assert response.json()["tls_ca_pin"] == pem

    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["tls_ca_pinned"] is True
    assert payload["target_id"] == str(created_id)
    # No prior pin → empty-string "before" marker (not None, which the
    # audit fold-in would drop).
    assert payload["tls_ca_pin_before"] == ""
    assert payload["tls_ca_pin_after"] == _ca_pin_digest(pem)
    # The PEM body is never put in the audit payload — only a digest.
    assert "BEGIN CERTIFICATE" not in str(payload)


@pytest.mark.asyncio
async def test_patch_set_ca_pin_writes_ca_pin_audit_payload(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH that adds a pin → audit payload records before=None, after=digest."""
    from meho_backplane.api.v1.targets import _ca_pin_digest

    pem = _audit_ca_pem()
    t = await _insert_target(name="lambda", tls_ca_pin=None)
    key = make_rsa_keypair("kid-T5-patch-set")
    token = mint_token(key, sub="adm-t5-set", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"tls_ca_pin": pem},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["tls_ca_pinned"] is True
    assert payload["tls_ca_pin_before"] == ""
    assert payload["tls_ca_pin_after"] == _ca_pin_digest(pem)


@pytest.mark.asyncio
async def test_patch_clear_ca_pin_writes_ca_pin_audit_payload(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH ``{"tls_ca_pin": null}`` → audit records before=digest, after=None."""
    from meho_backplane.api.v1.targets import _ca_pin_digest

    pem = _audit_ca_pem()
    t = await _insert_target(name="mu", tls_ca_pin=pem)
    key = make_rsa_keypair("kid-T5-patch-clear")
    token = mint_token(key, sub="adm-t5-clear", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"tls_ca_pin": None},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["tls_ca_pin"] is None
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["tls_ca_pinned"] is False
    assert payload["tls_ca_pin_before"] == _ca_pin_digest(pem)
    # Pin cleared → empty-string "after" marker.
    assert payload["tls_ca_pin_after"] == ""


@pytest.mark.asyncio
async def test_patch_without_ca_pin_binds_no_ca_pin_audit_keys(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATCH that does not touch ``tls_ca_pin`` binds no CA-pin audit keys."""
    pem = _audit_ca_pem()
    t = await _insert_target(name="nu", tls_ca_pin=pem)
    key = make_rsa_keypair("kid-T5-patch-noop")
    token = mint_token(key, sub="adm-t5-noop", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"notes": "ticket-7"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert "tls_ca_pinned" not in payload
    assert "tls_ca_pin_before" not in payload
    assert "tls_ca_pin_after" not in payload


@pytest.mark.asyncio
async def test_patch_pin_on_insecure_target_rejected_422(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH adding a pin to a verify_tls=false row is rejected (merged 422).

    The schema validator only sees the request body, so the cross-row
    contradiction (set a pin on a row already at verify_tls=false) is
    enforced by the route handler.
    """
    pem = _audit_ca_pem()
    t = await _insert_target(name="xi", verify_tls=False, tls_ca_pin=None)
    key = make_rsa_keypair("kid-T5-merged")
    token = mint_token(key, sub="adm-t5-merged", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"tls_ca_pin": pem},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    assert "mutually exclusive" in str(response.json())
