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
  park), validate ``safe``.
* The v2 registry resolves the connector for a ``(mehoauto, 0.1.0)`` target
  fingerprint (boot-stamped from the shipped profile).

The MCP agent surface is unchanged (no per-op tools) — the three ops ride
``op_id`` under ``call_operation``; ``test_mcp_surface_conformance`` guards the
tool inventory globally and is unaffected by this data-only connector.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import select

from meho_backplane.connectors.base import shim_kind
from meho_backplane.connectors.meho_automation.ingest_safety import meho_automation_safety_floor
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
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    has_safety_floor,
)
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
        ("POST", _VALIDATE, "safe"),
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
    # The decided tiers, pinned by the connector floor.
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
    ) == ("safe", False)


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
    assert floored[f"POST:{_LAUNCH}"].safety_level == "caution"
    assert floored[f"POST:{_GATE}"].safety_level == "caution"
    assert floored[f"POST:{_VALIDATE}"].safety_level == "safe"
    for p in protos:
        assert has_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=p)


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
