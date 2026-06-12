# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Automation E2E recorded-fixture integration test (G3.6-T12 #840).

Covers every acceptance criterion from Issue #840 inside a single
SQLite-backed test module (no Docker dependency; runs in the
``meho-runners`` CI lane alongside the other
``backend/tests/test_connectors_*.py`` E2E suites).

Acceptance contract:

(a) **Both planes dispatch** -- all 11 curated VCFA core ops
    (6 provider + 5 tenant) dispatch through ``call_operation``
    against a respx-mocked VCFA appliance and return ``status='ok'``.
    Each plane carries its bespoke auth flow (provider Basic ->
    ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` JWT; tenant JSON body ->
    ``{"token": ...}``); the connector picks the right token by path
    prefix.

(b) **Vhost (``--fqdn``) routing.** A vhost-routed target (``fqdn``
    set, IP host) reaches the appliance via the FQDN-rooted base URL;
    the recorded fixture covers the happy path. A target reached by
    IP with **no** ``fqdn`` set surfaces a structured
    ``connector_error`` whose message names ``fqdn`` -- proving the
    descriptive-error contract, not a blank 404.

(c) **Audit rows.** Each dispatch inserts an ``AuditLog`` row carrying
    ``method='DISPATCH'``, a non-null ``target_id``, and a non-empty
    ``payload['params_hash']``.

(d) **JSONFlux handle path.** Dispatching
    ``GET:/iaas/api/deployments`` with a force-handle reducer returns
    a populated ``OperationResult.handle`` with at least one
    ``sample_rows`` entry.

(e) **Per-call ``fqdn`` override.** The dispatch body's ``target.fqdn``
    field overrides the resolved Target's ``fqdn`` in memory; the DB
    row is not modified. Exercises the CLI ``--fqdn`` flag's
    end-to-end path (issue #840: "--fqdn threads through to the
    target resolution").

Recorded-fixture format
-----------------------

The respx routes are hand-built dicts in this module rather than read
from disk JSON. The G3.6-T13 (#841) refresh tool's recipe registry
explicitly excludes VCFA -- its dual-plane auth doesn't fit the
``session-token-header`` pattern. The inlined fixtures keep the
plane / payload shapes pinned next to the per-op assertions for
readability; future operators record a live fixture with the tool's
HTTP-level recorder and inline the payloads here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.vcf_automation import (
    VCFA_CONNECTOR_ID,
    VCFA_CORE_GROUPS,
    VCFA_CORE_OPS,
    VCFA_IMPL_ID,
    VCFA_PRODUCT,
    VCFA_VERSION,
    VcfAutomationConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

VCFA_E2E_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000fa")

# ``.test.invalid`` (RFC 6761 reserved) so no real network egress fires.
# The IP-host scenario uses a different host so both routers can coexist
# in their respective tests without contention.
VCFA_E2E_FQDN: str = "vcfa-e2e.test.invalid"
VCFA_E2E_FQDN_BASE_URL: str = f"https://{VCFA_E2E_FQDN}"
VCFA_E2E_TARGET_NAME: str = "vcfa-e2e-target"

# JWT + token values the respx routes return on the per-plane login
# endpoints. Distinct values per plane so a misrouted call surfaces
# loud rather than silently re-using the same Bearer header.
_PROVIDER_JWT = "vcfa-e2e-provider-jwt"
_TENANT_TOKEN = "vcfa-e2e-tenant-token"

# Operator the dispatch tests act under.
_OPERATOR = Operator(
    sub="vcfa-e2e-test",
    name="VCFA E2E Test Operator",
    email=None,
    raw_jwt="<vcfa-e2e-raw-jwt>",
    tenant_id=VCFA_E2E_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

# The op_id the JSONFlux force-handle test dispatches. ``deployments``
# is the largest tenant payload by design (per #840 acceptance d).
_FORCE_HANDLE_OP_ID = "GET:/iaas/api/deployments"

# Path-template params for the two get-by-id ops (substitution at
# dispatch time). Maps op_id -> the {id} value the route registers
# under.
_GET_BY_ID_PARAMS: dict[str, dict[str, object]] = {
    "GET:/cloudapi/1.0.0/orgs/{id}": {"id": "org-vcfa-e2e"},
    "GET:/cloudapi/1.0.0/regions/{id}": {"id": "region-vcfa-e2e"},
    "GET:/iaas/api/deployments/{id}": {"id": "deployment-vcfa-e2e"},
}

# Persisted as ``Target.fingerprint`` so the resolver binds
# :class:`VcfAutomationConnector`.
_FINGERPRINT: dict[str, Any] = FingerprintResult(
    vendor="vmware",
    product="vcf-automation",
    version="9.0",
    reachable=True,
    probed_at=datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    probe_method="GET /api/versions + GET /iaas/api/about",
    extras={"planes": ["provider", "tenant"]},
).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Inlined respx-replayed VCFA payloads (recorded-fixture shape)
# ---------------------------------------------------------------------------
#
# These are the "recorded fixtures" the issue's acceptance criterion (b)
# references -- minimal but realistic JSON bodies one per op. They live
# inline here rather than under backend/tests/fixtures/vcf/vcf-automation/
# because the dual-plane refresh tool intentionally doesn't drive VCFA
# (G3.6-T13 #841 explicitly excludes it; the recipe registry comment in
# backend/tests/fixtures/vcf/refresh.py records the rationale).

_PROVIDER_SITE: dict[str, Any] = {
    "id": "site-vcfa-e2e",
    "name": "VCFA-E2E",
    "description": "synthetic vcfa-e2e",
    "restName": "vcfa-e2e-rest",
    "productVersion": "9.0.0.0-12345",
}

_PROVIDER_ORGS: dict[str, Any] = {
    "values": [
        {
            "id": "org-vcfa-e2e",
            "name": "acme",
            "displayName": "Acme Corp",
            "isEnabled": True,
            "orgVdcCount": 2,
        },
        {
            "id": "org-other",
            "name": "globex",
            "displayName": "Globex",
            "isEnabled": False,
            "orgVdcCount": 0,
        },
    ],
    "resultTotal": 2,
}

_PROVIDER_ORG_DETAIL: dict[str, Any] = {
    "id": "org-vcfa-e2e",
    "name": "acme",
    "displayName": "Acme Corp",
    "description": "vcfa-e2e org",
    "isEnabled": True,
    "orgVdcCount": 2,
    "userCount": 5,
    "catalogCount": 3,
    "vappCount": 12,
    "runningVMCount": 25,
    "diskCount": 40,
}

_PROVIDER_REGIONS: dict[str, Any] = {
    "values": [
        {
            "id": "region-vcfa-e2e",
            "name": "rdc-east",
            "description": "RDC east region",
            "isEnabled": True,
            "nsxManager": {"id": "nsx-1", "name": "nsx-rdc"},
        }
    ],
    "resultTotal": 1,
}

_PROVIDER_REGION_DETAIL: dict[str, Any] = {
    "id": "region-vcfa-e2e",
    "name": "rdc-east",
    "description": "RDC east region detail",
    "isEnabled": True,
    "nsxManager": {"id": "nsx-1", "name": "nsx-rdc"},
    "supervisors": [{"id": "sup-1", "name": "rdc-supervisor"}],
    "storagePolicies": [],
    "totalCpuMhz": 100000,
    "totalMemoryMB": 524288,
    "allocatedCpuMhz": 50000,
    "allocatedMemoryMB": 262144,
}

_PROVIDER_USERS: dict[str, Any] = {
    "values": [
        {
            "id": "user-1",
            "username": "admin@System",
            "fullName": "System Administrator",
            "email": "admin@example.test",
            "isEnabled": True,
            "roleEntityRefs": [{"id": "role-system-admin"}],
        }
    ],
    "resultTotal": 1,
}

_TENANT_ABOUT: dict[str, Any] = {
    "latestApiVersion": "2024-01-01",
    "supportedApis": [
        {"apiVersion": "2024-01-01", "documentation": "https://example.test/iaas"},
        {"apiVersion": "2023-01-01", "documentation": "https://example.test/iaas-old"},
    ],
}

_TENANT_PROJECTS: dict[str, Any] = {
    "content": [
        {
            "id": "project-vcfa-e2e",
            "name": "team-a",
            "description": "team-a project",
            "organizationId": "org-vcfa-e2e",
            "administrators": [],
            "members": [],
            "operationTimeout": 3600,
        }
    ],
    "totalElements": 1,
    "totalPages": 1,
}

# Deployments — populated with 8 entries so the force-handle reducer
# sees a non-trivial list to sample from. The first entry's id matches
# the get-by-id route below.
_TENANT_DEPLOYMENTS: dict[str, Any] = {
    "content": [
        {
            "id": f"deployment-vcfa-e2e{'' if i == 0 else f'-{i}'}",
            "name": f"deploy-{i:02d}",
            "description": "synthetic deployment",
            "status": "CREATE_SUCCESSFUL",
            "projectId": "project-vcfa-e2e",
            "blueprintId": "blueprint-vcfa-e2e",
            "ownedBy": "user-1",
            "createdAt": "2026-05-01T00:00:00Z",
            "lastUpdatedAt": "2026-05-15T00:00:00Z",
            "resources": [],
        }
        for i in range(8)
    ],
    "totalElements": 8,
    "totalPages": 1,
}

_TENANT_DEPLOYMENT_DETAIL: dict[str, Any] = {
    "id": "deployment-vcfa-e2e",
    "name": "deploy-00",
    "description": "synthetic deployment detail",
    "status": "CREATE_SUCCESSFUL",
    "projectId": "project-vcfa-e2e",
    "blueprintId": "blueprint-vcfa-e2e",
    "ownedBy": "user-1",
    "createdAt": "2026-05-01T00:00:00Z",
    "lastUpdatedAt": "2026-05-15T00:00:00Z",
    "resources": [
        {"id": "res-vm-1", "type": "Cloud.vSphere.Machine", "name": "vm-app-01"},
    ],
    "lastRequestId": "req-deploy-1",
    "inputs": {"size": "M"},
}

_TENANT_BLUEPRINTS: dict[str, Any] = {
    "content": [
        {
            "id": "blueprint-vcfa-e2e",
            "name": "web-app",
            "description": "Web app template",
            "projectId": "project-vcfa-e2e",
            "version": "1.0",
            "status": "RELEASED",
            "updatedAt": "2026-04-01T00:00:00Z",
            "content": "name: web-app\n",
        }
    ],
    "totalElements": 1,
    "totalPages": 1,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars that :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches around every test."""
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stub out :func:`publish_event` so the broadcast bus doesn't fire."""
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _vcfa_credentials_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Stub credentials loader -- bypasses the live Vault read for the E2E dispatch tests.

    The dual-plane dispatch acceptance criteria in #840 don't exercise
    the operator-context Vault read (that's the responsibility of the
    cred-read recorded-fixture E2E in
    ``test_connectors_vcf_automation_credread.py``). Keeping the stub
    here avoids forcing every dispatch test to wire the Vault fake.
    """
    return {"username": "svc-meho", "password": "vcfa-e2e-password"}


async def _insert_vcfa_descriptors() -> None:
    """Seed the 11 curated VCFA core ops + their 8 groups as enabled rows.

    Every row carries ``product=VCFA_PRODUCT="vcfa"`` (what
    :func:`parse_connector_id("vcfa-rest-9.0")` derives) and the
    ``spec:<source>`` tag the G0.7 ingest writes -- ``spec:cloudapi``
    for provider-plane ops, ``spec:iaas`` for tenant-plane ops.
    The tag is the load-bearing signal the dispatcher's plane-aware
    auth path would use; the descriptors below mirror that contract
    so the dispatch test exercises the real shape.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group in VCFA_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=VCFA_PRODUCT,
                version=VCFA_VERSION,
                impl_id=VCFA_IMPL_ID,
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in VCFA_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            spec_tag = "spec:iaas" if path.startswith("/iaas/api/") else "spec:cloudapi"
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=VCFA_PRODUCT,
                version=VCFA_VERSION,
                impl_id=VCFA_IMPL_ID,
                op_id=op.op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[op.group_key],
                summary=f"VCFA core op {op.op_id} (curated read).",
                description=f"VCFA core op {op.op_id} (curated read).",
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=op.llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=[spec_tag],
            )
            session.add(descriptor)
        await session.commit()


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` declaring each ``{var}`` as a path param."""
    import re as _re

    placeholders = _re.findall(r"\{([^{}]+)\}", path)
    if not placeholders:
        return {"type": "object", "properties": {}}
    return {
        "type": "object",
        "properties": {
            name: {"type": "string", "x-meho-param-loc": "path"} for name in placeholders
        },
        "required": list(placeholders),
    }


async def _seed_target(*, host: str, fqdn: str | None) -> Target:
    """Insert the E2E Target row and return it (expunged from the session).

    Centralised so both the happy-path and the IP-without-fqdn scenarios
    can pick their own host / fqdn combination.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=VCFA_E2E_OPERATOR_TENANT,
            name=VCFA_E2E_TARGET_NAME,
            aliases=[],
            product=VcfAutomationConnector.product,
            host=host,
            port=443,
            fqdn=fqdn,
            secret_ref="vcfa/e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=_FINGERPRINT,
            notes="seeded by test_connectors_vcf_automation_e2e._seed_target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> VcfAutomationConnector:
    """Resolve + cache the VcfAutomationConnector instance with a stubbed loader."""
    registry = all_connectors_v2()
    connector_cls = registry.get((VCFA_PRODUCT_REGISTRY, VCFA_VERSION, VCFA_IMPL_ID))
    if connector_cls is None:
        # Re-import the package if a sibling test cleared the v2 registry.
        import importlib

        import meho_backplane.connectors.vcf_automation as _vcfa_pkg

        importlib.reload(_vcfa_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get((VCFA_PRODUCT_REGISTRY, VCFA_VERSION, VCFA_IMPL_ID))
    assert connector_cls is VcfAutomationConnector, (
        f"expected VcfAutomationConnector registered for "
        f"({VCFA_PRODUCT_REGISTRY}, {VCFA_VERSION}, {VCFA_IMPL_ID}); got {connector_cls!r}"
    )
    instance = get_or_create_connector_instance(connector_cls)
    instance._credentials_loader = _vcfa_credentials_loader  # type: ignore[attr-defined]
    return instance


# The connector class registers under the *target* product slug
# (``"vcf-automation"``); the descriptor rows use the ``vcfa`` slug
# parse_connector_id derives. The two are distinct deliberately --
# same shape the SDDC Manager precedent established (sddc-manager vs
# sddc).
VCFA_PRODUCT_REGISTRY = VcfAutomationConnector.product


def _register_vcfa_routes(mock: respx.MockRouter) -> None:
    """Register the dual-plane login + 11 read-op respx routes on *mock*.

    Provider plane:
      * ``POST /cloudapi/1.0.0/sessions/provider`` -> 200 +
        ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` header (the JWT).
      * 6 ``GET`` ops returning the inlined fixtures.

    Tenant plane:
      * ``POST /iaas/api/login`` -> 200 + ``{"token": ...}`` body.
      * 5 ``GET`` ops returning the inlined fixtures.
    """
    # ---- provider login ----
    mock.post("/cloudapi/1.0.0/sessions/provider").respond(
        200,
        headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": _PROVIDER_JWT},
    )
    # ---- tenant login ----
    mock.post("/iaas/api/login").respond(200, json={"token": _TENANT_TOKEN})

    # ---- provider read ops ----
    mock.get("/cloudapi/1.0.0/site").respond(200, json=_PROVIDER_SITE)
    mock.get("/cloudapi/1.0.0/orgs").respond(200, json=_PROVIDER_ORGS)
    mock.get("/cloudapi/1.0.0/orgs/org-vcfa-e2e").respond(200, json=_PROVIDER_ORG_DETAIL)
    mock.get("/cloudapi/1.0.0/regions").respond(200, json=_PROVIDER_REGIONS)
    mock.get("/cloudapi/1.0.0/regions/region-vcfa-e2e").respond(200, json=_PROVIDER_REGION_DETAIL)
    mock.get("/cloudapi/1.0.0/users").respond(200, json=_PROVIDER_USERS)

    # ---- tenant read ops ----
    mock.get("/iaas/api/about").respond(200, json=_TENANT_ABOUT)
    mock.get("/iaas/api/projects").respond(200, json=_TENANT_PROJECTS)
    mock.get("/iaas/api/deployments").respond(200, json=_TENANT_DEPLOYMENTS)
    mock.get("/iaas/api/deployments/deployment-vcfa-e2e").respond(
        200, json=_TENANT_DEPLOYMENT_DETAIL
    )
    mock.get("/iaas/api/blueprints").respond(200, json=_TENANT_BLUEPRINTS)


@dataclass(frozen=True)
class _VcfaE2EBundle:
    target_name: str
    connector_instance: VcfAutomationConnector
    db_target: Any


@pytest.fixture
async def vcfa_e2e_canary(captured_events: list[Any]) -> AsyncIterator[_VcfaE2EBundle]:
    """Dispatcher-ready VCFA setup over a respx-mocked dual-plane appliance.

    Lifecycle:
    1. Insert :data:`VCFA_CORE_OPS` descriptors + groups into the
       per-test SQLite DB.
    2. Seed a :class:`Target` row carrying :data:`_FINGERPRINT` so the
       resolver binds :class:`VcfAutomationConnector`. The target's
       ``host`` is an IP literal and ``fqdn`` is the canonical vhost
       -- this combination is the load-bearing one (vhost routing
       working when reached by IP). Without ``fqdn``,
       :func:`_base_url` raises ``VcfAutomationConfigurationError`` at
       session-establish (see the
       :func:`test_vcfa_e2e_ip_without_fqdn_surfaces_structured_error`
       test below).
    3. Resolve + cache the connector instance; patch its
       ``_credentials_loader`` to bypass Vault.
    4. Activate a respx router for the FQDN-rooted base URL and
       register the dual-plane login + 11 read-op routes.
    """
    await _insert_vcfa_descriptors()
    seeded_target = await _seed_target(host="10.10.10.5", fqdn=VCFA_E2E_FQDN)
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VCFA_E2E_FQDN_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vcfa_routes(mock)
        try:
            yield _VcfaE2EBundle(
                target_name=VCFA_E2E_TARGET_NAME,
                connector_instance=instance,
                db_target=seeded_target,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in VCFA_CORE_OPS)
assert len(_OP_IDS) == 11, f"Expected 11 curated VCFA ops, got {len(_OP_IDS)}: {_OP_IDS}"


@pytest.mark.parametrize("op_id", _OP_IDS, ids=lambda op: op)
async def test_vcfa_e2e_all_ops_dispatch_ok(
    op_id: str,
    vcfa_e2e_canary: _VcfaE2EBundle,
) -> None:
    """All 11 VCFA core ops dispatch and return ``status='ok'``.

    Exercises acceptance criterion (a) -- both planes are covered
    because :data:`VCFA_CORE_OPS` spans 6 provider + 5 tenant ops.
    Each plane establishes its own bespoke session on first dispatch;
    subsequent dispatches re-use the cached per-plane token.
    """
    params = _GET_BY_ID_PARAMS.get(op_id, {})
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VCFA_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": vcfa_e2e_canary.target_name},
            "params": params,
        },
    )
    assert result["status"] == "ok", (
        f"VCFA op {op_id!r} did not return status='ok': "
        f"error={result.get('error')!r} full={result!r}"
    )


