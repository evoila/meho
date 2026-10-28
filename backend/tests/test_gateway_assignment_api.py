# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the gateway checks API (Initiative #2415, #2499).

Exercises :mod:`meho_backplane.api.v1.checks` end to end against a minimal
app that mounts the real router behind the real JWT chain (respx-mocked
JWKS) so the runner route cage + ``require_runner`` + ``assert_runner_scope``
all run for real:

* ``PUT /api/v1/checks/assignment/{runner}`` — operator authoring +
  structured-422 validation (non-safe / unknown op, unknown target).
* ``GET /api/v1/checks/assignment`` — digest-versioned materialisation with
  resolved target descriptors, 304-on-unchanged, drift-changes-digest.
* ``POST /api/v1/checks/results`` — idempotent batch ingest, central-stamped
  ``received_at``.
* Runner scoping: a runner reaches only its own assignment/results;
  operator-only PUT; unauthenticated 401.

The wire shapes are :mod:`meho_backplane.runner.wire`'s — one schema on both
ends by construction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    RunnerAssignmentRow,
    RunnerCheckResult,
    RunnerPrincipal,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.gateway.assignment_service import descriptor_from_target
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.runner import wire

# Autouse fixtures (settings env, JWKS cache reset, connector-registry reset).
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401  (autouse)
    _isolated_jwks_cache,  # noqa: F401  (autouse)
    _operator_token,
    _settings_env,  # noqa: F401  (autouse)
)

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_RUNNER_A_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
_RUNNER_B_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000000b")

_PRODUCT = "checkprod"
_VERSION = "1.x"
_IMPL = "checkprod-api"
_OP_SAFE = "check.status"
_OP_CAUTION = "check.reboot"
_OP_UNKNOWN = "check.ghost"
_HANDLER_REF = "meho_backplane.connectors.checkprod.handlers.status"

_TARGET_NAME = "check-target"
_SECRET_REF = f"secret/tenants/{DEFAULT_TENANT_ID}/checkprod"
_TLS_CA_PIN_A = "-----BEGIN CERTIFICATE-----\npin-A\n-----END CERTIFICATE-----"
_TLS_CA_PIN_B = "-----BEGIN CERTIFICATE-----\npin-B-rotated\n-----END CERTIFICATE-----"
_TLS_SERVER_NAME = "check.corp.internal"


class _CheckConnector(Connector):
    """A tiny versioned connector so the target resolves to a real class.

    Class attrs match the registration triple (the canonical pattern real
    connectors follow); ``supported_version_range`` unset ⇒ any version.
    """

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL

    async def probe(self, target: Any) -> Any:  # pragma: no cover - never dispatched
        raise NotImplementedError

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def execute(  # pragma: no cover - never dispatched
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> Any:
        raise NotImplementedError


def _register_connector() -> None:
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL, cls=_CheckConnector)


def _build_app() -> FastAPI:
    from meho_backplane.api.v1.checks import router as checks_router

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(checks_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _runner_token(key: Any, *, runner_id: uuid.UUID, sub: str = "runner-sub") -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_id=DEFAULT_TENANT_ID,
        tenant_role="read_only",
        principal_kind="runner",
        runner_id=str(runner_id),
    )


async def _seed_identities() -> None:
    """Seed the tenant + two runner principals (runner-a -> A, runner-b -> B)."""
    async with get_sessionmaker()() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-checks", name="Checks Tenant"))
        for rid, rname in ((_RUNNER_A_ID, "runner-a"), (_RUNNER_B_ID, "runner-b")):
            if (
                await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.id == rid))
            ).scalar_one_or_none() is None:
                session.add(
                    RunnerPrincipal(
                        id=rid,
                        tenant_id=_TENANT,
                        name=rname,
                        keycloak_client_id=f"runner:{rname}",
                        keycloak_internal_id=f"kc-{rname}",
                        owner_sub="op-admin",
                        created_by_sub="op-admin",
                    )
                )
        await session.commit()


def _new_target(**overrides: Any) -> TargetORM:
    """Build a fully-specified (unpersisted) target row for the check connector."""
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": _TENANT,
        "name": _TARGET_NAME,
        "aliases": [],
        "product": _PRODUCT,
        "version": _VERSION,
        "host": "10.9.9.9",
        "port": 443,
        "fqdn": "check.corp",
        "secret_ref": _SECRET_REF,
        "auth_model": "shared_service_account",
        "vpn_required": False,
        "verify_tls": True,
        "tls_ca_pin": _TLS_CA_PIN_A,
        "tls_server_name": _TLS_SERVER_NAME,
        "extras": {},
        "notes": None,
        "fingerprint": None,
        "preferred_impl_id": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return TargetORM(**fields)


