# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Platform-wide contract test for the unified list-envelope shape.

#2338 (breaking pass, Parent initiative #2364). Every reference
GET-list endpoint returns the ``{items, next_cursor?, ...sidecars}``
envelope codified in ``docs/codebase/api-shape-conventions.md`` §2 --
unconditionally, as the default and only shape. The ``?envelope=v2``
opt-in that bridged the migration (G0.16-T6 #1312 / G0.18-T3 #1356 /
G0.22-T6 #1611) was retired in this pass.

This module is the single CI-enforced home of the convention. It:

* enumerates the governed §2 list endpoints in one registry
  (:data:`_LIST_ENVELOPE_ENDPOINTS`);
* drives each against the production ``meho_backplane.main:app`` and
  asserts the *runtime* body is the unified envelope (``items`` is a
  list, ``next_cursor`` is present, no legacy list key leaks, and the
  body is never a bare JSON array);
* introspects the generated **OpenAPI schema** and asserts each
  governed endpoint's ``GET`` 200 response resolves to an object schema
  carrying ``items`` (array) + ``next_cursor`` -- the schema the CLI /
  Go client is generated from, so a drift here is a client-contract
  break, not just a runtime one;
* pins the registry against the live route table so a governed
  endpoint that is renamed or dropped fails here.

Forward guard: a new GET-list endpoint that returns a collection joins
the convention by (a) returning the ``{items, next_cursor?}`` envelope
and (b) being added to :data:`_LIST_ENVELOPE_ENDPOINTS`. Anything else
-- a bare array, a resource-named list key (``{"widgets": [...]}``), a
renamed list field -- is exactly the divergence §2 exists to forbid,
and this test is where it is caught.

Out of scope (documented, not §2 reference list endpoints): sub-resource
reads that predate the convention (``conventions/{slug}/history``,
``topology/edges``, ``approvals``, ``agent/runs``, ``doc-collections``)
and the topology closure reads (``dependents`` / ``dependencies``),
which converge on the §4 ``{kind, nodes}`` discriminated shape rather
than §2. Those are tracked for a future convergence pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_TENANT = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable auth + point at the mock IdP (mirrors the per-router suites)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    with TestClient(app) as test_client:
        yield test_client


def _token(key: Any) -> str:
    """Mint a tenant_admin JWT (the most permissive built-in tier).

    ``tenant_admin`` satisfies every list endpoint's RBAC gate
    (``broadcast/overrides`` requires it; the rest require only
    ``operator``, which ``tenant_admin`` subsumes).
    """
    return mint_token(
        key,
        sub="ops@example.com",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT),
    )


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        existing = await session.execute(select(Tenant).where(Tenant.id == _TENANT))
        if existing.scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-env", name="Tenant Env"))
            await session.commit()


#: (path, legacy_key). ``legacy_key`` is the resource-named list key the
#: v0.8.0 pre-convergence shape wrapped its list under (or ``None`` when
#: that shape was a bare JSON array); the contract asserts it does NOT
#: appear on the converged envelope. This registry is the source of
#: truth for "§2 list endpoints" -- a new GET-list surface joins the
#: convention by returning ``{items, next_cursor?}`` and being added
#: here.
_LIST_ENVELOPE_ENDPOINTS = [
    pytest.param("/api/v1/targets", None, id="targets"),
    pytest.param("/api/v1/connectors", "connectors", id="connectors"),
    pytest.param("/api/v1/conventions", "entries", id="conventions"),
    pytest.param("/api/v1/audit/my-recent", "rows", id="audit-my-recent"),
    pytest.param("/api/v1/broadcast/overrides", None, id="broadcast-overrides"),
    pytest.param("/api/v1/runbooks/templates", "templates", id="runbook-templates"),
    pytest.param("/api/v1/runbooks/runs", "runs", id="runbook-runs"),
]

#: Bare path list for the schema / route-table checks (the parametrize
#: id + legacy key aren't needed there).
_LIST_ENVELOPE_PATHS = [p.values[0] for p in _LIST_ENVELOPE_ENDPOINTS]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "legacy_key"), _LIST_ENVELOPE_ENDPOINTS)
async def test_runtime_body_is_unified_envelope(
    client: TestClient,
    path: str,
    legacy_key: str | None,
) -> None:
    """Every governed endpoint returns ``{items, next_cursor}`` by default."""
    await _seed_tenant()
    key = make_rsa_keypair("kid-A")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(path, headers={"Authorization": f"Bearer {_token(key)}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Never a bare array -- the anti-pattern §2 forbids (forecloses
    # pagination / sidecars without a further breaking change).
    assert isinstance(body, dict), f"{path} returned a non-object body"
    # The §2 contract: items always present as a list, next_cursor
    # always present (so a client reads it without a KeyError guard).
    assert isinstance(body["items"], list), f"{path} items is not a list"
    assert "next_cursor" in body, f"{path} missing next_cursor"
    # No resource-named legacy list key leaks onto the converged shape.
    if legacy_key is not None:
        assert legacy_key not in body, f"{path} still carries legacy key {legacy_key!r}"


def test_openapi_schema_declares_items_and_next_cursor() -> None:
    """Each governed endpoint's OpenAPI GET-200 schema is the envelope.

    This is the client-facing half of the contract: the generated Go /
    CLI client is produced from this schema, so a governed endpoint
    whose 200 schema is a bare array (``type: array``) or lacks
    ``items`` / ``next_cursor`` would ship a broken typed client even if
    the runtime body happened to look right.
    """
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})

    def _resolve(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        if ref is None:
            return node
        name = ref.rsplit("/", 1)[-1]
        return components[name]

    for path in _LIST_ENVELOPE_PATHS:
        op = schema["paths"][path]["get"]
        content = op["responses"]["200"]["content"]["application/json"]
        resolved = _resolve(content["schema"])
        assert resolved.get("type") == "object", (
            f"{path} 200 schema is not an object (bare-array or union?): {resolved!r}"
        )
        props = resolved.get("properties", {})
        assert "items" in props, f"{path} 200 schema missing items property"
        assert props["items"].get("type") == "array", f"{path} 200 schema items is not an array"
        assert "next_cursor" in props, f"{path} 200 schema missing next_cursor property"


def test_registry_paths_are_registered_get_routes() -> None:
    """Guard against a governed endpoint being renamed / dropped silently.

    Uses the OpenAPI ``paths`` map (the surface the client is generated
    from) rather than ``app.routes`` — the versioned routers are mounted
    under a nested sub-application, so they don't appear on the
    top-level ``app.routes`` list.
    """
    paths = app.openapi()["paths"]
    for path in _LIST_ENVELOPE_PATHS:
        assert path in paths, f"governed list endpoint {path} is not a registered route"
        assert "get" in paths[path], f"governed list endpoint {path} lost its GET method"