async def test_vcfa_e2e_provider_login_fires_once_per_target(
    vcfa_e2e_canary: _VcfaE2EBundle,
) -> None:
    """Provider session-establish runs on first dispatch and the JWT caches.

    Verifies the provider half of the dual-plane auth contract: the
    first ``/cloudapi/*`` dispatch fires
    ``POST /cloudapi/1.0.0/sessions/provider``; subsequent dispatches
    re-use the cached JWT (the cache lives on
    :attr:`VcfAutomationConnector._provider_tokens`).
    """
    instance = vcfa_e2e_canary.connector_instance
    target_name = vcfa_e2e_canary.target_name
    # The token caches key on the tenant-unique (tenant_id, id) tuple
    # (#1642/#1672), not the bare name.
    cache_key = target_cache_key(vcfa_e2e_canary.db_target)

    assert cache_key not in instance._provider_tokens, (
        f"Expected empty provider cache before first dispatch; got {instance._provider_tokens!r}"
    )
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VCFA_CONNECTOR_ID,
            "op_id": "GET:/cloudapi/1.0.0/site",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    assert instance._provider_tokens.get(cache_key) == _PROVIDER_JWT, (
        "Expected provider JWT cached after first provider-plane dispatch; "
        f"got _provider_tokens={instance._provider_tokens!r}"
    )
    # Tenant cache must remain untouched: provider dispatch only
    # establishes the provider session.
    assert instance._tenant_tokens.get(cache_key) is None, (
        f"Tenant cache should be empty after provider-only dispatch; "
        f"got _tenant_tokens={instance._tenant_tokens!r}"
    )