async def _seed_target(**overrides: Any) -> None:
    async with get_sessionmaker()() as session:
        session.add(_new_target(**overrides))
        await session.commit()


async def _seed_descriptor(
    *, op_id: str, safety_level: str = "safe", handler_ref: str | None = _HANDLER_REF
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                tenant_id=_TENANT,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL,
                op_id=op_id,
                source_kind="typed",
                handler_ref=handler_ref,
                safety_level=safety_level,
                is_enabled=True,
            )
        )
        await session.commit()


async def _assignment_row_count() -> int:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(RunnerAssignmentRow))).scalars().all()
        return len(rows)


def _put_body(*items: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(items)}


def _safe_item() -> dict[str, Any]:
    return {
        "check_ref": "chk-1",
        "target_name": _TARGET_NAME,
        "op": _OP_SAFE,
        "params": {"detail": "full"},
        "cadence_seconds": 60,
    }


# ---------------------------------------------------------------------------
# PUT — authoring validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_rejects_non_safe_op(client: TestClient) -> None:
    """Non-safe op → 422 assignment_op_not_safe; unknown op → assignment_op_unknown;
    unknown target → structured error; nothing is stored on rejection."""
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE, safety_level="safe")
    await _seed_descriptor(op_id=_OP_CAUTION, safety_level="caution")
    _register_connector()

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        headers = {"Authorization": f"Bearer {_operator_token(key)}"}

        # Non-safe op.
        non_safe = {**_safe_item(), "op": _OP_CAUTION}
        resp_unsafe = client.put(
            "/api/v1/checks/assignment/runner-a", json=_put_body(non_safe), headers=headers
        )
        # Unknown op (no descriptor).
        unknown_op = {**_safe_item(), "op": _OP_UNKNOWN}
        resp_unknown = client.put(
            "/api/v1/checks/assignment/runner-a", json=_put_body(unknown_op), headers=headers
        )
        # Unknown target.
        unknown_target = {**_safe_item(), "target_name": "no-such-target"}
        resp_target = client.put(
            "/api/v1/checks/assignment/runner-a", json=_put_body(unknown_target), headers=headers
        )

    assert resp_unsafe.status_code == 422
    assert resp_unsafe.json()["detail"][0]["type"] == "assignment_op_not_safe"
    assert resp_unknown.status_code == 422
    assert resp_unknown.json()["detail"][0]["type"] == "assignment_op_unknown"
    assert resp_target.status_code in (404, 422)
    assert resp_target.json()["detail"][0]["type"] == "assignment_target_unknown"

    # No row written on any rejection.
    assert await _assignment_row_count() == 0


@pytest.mark.asyncio
async def test_put_stores_safe_assignment(client: TestClient) -> None:
    """A valid PUT stores exactly one document row and echoes it back."""
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.put(
            "/api/v1/checks/assignment/runner-a",
            json=_put_body(_safe_item()),
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )

    assert resp.status_code == 200
    assert resp.json()["runner"] == "runner-a"
    assert resp.json()["items"][0]["check_ref"] == "chk-1"
    assert await _assignment_row_count() == 1


# ---------------------------------------------------------------------------
# GET — digest-versioned materialisation
# ---------------------------------------------------------------------------


