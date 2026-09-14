# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The meho-automation add-on registered as a profile-backed generic connector.

Pins the acceptance of the governed-launch T3 task (public issue: register the
automation add-on as a profile-backed generic connector target; launch /
validate / gate ops; oauth2_mint external issuer; runtime spec ingest):

* The shipped catalog row + ExecutionProfile parse and clear every boot guard
  WITHOUT a vendored spec (``spec_resource: null``): the OpenAPI is ingested at
  registration time from the add-on's ``/openapi.json`` via the operator
  ``--spec`` on-ramp, never committed to this repo.
* The profile declares ``oauth2_mint`` with an EXTERNAL issuer whose
  ``token_url`` is sourced per-target from the Vault credential (never
  hard-coded in the public profile), audience ``meho-automation``.
* A fixture-backed ingest of a MINIMAL SYNTHETIC 3-route OpenAPI (generic
  schema names, never the real spec) lands exactly the three ops staged /
  disabled with the decided tiers: launch + gate ``caution`` (no approval
  park), and validate ``caution`` too — a read-side dry-run that still rides
  caution because ingested POSTs never sit below the caution floor.
* The v2 registry resolves the connector for a ``(mehoauto, 0.1.0)`` target
  fingerprint (boot-stamped from the shipped profile).

The MCP agent surface is unchanged (no per-op tools) — the three ops ride
``op_id`` under ``call_operation``; ``test_mcp_surface_conformance`` guards the
tool inventory globally and is unaffected by this data-only connector.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import select

from meho_backplane.connectors.base import shim_kind
from meho_backplane.connectors.meho_automation.ingest_safety import (
    meho_automation_safety_floor,
    register_safety_floor,
)
from meho_backplane.connectors.profile import ExecutionProfile, validate_execution_profile
from meho_backplane.connectors.registry import (
    _eager_import_connectors,
    all_connectors_v2,
    clear_registry,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest.boot_stamp import stamp_catalog_profiled_connectors
from meho_backplane.operations.ingest.catalog import (
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    load_catalog,
    load_profile_resource,
    validate_catalog_registry_coverage,
    validate_shipped_artifacts,
)
from meho_backplane.operations.ingest.openapi import parse_openapi
from meho_backplane.operations.ingest.parser import parse_connector_id
from meho_backplane.operations.ingest.register_ingested import register_ingested_operations
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor, has_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

_PRODUCT = "mehoauto"
_VERSION = "0.1.0"
_IMPL_ID = "mehoauto-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_TRIPLE = (_PRODUCT, _VERSION, _IMPL_ID)

_LAUNCH = "/api/v1/runs"
_VALIDATE = "/api/v1/blueprints/{blueprint_id}/validate"
_GATE = "/api/v1/runs/{run_id}/gates/{node_id}/decision"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@dataclass
class _FakeFingerprint:
    version: str | None


@dataclass
class _FakeTarget:
    product: str
    fingerprint: _FakeFingerprint | None = None
    preferred_impl_id: str | None = None
    version: str | None = None


def _synthetic_spec() -> str:
    """A minimal OpenAPI 3.1 with the add-on's three route paths.

    GENERIC schema names only — this is NOT the add-on's real spec, which is
    never vendored into this repo. It exists only to prove that an ingest of a
    3-route spec lands exactly those three ops with the decided tiers.
    """
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic Automation", "version": _VERSION},
        "paths": {
            _LAUNCH: {
                "post": {
                    "summary": "Launch a run",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/LaunchBody"}
                            }
                        }
                    },
                    "responses": {
                        "202": {
                            "description": "accepted",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Record"}
                                }
                            },
                        }
                    },
                }
            },
            _VALIDATE: {
                "post": {
                    "summary": "Validate a blueprint",
                    "parameters": [
                        {
                            "name": "blueprint_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ValidateBody"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Report"}
                                }
                            },
                        }
                    },
                }
            },
            _GATE: {
                "post": {
                    "summary": "Decide a gate",
                    "parameters": [
                        {
                            "name": "run_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "node_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DecisionBody"}
                            }
                        }
                    },
                    "responses": {
                        "202": {
                            "description": "accepted",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Record"}
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "LaunchBody": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "inputs": {"type": "object"}},
                },
                "ValidateBody": {"type": "object", "properties": {"inputs": {"type": "object"}}},
                "DecisionBody": {"type": "object", "properties": {"decision": {"type": "string"}}},
                "Record": {"type": "object", "properties": {"id": {"type": "string"}}},
                "Report": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            }
        },
    }
    return json.dumps(spec)