async def test_vcfa_e2e_tenant_login_fires_once_per_target(
    vcfa_e2e_canary: _VcfaE2EBundle,
) -> None:
    """Tenant session-establish runs on first tenant dispatch and the token caches."""
    instance = vcfa_e2e_canary.connector_instance
    target_name = vcfa_e2e_canary.target_name
    cache_key = target_cache_key(vcfa_e2e_canary.db_target)

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VCFA_CONNECTOR_ID,
            "op_id": "GET:/iaas/api/about",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    assert instance._tenant_tokens.get(cache_key) == _TENANT_TOKEN, (
        "Expected tenant token cached after first tenant-plane dispatch; "
        f"got _tenant_tokens={instance._tenant_tokens!r}"
    )


async def test_vcfa_e2e_dispatch_writes_audit_row(
    vcfa_e2e_canary: _VcfaE2EBundle,
) -> None:
    """Each dispatch inserts an AuditLog row with op_id + target_id + params_hash.

    Exercises acceptance criterion (c).
    """
    op_id = "GET:/cloudapi/1.0.0/site"
    sessionmaker = get_sessionmaker()

    async def _count_dispatch_rows() -> int:
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.method == "DISPATCH",
                    AuditLog.path == op_id,
                )
            )
            return len(list(result.scalars().all()))

    baseline = await _count_dispatch_rows()
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VCFA_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": vcfa_e2e_canary.target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    final = await _count_dispatch_rows()
    assert final - baseline == 1, (
        f"Expected exactly one new DISPATCH row for {op_id!r}; baseline={baseline} final={final}"
    )

    async with sessionmaker() as session:
        row_result = await session.execute(
            select(AuditLog)
            .where(AuditLog.method == "DISPATCH", AuditLog.path == op_id)
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        row = row_result.scalars().first()
    assert row is not None
    assert row.target_id is not None, (
        "AuditLog.target_id must not be None for a targeted dispatch; "
        f"got target_id=None on row {row!r}"
    )
    assert row.payload.get("op_id") == op_id, (
        f"AuditLog.payload['op_id'] must equal the dispatched op_id; got payload={row.payload!r}"
    )
    assert row.payload.get("params_hash"), (
        f"AuditLog.payload must carry a non-empty 'params_hash'; got payload={row.payload!r}"
    )


async def test_vcfa_e2e_jsonflux_handle_populated_for_deployment_list(
    vcfa_e2e_canary: _VcfaE2EBundle,
) -> None:
    """Tenant deployment list dispatched with the real JsonFluxReducer returns a populated handle.

    Exercises acceptance criterion (d) -- the JSONFlux seam threads
    the reducer's :class:`ResultHandle` onto :class:`OperationResult`.
    """
    expected_rows = len(_TENANT_DEPLOYMENTS["content"])  # type: ignore[arg-type]

    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        result_envelope = await call_operation(
            _OPERATOR,
            {
                "connector_id": VCFA_CONNECTOR_ID,
                "op_id": _FORCE_HANDLE_OP_ID,
                "target": {"name": vcfa_e2e_canary.target_name},
                "params": {},
            },
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result_envelope["status"] == "ok", (
        f"Expected JSONFlux dispatch to succeed; got {result_envelope!r}"
    )
    handle = result_envelope.get("handle")
    assert handle is not None, (
        "Expected OperationResult.handle to be populated by JsonFluxReducer; "
        f"got handle=None on envelope={result_envelope!r}"
    )
    uuid.UUID(handle["handle_id"])
    assert handle["total_rows"] == expected_rows, (
        f"Expected {expected_rows} deployment rows from _TENANT_DEPLOYMENTS; "
        f"got handle.total_rows={handle['total_rows']}"
    )
    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"Expected ≥1 sample row from the seeded deployment list; got sample_rows={sample_rows!r}"
    )
    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"Expected reducer summary on result.row_count={expected_rows}; got result={payload!r}"
    )


