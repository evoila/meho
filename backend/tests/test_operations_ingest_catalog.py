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

# The eight catalog entries currently shipped. The seven v0.3.0
# connectors plus ``gh/3`` (G3.11-T3 #1223 -- the GitHub REST API
# entry that consumes the GitHubRestConnector class shipped by
# G3.11-T1 #1221). The ``gh`` row's ``version`` field stores the
# digit-prefix ``"3"`` (G3.11-T8 #1242 reconciled the catalog with
# the registry's parse-friendly form -- the dispatcher's
# parse_connector_id pins version to ``^[0-9][A-Za-z0-9._]*$``);
# the upstream "v3" label lives in the row's ``notes`` and in
# docs/cross-repo/github-connector.md.
_EXPECTED_PRODUCT_VERSION = {
    ("vmware", "9.0"),
    ("sddc-manager", "9.0"),
    ("harbor", "2.x"),
    ("nsx", "4.2"),
    ("gh", "3"),
    ("vault", "1.x"),
    ("k8s", "1.x"),
    ("bind9", "9.x"),
}
_TYPED_PRODUCTS = {"vault", "k8s", "bind9"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the Keycloak + Vault settings the auth dependency reads.

    The unauthenticated-401 route test drives a request through
    ``require_role``, which constructs :class:`Settings` (Keycloak *and*
    Vault fields are required). Every test file pins these (see
    ``conftest.py`` and the sibling ``test_api_v1_connectors_ingest.py``);
    CI runs with a clean env, so the fixture — not the ambient shell —
    must provide them.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")


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
    from meho_backplane.connectors.github import GitHubRestConnector
    from meho_backplane.connectors.harbor import HarborConnector
    from meho_backplane.connectors.kubernetes import KubernetesConnector
    from meho_backplane.connectors.nsx import NsxConnector
    from meho_backplane.connectors.sddc_manager import SddcManagerConnector
    from meho_backplane.connectors.vault import VaultConnector
    from meho_backplane.connectors.vmware_rest import VmwareRestConnector

    for cls in (
        Bind9Connector,
        GitHubRestConnector,
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
# (b') Triple registration — G3.11-T10 #1253 extension
#
# The class-presence check above catches the case where the connector
# class isn't registered at all. The triple-registration check below
# catches the T8 #1242 class of bug: the class IS registered, but under
# a different (product, version, impl_id) key than the catalog row
# names. The shipped catalog is exercised by
# test_validate_catalog_registry_coverage_passes_for_shipped_catalog
# (same fixture covers both axes); the dedicated tests below focus on
# the synthetic-drift envelope shape per the G0.14-T11 #1141 convention.
# ---------------------------------------------------------------------------


def test_validate_catalog_registry_coverage_raises_on_triple_drift(
    _registered_connectors: set[str],
) -> None:
    """The exact T8 #1242 shape: catalog ``v3`` vs registry ``3``.

    ``GitHubRestConnector`` is registered (by ``_registered_connectors``)
    under the triple ``("gh", "3", "gh-rest")`` — the class-presence
    check passes — but the synthetic catalog row names the pre-T8
    drifted ``("gh", "v3", "gh-rest")`` triple, which is NOT in the
    registry. The validator must raise ``CatalogError`` with the
    ``catalog_registry_triple_mismatch:`` code prefix and the
    closest-same-product hint.
    """
    drifted = ConnectorSpecCatalog(
        entries=(
            _entry(
                product="gh",
                version="v3",  # drifted from registry's "3"
                impl_id="gh-rest",
                requires_connector_class="GitHubRestConnector",
            ),
        ),
    )
    with pytest.raises(CatalogError) as exc_info:
        validate_catalog_registry_coverage(drifted)
    msg = str(exc_info.value)
    assert msg.startswith("catalog_registry_triple_mismatch:"), msg
    # (a) The catalog triple is named verbatim.
    assert "product='gh'" in msg and "version='v3'" in msg and "impl_id='gh-rest'" in msg
    # (b) The closest registered triple for the same product is the
    # canonical post-T8 form — `version='3'` (no leading 'v').
    assert "closest registered triple" in msg
    assert "version='3'" in msg
    # (c) The remediation imperative names both source-of-truth files
    # and points at the error-message-shape doc.
    assert "catalog.yaml" in msg
    assert "register_connector_v2" in msg
    assert "error-message-shape.md" in msg


def test_validate_catalog_registry_coverage_drift_hint_falls_back_to_global(
    _registered_connectors: set[str],
) -> None:
    """Product slug typo'd → hint falls back to the closest GLOBAL triple.

    When the catalog row's ``product`` doesn't match ANY registered
    triple, the same-product hint logic has nothing to compare against;
    the validator should still produce a useful hint by matching the
    full ``product|version|impl_id`` string against every registered
    triple. The shipped catalog has ``("k8s", "1.x", "k8s")`` registered,
    so a row claiming ``("k8z", "1.x", "k8s")`` (one-character product
    typo) should surface ``k8s`` as the closest hint.
    """
    typo = ConnectorSpecCatalog(
        entries=(
            _entry(
                product="k8z",
                version="1.x",
                impl_id="k8s",
                requires_connector_class="KubernetesConnector",
            ),
        ),
    )
    with pytest.raises(CatalogError) as exc_info:
        validate_catalog_registry_coverage(typo)
    msg = str(exc_info.value)
    assert "catalog_registry_triple_mismatch:" in msg
    assert "product='k8z'" in msg
    # Hint resolves to the closest globally — k8s with version 1.x.
    assert "product='k8s'" in msg
    assert "version='1.x'" in msg


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


def test_entry_strips_identifier_whitespace() -> None:
    entry = _entry(product="  vmware  ", impl_id=" vmware-rest ")
    assert entry.product == "vmware"
    assert entry.impl_id == "vmware-rest"


def test_entry_rejects_whitespace_only_identifier() -> None:
    with pytest.raises(ValidationError, match="blank or whitespace-only"):
        _entry(version="   ")


def test_entry_strips_upstream_urls() -> None:
    entry = _entry(upstream=["  https://example.test/spec.yaml  "])
    assert entry.upstream == ("https://example.test/spec.yaml",)


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
    # response_model=CatalogListResponse → the handler returns the typed
    # model (not a dict).
    response = await catalog_endpoint(operator=MagicMock())
    returned = {(e.product, e.version) for e in response.catalog}
    assert returned == _EXPECTED_PRODUCT_VERSION