def _shipped_row() -> ConnectorSpecEntry:
    return next(e for e in load_catalog().entries if e.product == _PRODUCT)


def _shipped_profile() -> ExecutionProfile:
    return ExecutionProfile.model_validate(
        yaml.safe_load(load_profile_resource("meho_automation_minimal.yaml"))
    )


# ---------------------------------------------------------------------------
# Shipped catalog row + profile shape (no vendored spec)
# ---------------------------------------------------------------------------


def test_shipped_catalog_row_is_profile_backed_without_a_vendored_spec() -> None:
    row = _shipped_row()
    assert (row.product, row.version, row.impl_id) == _TRIPLE
    assert row.upstream is None
    # No spec is vendored: the OpenAPI is ingested at registration from the
    # add-on's /openapi.json via the operator --spec on-ramp.
    assert row.spec_resource is None
    assert row.profile_resource == "meho_automation_minimal.yaml"
    assert row.catalog_ingest == "spec-only"
    assert row.requires_connector_class == "ProfiledRestConnector_mehoauto_0_1_0"


def test_shipped_profile_declares_oauth2_external_issuer_credential_sourced() -> None:
    profile = _shipped_profile()
    assert (profile.product, profile.version) == (_PRODUCT, _VERSION)
    assert profile.auth.scheme == "oauth2_mint"
    # client_id/client_secret AND token_url all come from the Vault credential;
    # the realm token endpoint is a per-deployment value, never hard-coded in
    # the public profile.
    assert profile.auth.secret_fields == ("client_id", "client_secret", "token_url")
    assert profile.auth.token_url is None
    assert profile.auth.scope is None
    assert profile.auth.audience == "meho-automation"
    assert profile.fingerprint.path == "/api/v1/version"
    assert profile.fingerprint.authenticated is False
    assert profile.fingerprint.version_key == "version"
    assert profile.probe == "delegate"


def test_connector_id_round_trips_to_the_dispatch_triple() -> None:
    # The hyphen-free product slug is what makes the connector_id parse recover
    # ``mehoauto`` cleanly (a hyphenated product would not round-trip).
    assert parse_connector_id(_CONNECTOR_ID) == _TRIPLE


def test_boot_guards_accept_the_row_and_profile_without_a_spec() -> None:
    _eager_import_connectors()
    catalog = load_catalog()
    # None of these raise: coverage exempts the profile-backed row, and
    # validate_shipped_artifacts only dry-run-parses the profile (spec_resource
    # is null, so there is nothing to parse — and nothing to leak).
    validate_catalog_registry_coverage(catalog)
    validate_shipped_artifacts(catalog)
    profile = _shipped_profile()
    validate_execution_profile(profile)


# ---------------------------------------------------------------------------
# Safety floor — decided tiers, pinned and idempotent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", _LAUNCH, "caution"),
        ("POST", _GATE, "caution"),
        ("POST", _VALIDATE, "caution"),
    ],
)
def test_safety_floor_pins_the_decided_tiers(method: str, path: str, expected: str) -> None:
    proto = _proto(method, path)
    floored = meho_automation_safety_floor(_VERSION, proto)
    assert floored.safety_level == expected
    # None of the three park for a backplane approval — the add-on's own gate
    # nodes are the human control (operator decision).
    assert floored.requires_approval is False


def test_safety_floor_is_idempotent_and_version_scoped() -> None:
    proto = _proto("POST", _LAUNCH)
    once = meho_automation_safety_floor(_VERSION, proto)
    twice = meho_automation_safety_floor(_VERSION, once)
    assert (twice.safety_level, twice.requires_approval) == ("caution", False)
    # A different version label is not this connector's concern — untouched.
    assert meho_automation_safety_floor("9.9", proto) is proto
    # An op the connector does not curate is passed through unchanged.
    other = _proto("GET", "/api/v1/runs")
    assert meho_automation_safety_floor(_VERSION, other) is other