async def test_vcfa_e2e_per_call_fqdn_override_threads_to_connector(
    captured_events: list[Any],
) -> None:
    """Per-call ``target.fqdn`` override on the dispatch body wins over the DB row.

    Exercises acceptance criterion (e). The seeded Target has its
    ``fqdn`` column set to the **wrong** value; the dispatch body
    supplies the correct vhost as a per-call override. The connector
    uses the override at base-URL composition time so the request
    lands on the FQDN-rooted respx router. Without the override,
    the dispatch would land on a different (un-mocked) host and the
    request would fail.

    The DB row is **not** modified by the override -- a follow-up
    fetch confirms the persisted ``fqdn`` is still the wrong value.
    """
    await _insert_vcfa_descriptors()
    # Seed the target with the *wrong* fqdn so the per-call override
    # is the only way the dispatch can find the mocked appliance.
    wrong_fqdn = "vcfa-wrong.test.invalid"
    await _seed_target(host="10.10.10.5", fqdn=wrong_fqdn)
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VCFA_E2E_FQDN_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vcfa_routes(mock)
        try:
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VCFA_CONNECTOR_ID,
                    "op_id": "GET:/cloudapi/1.0.0/site",
                    "target": {"name": VCFA_E2E_TARGET_NAME, "fqdn": VCFA_E2E_FQDN},
                    "params": {},
                },
            )
            assert result["status"] == "ok", (
                f"Per-call fqdn override should reach the mocked appliance; got {result!r}"
            )

            # DB row's fqdn must NOT have been mutated by the per-call override.
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                row = (
                    await session.execute(select(Target).where(Target.name == VCFA_E2E_TARGET_NAME))
                ).scalar_one()
                assert row.fqdn == wrong_fqdn, (
                    "Per-call fqdn override must not modify the DB row; "
                    f"got persisted Target.fqdn={row.fqdn!r}"
                )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