async def _author_via_put(client: TestClient, key: Any) -> None:
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.put(
            "/api/v1/checks/assignment/runner-a",
            json=_put_body(_safe_item()),
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_assignment_versioned(client: TestClient) -> None:
    """GET returns a hex digest + materialised item; 304 on match; digest shifts on drift."""
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()

    key = make_rsa_keypair("kid-A")
    await _author_via_put(client, key)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        rheaders = {"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"}
        first = client.get("/api/v1/checks/assignment?runner=runner-a", headers=rheaders)
        digest = first.json()["assignment_version"]
        second = client.get(
            f"/api/v1/checks/assignment?runner=runner-a&known_version={digest}", headers=rheaders
        )

    assert first.status_code == 200
    # 64-char lowercase hex digest.
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)

    item = first.json()["items"][0]
    assert item["check_ref"] == "chk-1"
    assert item["handler_ref"] == _HANDLER_REF
    assert item["safety_level"] == "safe"
    assert item["op_id"] == _OP_SAFE
    assert item["product"] == _PRODUCT
    # Principal context of the requesting runner.
    assert item["principal"]["principal_kind"] == "runner"
    assert item["principal"]["tenant_role"] == "read_only"
    # Resolved target descriptor carries the full connection-routing set.
    # (wire field name is ``target_descriptor``; the issue's "target".)
    td = item["target_descriptor"]
    for field in (
        "host",
        "port",
        "product",
        "fingerprint",
        "preferred_impl_id",
        "secret_ref",
        "verify_tls",
        "tls_ca_pin",
        "tls_server_name",
    ):
        assert field in td, field
    assert td["host"] == "10.9.9.9"
    assert td["port"] == 443
    assert td["secret_ref"] == _SECRET_REF
    assert td["tls_ca_pin"] == _TLS_CA_PIN_A

    # Unchanged → 304 with empty body.
    assert second.status_code == 304
    assert second.content == b""

    # Drift: rotate the target's tls_ca_pin; same known_version now 200 + new digest.
    async with get_sessionmaker()() as session:
        await session.execute(
            update(TargetORM)
            .where(TargetORM.tenant_id == _TENANT, TargetORM.name == _TARGET_NAME)
            .values(tls_ca_pin=_TLS_CA_PIN_B)
        )
        await session.commit()

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        rheaders = {"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"}
        after = client.get(
            f"/api/v1/checks/assignment?runner=runner-a&known_version={digest}", headers=rheaders
        )

    assert after.status_code == 200
    assert after.json()["assignment_version"] != digest
    assert after.json()["items"][0]["target_descriptor"]["tls_ca_pin"] == _TLS_CA_PIN_B


@pytest.mark.asyncio
async def test_wire_models_shared(client: TestClient) -> None:
    """The GET body parses as ``runner/wire.py``'s RunnerAssignment; one descriptor class."""
    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()

    key = make_rsa_keypair("kid-A")
    await _author_via_put(client, key)
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.get(
            "/api/v1/checks/assignment?runner=runner-a",
            headers={"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"},
        )

    assert resp.status_code == 200
    # One schema on both sides — the response validates as the wire model.
    parsed = wire.RunnerAssignment.model_validate(resp.json())
    assert parsed.items[0].check_ref == "chk-1"
    assert parsed.items[0].target_descriptor is not None

    # ``ResolvedTargetDescriptor`` is defined in exactly one file (no fork).
    src_root = Path(__file__).resolve().parent.parent / "src" / "meho_backplane"
    hits = [
        p
        for p in src_root.rglob("*.py")
        if "class ResolvedTargetDescriptor" in p.read_text(encoding="utf-8")
    ]
    assert len(hits) == 1
    assert hits[0].name == "wire.py"