def _proto(method: str, path: str) -> EndpointDescriptorProto:
    return EndpointDescriptorProto(
        op_id=f"{method}:{path}",
        method=method,
        path=path,
        summary="s",
        description="d",
        tags=["ops"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema={"type": "object"},
        safety_level="caution",
        requires_approval=False,
    )


# ---------------------------------------------------------------------------
# Fixture-backed ingest — exactly three ops, staged/disabled, decided tiers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixture_ingest_lands_three_ops_staged_with_decided_tiers(
    stub_embedding_service: AsyncMock,
) -> None:
    _eager_import_connectors()  # registers the (mehoauto, mehoauto-rest) floor
    protos = parse_openapi(
        "spec:synthetic-meho-automation",
        spec_source="spec:synthetic-meho-automation",
        content=_synthetic_spec(),
    )
    # Ingest lands exactly the three routes as op_id = method:path (the ops ride
    # op_id under call_operation — no per-op MCP tool is created).
    assert {p.op_id for p in protos} == {
        f"POST:{_LAUNCH}",
        f"POST:{_VALIDATE}",
        f"POST:{_GATE}",
    }

    result = await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_source="synthetic-meho-automation",
        operations=protos,
        base_url="http://meho-automation:8000",
        embedding_service=stub_embedding_service,
        register_shim=False,  # the profiled class is stamped from the catalog
    )
    assert result.inserted_count == 3

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 3
    by_id = {row.op_id: row for row in rows}
    # Staged / disabled at ingest — the review gate stays the interlock.
    for row in rows:
        assert row.is_enabled is False
        assert row.source_kind == "ingested"
        assert row.handler_ref is None
    # The decided tiers. All three ingested POSTs ride `caution` (launch/gate
    # is the decided tier; validate is a read-side dry-run that still rides
    # caution because an ingested POST never sits below the caution floor).
    # Because the tier is identical whether or not this connector's floor
    # registration survived a prior test's registry reset (the POST heuristic
    # also yields `caution`), these assertions are order-independent — they do
    # not flake under xdist worker/collection ordering.
    assert (by_id[f"POST:{_LAUNCH}"].safety_level, by_id[f"POST:{_LAUNCH}"].requires_approval) == (
        "caution",
        False,
    )
    assert (by_id[f"POST:{_GATE}"].safety_level, by_id[f"POST:{_GATE}"].requires_approval) == (
        "caution",
        False,
    )
    assert (
        by_id[f"POST:{_VALIDATE}"].safety_level,
        by_id[f"POST:{_VALIDATE}"].requires_approval,
    ) == ("caution", False)


def test_ingest_protos_carry_the_floor_before_persistence() -> None:
    """The floor is applied at ingest (apply_safety_floor), not only in the DB."""
    protos = parse_openapi(
        "spec:synthetic-meho-automation",
        spec_source="spec:synthetic-meho-automation",
        content=_synthetic_spec(),
    )
    _eager_import_connectors()
    floored = {
        p.op_id: apply_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=p)
        for p in protos
    }
    # All three ingested POSTs ride `caution` at ingest (see the decided-tiers
    # note in the fixture-ingest test). The tier holds whether or not the
    # connector's floor registration survived a prior test's registry reset, so
    # these assertions are order-independent.
    assert floored[f"POST:{_LAUNCH}"].safety_level == "caution"
    assert floored[f"POST:{_GATE}"].safety_level == "caution"
    assert floored[f"POST:{_VALIDATE}"].safety_level == "caution"
    # The connector curates all three ops — asserted against its own floor
    # function directly (not the process-global registry lookup) so the check
    # stays deterministic regardless of test ordering.
    for p in protos:
        curated = meho_automation_safety_floor(_VERSION, p)
        assert curated is not p
        assert curated.safety_level == "caution"


# ---------------------------------------------------------------------------
# Floor registration is wired into the process-global registry
# ---------------------------------------------------------------------------


def test_connector_registers_its_safety_floor_in_the_global_registry() -> None:
    """A broken side-effect import in the connector package would leave the
    floor unregistered and silently fall back to the generic verb heuristic.

    Order-robust: re-invoke the connector's own idempotent registration
    function first (do NOT rely on import order or on another test having
    imported the package), then assert the process-global registry reports the
    floor for the (product, impl_id). ``_FLOORS`` cross-test isolation is
    tracked separately in #3605.
    """
    register_safety_floor()  # idempotent — safe to call regardless of prior state
    # A curated op the floor rewrites -> has_safety_floor detects the floor.
    assert has_safety_floor(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        proto=_proto("POST", _LAUNCH),
    )
    # Idempotency: a second call leaves the floor registered (no duplicate, no
    # unregister) — the assertion still holds.
    register_safety_floor()
    assert has_safety_floor(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        proto=_proto("POST", _LAUNCH),
    )


