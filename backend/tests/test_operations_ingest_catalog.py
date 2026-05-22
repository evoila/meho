# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the curated connector-spec catalog (Goal #214 on-ramp; #743).

Coverage matrix (Task #743 acceptance criteria):

* **(a) The shipped catalog parses cleanly** — :func:`load_catalog`
  returns a validated :class:`ConnectorSpecCatalog` with one entry per
  v0.3.0 connector.
* **(b) Every ``requires_connector_class`` is registered** —
  :func:`validate_catalog_registry_coverage` cross-checks each entry
  against :func:`all_connectors_v2` with the seven connectors registered.
* **(c) Every ``spec_info_version`` is PEP 440** — the field validator
  rejects a non-PEP-440 string; every present value in the real catalog
  parses via :class:`packaging.version.Version`.

Plus schema-strictness, the malformed-catalog crash path, the
typed-vs-generic ``upstream`` contract, and the ``GET
/api/v1/connectors/catalog`` route the CLI verbs (#915) consume.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from packaging.version import Version
from pydantic import ValidationError

from meho_backplane.api.v1.connectors_ingest import (
    catalog_endpoint,
)
from meho_backplane.api.v1.connectors_ingest import (
    router as connectors_ingest_router,
)
from meho_backplane.audit import AuditMiddleware
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    register_connector_v2,
)
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.ingest.catalog import (
    CatalogError,
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    load_catalog,
    parse_catalog,
    validate_catalog_registry_coverage,
)

# The seven v0.3.0 connectors the catalog enumerates.
_EXPECTED_PRODUCT_VERSION = {
    ("vmware", "9.0"),
    ("sddc-manager", "9.0"),
    ("harbor", "2.x"),
    ("nsx", "4.2"),
    ("vault", "1.x"),
    ("k8s", "1.x"),
    ("bind9", "9.x"),
}
_TYPED_PRODUCTS = {"vault", "k8s", "bind9"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the Keycloak issuer/audience the auth dependency reads.

    The unauthenticated-401 route test drives a request through
    ``require_role``, which constructs :class:`Settings`; without these
    every test file pins them (see ``conftest.py`` and the sibling
    ``test_api_v1_connectors_ingest.py``).
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")


def _entry(**overrides: object) -> ConnectorSpecEntry:
    """Build a minimal valid entry, overriding fields as needed."""
    base: dict[str, object] = {
        "product": "demo",
        "version": "1.0",
        "impl_id": "demo-rest",
        "requires_connector_class": "DemoConnector",
    }
    base.update(overrides)
    return ConnectorSpecEntry.model_validate(base)


# ---------------------------------------------------------------------------
# (a) The shipped catalog parses + has the expected coverage
# ---------------------------------------------------------------------------


def test_shipped_catalog_parses_cleanly() -> None:
    catalog = load_catalog()
    assert isinstance(catalog, ConnectorSpecCatalog)
    pairs = {(e.product, e.version) for e in catalog.entries}
    assert pairs == _EXPECTED_PRODUCT_VERSION


def test_shipped_catalog_typed_connectors_have_null_upstream() -> None:
    """Typed connectors carry ``upstream: null``; generic ones carry URLs."""
    for entry in load_catalog().entries:
        if entry.product in _TYPED_PRODUCTS:
            assert entry.upstream is None, f"{entry.product} should be typed (null upstream)"
        else:
            assert entry.upstream, f"{entry.product} is generic and needs upstream URL(s)"


# ---------------------------------------------------------------------------
# (b) Registry coverage — every requires_connector_class is registered
# ---------------------------------------------------------------------------


@pytest.fixture
def _registered_connectors() -> set[str]:
    """Register the seven catalog connectors, return their class names.

    The autouse ``_isolate_global_registries`` fixture snapshots/restores
    the registry around each test; depending on collection order it may
    present an empty registry (cached imports do not re-fire the module-
    level ``register_connector_v2`` side effect). Registering defensively
    here — swallowing the duplicate-key ``RuntimeError`` — makes the
    coverage assertion order-independent under pytest-xdist.
    """
    from meho_backplane.connectors.bind9 import Bind9Connector
    from meho_backplane.connectors.harbor import HarborConnector
    from meho_backplane.connectors.kubernetes import KubernetesConnector
    from meho_backplane.connectors.nsx import NsxConnector
    from meho_backplane.connectors.sddc_manager import SddcManagerConnector
    from meho_backplane.connectors.vault import VaultConnector
    from meho_backplane.connectors.vmware_rest import VmwareRestConnector

    for cls in (
        Bind9Connector,
        HarborConnector,
        KubernetesConnector,
        NsxConnector,
        SddcManagerConnector,
        VaultConnector,
        VmwareRestConnector,
    ):
        # Swallow the duplicate-key RuntimeError when the connector is
        # already registered this session (cached import history).
        with contextlib.suppress(RuntimeError):
            register_connector_v2(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                cls=cls,
            )
    return {cls.__name__ for cls in all_connectors_v2().values()}


def test_every_requires_connector_class_is_registered(
    _registered_connectors: set[str],
) -> None:
    for entry in load_catalog().entries:
        assert entry.requires_connector_class in _registered_connectors


def test_validate_catalog_registry_coverage_passes_for_shipped_catalog(
    _registered_connectors: set[str],
) -> None:
    validate_catalog_registry_coverage()  # must not raise


def test_validate_catalog_registry_coverage_raises_on_unknown_class() -> None:
    bogus = ConnectorSpecCatalog(
        entries=(_entry(requires_connector_class="NoSuchConnector999"),),
    )
    with pytest.raises(CatalogError, match="unregistered connector class"):
        validate_catalog_registry_coverage(bogus)


# ---------------------------------------------------------------------------
# (c) spec_info_version is PEP 440
# ---------------------------------------------------------------------------


def test_shipped_catalog_spec_info_versions_are_pep440() -> None:
    for entry in load_catalog().entries:
        if entry.spec_info_version is not None:
            Version(entry.spec_info_version)  # raises InvalidVersion on failure


def test_entry_accepts_valid_pep440_spec_info_version() -> None:
    assert _entry(spec_info_version="9.0.1").spec_info_version == "9.0.1"


def test_entry_rejects_non_pep440_spec_info_version() -> None:
    with pytest.raises(ValidationError, match="PEP 440"):
        _entry(spec_info_version="not-a-version")


# ---------------------------------------------------------------------------
# Schema strictness + malformed-catalog crash path
# ---------------------------------------------------------------------------


def test_entry_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _entry(typo_field="oops")


def test_entry_rejects_bad_sha256() -> None:
    with pytest.raises(ValidationError, match="64 lowercase hex"):
        _entry(sha256="deadbeef")


def test_entry_rejects_empty_upstream_list() -> None:
    with pytest.raises(ValidationError):
        _entry(upstream=[])


def test_catalog_rejects_duplicate_product_version() -> None:
    with pytest.raises(ValidationError, match="duplicate catalog entry"):
        ConnectorSpecCatalog(entries=(_entry(), _entry()))


def test_parse_catalog_rejects_non_mapping() -> None:
    with pytest.raises(CatalogError, match="mapping with an 'entries' key"):
        parse_catalog("- just\n- a\n- list\n")


def test_parse_catalog_rejects_malformed_yaml() -> None:
    with pytest.raises(CatalogError, match="not valid YAML"):
        parse_catalog("entries: [unterminated\n")


def test_parse_catalog_rejects_unknown_top_level_field() -> None:
    with pytest.raises(CatalogError, match="schema validation"):
        parse_catalog("entries: []\nbogus: 1\n")


# ---------------------------------------------------------------------------
# GET /api/v1/connectors/catalog route (consumed by the #915 CLI verbs)
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(connectors_ingest_router)
    return app


def test_catalog_route_is_mounted() -> None:
    paths = {route.path for route in _build_app().routes}  # type: ignore[attr-defined]
    assert "/api/v1/connectors/catalog" in paths


def test_catalog_unauthenticated_returns_401() -> None:
    client = TestClient(_build_app())
    response = client.get("/api/v1/connectors/catalog")
    assert response.status_code == 401


async def test_catalog_endpoint_returns_all_entries() -> None:
    # The handler ignores the operator (it returns global reference data);
    # require_role already gates auth, exercised by the 401 test above.
    body = await catalog_endpoint(operator=MagicMock())
    assert set(body) == {"catalog"}
    returned = {(e["product"], e["version"]) for e in body["catalog"]}
    assert returned == _EXPECTED_PRODUCT_VERSION