@pytest.mark.asyncio
async def test_descriptor_carries_no_secret_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GET payload ships ``secret_ref`` verbatim and never a credential value."""
    calls: list[Any] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("credential backend must not be dialed during assignment GET")

    monkeypatch.setattr(
        "meho_backplane.connectors._shared.vault_creds.load_basic_credentials", _spy
    )

    await _seed_identities()
    await _seed_target()
    await _seed_descriptor(op_id=_OP_SAFE)
    _register_connector()

    key = make_rsa_keypair("kid-A")
    await _author_via_put(client, key)
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.get(
            "/api/v1/checks/assignment?runner=runner-a",
            headers={"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"},
        )

    assert resp.status_code == 200
    assert not calls  # no Vault/GSM call during GET
    td = resp.json()["items"][0]["target_descriptor"]
    assert td["secret_ref"] == _SECRET_REF
    for forbidden in ("password", "token", "secret_data"):
        assert forbidden not in td
    # And no credential value leaks anywhere in the serialised payload.
    assert "secret_data" not in json.dumps(resp.json())


def test_descriptor_duck_types_resolver() -> None:
    """``resolve_connector`` picks the same class off the descriptor and the row."""
    _register_connector()
    target = _new_target()
    descriptor = descriptor_from_target(target)

    # No DB session is opened by ``resolve_connector`` — it reads attrs +
    # the in-process registry only.
    assert resolve_connector(descriptor) is resolve_connector(target)
    assert resolve_connector(descriptor) is _CheckConnector


# ---------------------------------------------------------------------------
# POST — idempotent result ingest
# ---------------------------------------------------------------------------


def _result_batch(runner_id: str, *uids: str) -> dict[str, Any]:
    return {
        "runner_id": runner_id,
        "results": [
            {
                "result_uid": uid,
                "check_ref": "chk-1",
                "op_id": _OP_SAFE,
                "status": "ok",
                "result": {"reachable": True},
                "error": None,
            }
            for uid in uids
        ],
    }


@pytest.mark.asyncio
async def test_results_batch_idempotent(client: TestClient) -> None:
    """POST N results → accepted N; re-POST identical → duplicates N; received_at server-set."""
    await _seed_identities()
    key = make_rsa_keypair("kid-A")
    # A naive floor (aiosqlite reads ``DateTime(timezone=True)`` back as
    # naive); a server-stamped ``received_at`` is well after this.
    sentinel = datetime(2000, 1, 1)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        rheaders = {"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"}
        batch = _result_batch("runner-a", "uid-1", "uid-2", "uid-3")
        first = client.post("/api/v1/checks/results", json=batch, headers=rheaders)
        second = client.post("/api/v1/checks/results", json=batch, headers=rheaders)

    assert first.status_code == 200
    assert first.json() == {"accepted": 3, "duplicates": 0}
    assert second.status_code == 200
    assert second.json() == {"accepted": 0, "duplicates": 3}

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(RunnerCheckResult))).scalars().all()
    assert len(rows) == 3
    # received_at is central-stamped, not the client sentinel (the wire model
    # has no received_at field, so a client cannot inject it).
    for row in rows:
        stored = row.received_at
        stored_naive = stored.replace(tzinfo=None) if stored.tzinfo is not None else stored
        assert stored_naive > sentinel


# ---------------------------------------------------------------------------
# Runner scoping + auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_scoping(client: TestClient) -> None:
    """A runner reaches only its own assignment/results; PUT is operator-only; 401 unauth."""
    await _seed_identities()
    key = make_rsa_keypair("kid-A")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        a_headers = {"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"}

        # Runner A fetching runner B's assignment → 403.
        cross_get = client.get("/api/v1/checks/assignment?runner=runner-b", headers=a_headers)
        # Runner A posting results for runner B → 403.
        cross_post = client.post(
            "/api/v1/checks/results", json=_result_batch("runner-b", "x-1"), headers=a_headers
        )
        # Runner token on operator-only PUT → 403.
        runner_put = client.put(
            "/api/v1/checks/assignment/runner-a", json=_put_body(), headers=a_headers
        )
        # Unauthenticated GET / POST → 401.
        unauth_get = client.get("/api/v1/checks/assignment?runner=runner-a")
        unauth_post = client.post("/api/v1/checks/results", json=_result_batch("runner-a", "y-1"))

    assert cross_get.status_code == 403
    assert cross_post.status_code == 403
    assert runner_put.status_code == 403
    assert unauth_get.status_code == 401
    assert unauth_post.status_code == 401


# ---------------------------------------------------------------------------
# Wire version check (#5020)
# ---------------------------------------------------------------------------


async def _runner_a_row() -> RunnerPrincipal:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.id == _RUNNER_A_ID))
        ).scalar_one()


@pytest.mark.asyncio
async def test_same_or_missing_wire_version_is_served_and_stored(client: TestClient) -> None:
    """A runner on the central version, and one that sends no version, work as before."""
    await _seed_identities()
    key = make_rsa_keypair("kid-A")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        auth = {"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"}
        same = client.get(
            "/api/v1/checks/assignment?runner=runner-a",
            headers={**auth, wire.WIRE_VERSION_HEADER: wire.SCHEMA_VERSION},
        )
        missing = client.get("/api/v1/checks/assignment?runner=runner-a", headers=auth)
        posted = client.post(
            "/api/v1/checks/results", json=_result_batch("runner-a", "w-1"), headers=auth
        )

    assert same.status_code == 200
    assert missing.status_code == 200
    assert posted.status_code == 200
    # A missing header counts as version 1, the version every older runner spoke.
    assert (await _runner_a_row()).wire_version == wire.WIRE_VERSION_WHEN_MISSING == "1"


@pytest.mark.asyncio
async def test_other_wire_version_is_refused_with_both_versions(client: TestClient) -> None:
    """A runner on another wire version gets a 409 naming both versions; nothing is served."""
    await _seed_identities()
    key = make_rsa_keypair("kid-A")
    before = (await _runner_a_row()).last_seen_at

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        headers = {
            "Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}",
            wire.WIRE_VERSION_HEADER: "2",
        }
        got = client.get("/api/v1/checks/assignment?runner=runner-a", headers=headers)
        posted = client.post(
            "/api/v1/checks/results", json=_result_batch("runner-a", "w-2"), headers=headers
        )

    for resp in (got, posted):
        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            "runner_wire_version_mismatch: runner wire version 2, "
            "central MEHO needs 1: update the central MEHO"
        )
    row = await _runner_a_row()
    # The runner list shows the version the runner sent ...
    assert row.wire_version == "2"
    # ... but a runner that cannot do its work does not count as alive.
    assert row.last_seen_at == before
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(RunnerCheckResult))).scalars().all() == []


@pytest.mark.asyncio
async def test_other_wire_version_is_refused_before_the_result_body_is_read(
    client: TestClient,
) -> None:
    """A result body the central models cannot read still gets the 409, not a bare 422.

    This is the case the check exists for: the other wire version changed a
    required result field. The runner must read both versions, and the
    version it sent must be stored.
    """
    await _seed_identities()
    key = make_rsa_keypair("kid-A")
    before = (await _runner_a_row()).last_seen_at
    unreadable = {"runner_id": "runner-a", "results": [{"result_uid": "w-3"}]}

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        auth = {"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"}
        refused = client.post(
            "/api/v1/checks/results",
            json=unreadable,
            headers={**auth, wire.WIRE_VERSION_HEADER: "2"},
        )
        # The same body on the central version is the usual body validation error.
        same_version = client.post("/api/v1/checks/results", json=unreadable, headers=auth)

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "runner_wire_version_mismatch: runner wire version 2, "
        "central MEHO needs 1: update the central MEHO"
    )
    assert same_version.status_code == 422
    row = await _runner_a_row()
    assert row.wire_version == "2"
    assert row.last_seen_at == before


@pytest.mark.asyncio
async def test_central_route_compares_with_schema_version(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a ``SCHEMA_VERSION`` bump, an old runner (no header) is told to update."""
    await _seed_identities()
    key = make_rsa_keypair("kid-A")
    monkeypatch.setattr(wire, "SCHEMA_VERSION", "2")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.get(
            "/api/v1/checks/assignment?runner=runner-a",
            headers={"Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "runner_wire_version_mismatch: runner wire version 1, "
        "central MEHO needs 2: update the runner"
    )


@pytest.mark.asyncio
async def test_malformed_wire_version_is_rejected_and_not_stored(client: TestClient) -> None:
    """A wire version that is not a short number is a 400 and never reaches the row."""
    await _seed_identities()
    key = make_rsa_keypair("kid-A")

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.get(
            "/api/v1/checks/assignment?runner=runner-a",
            headers={
                "Authorization": f"Bearer {_runner_token(key, runner_id=_RUNNER_A_ID)}",
                wire.WIRE_VERSION_HEADER: "1; drop",
            },
        )

    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("runner_wire_version_invalid")
    assert (await _runner_a_row()).wire_version is None


@pytest.mark.asyncio
async def test_runner_client_gets_clear_refusal_from_central(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real runner client, on another wire version, gets central's refusal end to end."""
    from meho_backplane.runner.client import RunnerClient, RunnerWireVersionMismatchError

    await _seed_identities()
    key = make_rsa_keypair("kid-A")
    # The runner is built with version 2; central still needs 1.
    monkeypatch.setattr(wire, "SCHEMA_VERSION", "2")
    runner_client = RunnerClient(
        central_url="http://central.test",
        runner_id="runner-a",
        token=_runner_token(key, runner_id=_RUNNER_A_ID),
    )
    monkeypatch.setattr(wire, "SCHEMA_VERSION", "1")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        mock_router.route(host="central.test").mock(
            side_effect=lambda request: _forward(client, request)
        )
        async with runner_client:
            with pytest.raises(RunnerWireVersionMismatchError) as exc_info:
                await runner_client.fetch_assignment(known_version=None)

    assert str(exc_info.value) == (
        "runner_wire_version_mismatch: runner wire version 2, "
        "central MEHO needs 1: update the central MEHO"
    )


def _forward(client: TestClient, request: Any) -> Any:
    """Replay an outgoing runner request against the in-process central app."""
    import httpx

    resp = client.request(
        request.method,
        request.url.raw_path.decode(),
        headers=dict(request.headers),
        content=request.content,
    )
    return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content)