# ---------------------------------------------------------------------------
# Ingest op allowlist (security review T3-F01) — a wide spec persists exactly
# the three allowlisted ops; every other route is dropped before persistence.
# ---------------------------------------------------------------------------

# The add-on's real /openapi.json publishes ~30 mutating routes; this synthetic
# spec mirrors the route FAMILIES (tenants / sites / env_specs / deployments /
# artifacts / blueprints / environments / adoptions / fleet / runs) with
# GENERIC schema names — it is NOT the add-on's real spec. It includes the
# three allowlisted ops plus DELETE routes and a POST /api/v1/fleet/import.
_WIDE_ROUTE_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        # The three allowlisted ops (must survive ingest).
        ("POST", _LAUNCH),
        ("POST", _VALIDATE),
        ("POST", _GATE),
        # tenants
        ("GET", "/api/v1/tenants"),
        ("POST", "/api/v1/tenants"),
        ("GET", "/api/v1/tenants/{tenant_id}"),
        ("DELETE", "/api/v1/tenants/{tenant_id}"),
        # sites
        ("GET", "/api/v1/sites"),
        ("POST", "/api/v1/sites"),
        ("DELETE", "/api/v1/sites/{site_id}"),
        # env_specs
        ("GET", "/api/v1/env_specs"),
        ("POST", "/api/v1/env_specs"),
        ("PUT", "/api/v1/env_specs/{spec_id}"),
        ("DELETE", "/api/v1/env_specs/{spec_id}"),
        # deployments
        ("GET", "/api/v1/deployments"),
        ("POST", "/api/v1/deployments"),
        ("DELETE", "/api/v1/deployments/{deployment_id}"),
        # artifacts
        ("GET", "/api/v1/artifacts"),
        ("POST", "/api/v1/artifacts"),
        ("DELETE", "/api/v1/artifacts/{artifact_id}"),
        # blueprints (CRUD around the allowlisted validate sub-route)
        ("GET", "/api/v1/blueprints"),
        ("POST", "/api/v1/blueprints"),
        ("PATCH", "/api/v1/blueprints/{blueprint_id}"),
        ("DELETE", "/api/v1/blueprints/{blueprint_id}"),
        # environments
        ("GET", "/api/v1/environments"),
        ("POST", "/api/v1/environments"),
        ("DELETE", "/api/v1/environments/{environment_id}"),
        # adoptions
        ("POST", "/api/v1/adoptions"),
        ("DELETE", "/api/v1/adoptions/{adoption_id}"),
        # fleet
        ("POST", "/api/v1/fleet/import"),
        ("GET", "/api/v1/fleet"),
        # runs (non-allowlisted verbs on the launch collection / items)
        ("GET", "/api/v1/runs"),
        ("GET", "/api/v1/runs/{run_id}"),
        ("DELETE", "/api/v1/runs/{run_id}"),
    }
)


def _wide_spec() -> str:
    """A ~34-route OpenAPI 3.1 mirroring the add-on's route families."""
    paths: dict[str, dict[str, object]] = {}
    for method, path in _WIDE_ROUTE_KEYS:
        operation: dict[str, object] = {
            "summary": f"{method} {path}",
            "responses": {"200": {"description": "ok"}},
        }
        params = [
            {"name": var, "in": "path", "required": True, "schema": {"type": "string"}}
            for var in re.findall(r"{(\w+)}", path)
        ]
        if params:
            operation["parameters"] = params
        if method in ("POST", "PUT", "PATCH"):
            operation["requestBody"] = {
                "content": {"application/json": {"schema": {"type": "object"}}}
            }
        paths.setdefault(path, {})[method.lower()] = operation
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Wide Automation", "version": _VERSION},
        "paths": paths,
    }
    return json.dumps(spec)