async def test_vcfa_e2e_ip_host_without_fqdn_surfaces_descriptive_error(
    captured_events: list[Any],
) -> None:
    """IP-host target with no ``fqdn`` set surfaces a descriptive error_code.

    Exercises acceptance criterion (b)'s "descriptive error, not blank
    404" requirement from issue #840. The connector raises
    :class:`VcfAutomationConfigurationError` at base-URL composition
    time when ``host`` is an IP literal and ``fqdn`` is unset; the
    dispatcher wraps that into a structured ``status='error'``
    envelope so the caller sees a clear "set --fqdn" message rather
    than a confusing post-login 404 storm.
    """
    await _insert_vcfa_descriptors()
    await _seed_target(host="10.10.10.5", fqdn=None)
    instance = _resolve_connector()

    try:
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": VCFA_CONNECTOR_ID,
                "op_id": "GET:/cloudapi/1.0.0/site",
                "target": {"name": VCFA_E2E_TARGET_NAME},
                "params": {},
            },
        )
    finally:
        await instance.aclose()
        reset_dispatcher_caches()

    assert result["status"] == "error", f"IP-host-no-fqdn dispatch must error; got {result!r}"
    # The top-level ``error`` field carries the class name; the
    # descriptive message lands in extras.exception_message per the
    # dispatcher's :func:`result_connector_error` contract.
    err_top = result.get("error") or ""
    assert "VcfAutomationConfigurationError" in err_top, (
        f"top-level error should name the configuration error class; got {err_top!r}"
    )
    extras = result.get("extras") or {}
    exc_msg = extras.get("exception_message") or ""
    assert "fqdn" in exc_msg.lower(), (
        f"exception_message must name 'fqdn' so operators know how to fix it; got {exc_msg!r}"
    )
    assert "10.10.10.5" in exc_msg, (
        f"exception_message must echo the IP that triggered the failure; got {exc_msg!r}"
    )


async def test_vcfa_e2e_provider_request_carries_bearer_jwt(
    captured_events: list[Any],
) -> None:
    """Provider-plane GET request carries Authorization: Bearer <provider-jwt>.

    Asserts the post-login Bearer header is the JWT the provider
    session-create endpoint returned -- the round-trip that proves
    the connector parsed the response header correctly. Tenant
    routes' Bearer must NOT carry the provider JWT (the per-plane
    isolation contract). Uses a dedicated respx router with a
    side-effect callback so the test owns the route shape rather
    than re-using the fixture's static responder.
    """
    await _insert_vcfa_descriptors()
    await _seed_target(host="10.10.10.5", fqdn=VCFA_E2E_FQDN)
    instance = _resolve_connector()

    captured: dict[str, str] = {}

    def _site_responder(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=_PROVIDER_SITE)

    try:
        async with respx.mock(
            base_url=VCFA_E2E_FQDN_BASE_URL,
            assert_all_called=False,
            assert_all_mocked=False,
        ) as mock:
            mock.post("/cloudapi/1.0.0/sessions/provider").respond(
                200,
                headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": _PROVIDER_JWT},
            )
            mock.get("/cloudapi/1.0.0/site").mock(side_effect=_site_responder)

            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VCFA_CONNECTOR_ID,
                    "op_id": "GET:/cloudapi/1.0.0/site",
                    "target": {"name": VCFA_E2E_TARGET_NAME},
                    "params": {},
                },
            )
            assert result["status"] == "ok"
            assert captured.get("/cloudapi/1.0.0/site") == f"Bearer {_PROVIDER_JWT}", (
                "Provider-plane GET must carry the provider JWT as Bearer; "
                f"got Authorization={captured.get('/cloudapi/1.0.0/site')!r}"
            )
    finally:
        await instance.aclose()
        reset_dispatcher_caches()