@pytest.mark.asyncio
async def test_wide_spec_ingests_to_exactly_the_three_allowlisted_ops(
    stub_embedding_service: AsyncMock,
) -> None:
    register_safety_floor()
    protos = parse_openapi("spec:wide", spec_source="spec:wide", content=_wide_spec())
    # Sanity: the wide spec really does parse the full mutating surface.
    assert len(protos) == len(_WIDE_ROUTE_KEYS)
    assert len(protos) >= 30

    result = await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_source="wide-synthetic-meho-automation",
        operations=protos,
        base_url="http://meho-automation:8000",
        embedding_service=stub_embedding_service,
        register_shim=False,
    )
    # Exactly the three allowlisted ops persist; every other route is dropped
    # before persistence (never staged), and the result carries the count.
    assert result.inserted_count == 3
    assert result.dropped_count == len(_WIDE_ROUTE_KEYS) - 3
    dropped_keys = {(dropped.method, dropped.path) for dropped in result.dropped_ops}
    # The mutating routes the finding calls out are among the dropped set.
    assert ("DELETE", "/api/v1/tenants/{tenant_id}") in dropped_keys
    assert ("POST", "/api/v1/fleet/import") in dropped_keys
    assert ("DELETE", "/api/v1/runs/{run_id}") in dropped_keys
    # None of the three allowlisted ops were dropped.
    assert dropped_keys.isdisjoint({("POST", _LAUNCH), ("POST", _VALIDATE), ("POST", _GATE)})

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )
    assert {row.op_id for row in rows} == {
        f"POST:{_LAUNCH}",
        f"POST:{_VALIDATE}",
        f"POST:{_GATE}",
    }
    # All three ride caution, no approval park, staged/disabled at ingest.
    for row in rows:
        assert (row.safety_level, row.requires_approval) == ("caution", False)
        assert row.is_enabled is False
        assert row.source_kind == "ingested"


@pytest.mark.asyncio
async def test_reingest_of_the_wide_spec_stays_bounded_to_three(
    stub_embedding_service: AsyncMock,
) -> None:
    """Re-ingesting the wide spec keeps exactly three persisted ops — the
    allowlist bounds every register call, not just the first."""
    register_safety_floor()
    protos = parse_openapi("spec:wide", spec_source="spec:wide", content=_wide_spec())

    first = await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_source="wide-synthetic-meho-automation",
        operations=protos,
        base_url="http://meho-automation:8000",
        embedding_service=stub_embedding_service,
        register_shim=False,
    )
    assert first.inserted_count == 3

    second = await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_source="wide-synthetic-meho-automation",
        operations=protos,
        base_url="http://meho-automation:8000",
        embedding_service=stub_embedding_service,
        register_shim=False,
    )
    # Idempotent re-ingest: the three unchanged ops skip, none inserted, and
    # the wide surface is still dropped (bounded on the re-ingest path too).
    assert second.inserted_count == 0
    assert second.dropped_count == len(_WIDE_ROUTE_KEYS) - 3

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        count = len(
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )
    assert count == 3


# ---------------------------------------------------------------------------
# v2 registry resolution — boot-stamp + resolve by target fingerprint
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
async def test_boot_stamp_registers_profiled_connector_and_resolver_resolves_it(
    _clean_registry: None,
) -> None:
    catalog = ConnectorSpecCatalog(
        entries=(
            ConnectorSpecEntry.model_validate(
                {
                    "product": _PRODUCT,
                    "version": _VERSION,
                    "impl_id": _IMPL_ID,
                    "requires_connector_class": "ProfiledRestConnector_mehoauto_0_1_0",
                    "upstream": None,
                    "spec_resource": None,
                    "profile_resource": "meho_automation_minimal.yaml",
                    "catalog_ingest": "spec-only",
                }
            ),
        )
    )

    count = await stamp_catalog_profiled_connectors(catalog)
    assert count == 1

    cls = all_connectors_v2()[_TRIPLE]
    assert shim_kind(cls) == "profiled"
    assert cls.__name__ == "ProfiledRestConnector_mehoauto_0_1_0"
    assert cls.profile is not None
    assert cls.profile.auth.scheme == "oauth2_mint"
    assert cls.profile.auth.audience == "meho-automation"

    # The v2 registry resolves the connector for a (mehoauto, 0.1.0) target
    # fingerprint — the stamped ProfiledRestConnector's supported_version_range
    # covers the label.
    target = _FakeTarget(product=_PRODUCT, fingerprint=_FakeFingerprint(_VERSION))
    assert resolve_connector(target) is cls
