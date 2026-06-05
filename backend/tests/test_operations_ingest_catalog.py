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
    info_version_matches_compatibility,
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
    # #1530: NSX-T 4.x was renumbered onto the VCF train at VCF 9.0;
    # the catalog row tracks the VCF-9-aligned "9.0" line (NsxConnector
    # covers both 4.x and 9.x via supported_version_range ">=4.0,<10.0").
    ("nsx", "9.0"),
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


def test_catalog_product_field_matches_target_create_enum(
    _registered_connectors: set[str],
) -> None:
    """Catalog ``product`` aligns with the registered-product enum (Finding B).

    G0.16-T6 Finding B (#1312). Per
    ``docs/codebase/api-shape-conventions.md`` §3 (Enum vocabulary
    discipline), the ``TargetCreate`` enum and the catalog ``product``
    field must hold identical strings for every product they both
    name. RDC #771 Finding 6 caught the v0.7-era ``"sddc"`` vs
    ``"sddc-manager"`` mismatch where the catalog advertised one
    spelling and the connector class registered the other.

    The test guards the convention structurally: every catalog
    entry's ``product`` must be present in the
    ``registered_product_tokens`` set the OpenAPI
    ``TargetCreate.product`` enum is generated from. A future
    drift (typo on a new catalog entry, rename on a connector
    without updating the catalog) trips here at unit-test time
    instead of surfacing as a 422 on the operator's first POST.
    """
    from meho_backplane.connectors.registry import registered_product_tokens

    catalog_products = {entry.product for entry in load_catalog().entries}
    enum_products = set(registered_product_tokens())
    missing = catalog_products - enum_products
    assert missing == set(), (
        f"catalog declares product(s) {missing!r} that are not in the "
        "TargetCreate product enum; reconcile the catalog YAML or the "
        "connector class registration per docs/codebase/"
        "api-shape-conventions.md §3."
    )


# Listing-emitted ``product`` tokens whose registry spelling does NOT
# round-trip through :func:`canonical_product_token` today. Each entry
# is a known split where the v2-registry ``product`` and the
# parser-derived listing token differ AND no
# :data:`~meho_backplane.connectors.registry.PRODUCT_ALIASES` entry
# bridges them yet. G0.18-T2 (#1355) reconciled the SDDC case
# (``sddc`` -> ``sddc-manager``); the other five are adjacent findings
# the structural test below surfaced, recorded here so the same test
# still catches a *new* drift (a future connector that lands with the
# same split shape) while the existing five await targeted follow-up
# Tasks under G0.18 / a successor Initiative.
#
# Each row is ``(listing_token, registry_product)``. Adding a token
# here is an explicit acknowledgement of an operator-visible 422 on
# ``POST /api/v1/targets`` with the listing spelling; removing one
# requires either dropping the alias-or-rename or otherwise
# reconciling the split.
_KNOWN_LISTING_PRODUCT_DRIFT: dict[str, str] = {
    "hetzner": "hetzner-robot",
    "vcfa": "vcf-automation",
    "fleet": "vcf-fleet",
    "vrli": "vcf-logs",
    "vrops": "vcf-operations",
}


def test_listing_product_round_trips_through_target_create_validator(
    _registered_connectors: set[str],
) -> None:
    """Every connector's listing ``product`` is accepted by ``POST /api/v1/targets``.

    G0.18-T2 (#1355) — extends the catalog-↔-enum check above with
    the half RDC #789 Finding 6 caught the hard way: the
    ``meho connector list`` token is the **parser-derived** product
    (what
    :func:`~meho_backplane.operations._lookup.parse_connector_id`
    extracts from the connector_id), not the v2-registry ``product``
    field. For SDDC the registry stores ``"sddc-manager"`` but the
    listing emits ``"sddc"`` (load-bearing for the #773
    connector_id round-trip), so the catalog-↔-enum check alone
    misses the operator-facing split: copying the listing token into
    a create still 422'd.

    The bridge is
    :data:`~meho_backplane.connectors.registry.PRODUCT_ALIASES` +
    :func:`~meho_backplane.connectors.registry.canonical_product_token`.
    This test asserts the round-trip structurally — every shipped
    connector's listing token must canonicalise to a registered
    product token, otherwise the operator's first POST fails. A
    future connector whose listing-emitted product is neither
    canonical nor an alias trips here at unit-test time, not on
    the next dogfood cycle.

    Five existing connectors carry the same split shape SDDC did
    pre-reconciliation (hetzner-robot, vcf-automation, vcf-fleet,
    vcf-logs, vcf-operations); they are recorded in
    :data:`_KNOWN_LISTING_PRODUCT_DRIFT` and excluded from the
    assertion so the SDDC fix can ship without spilling into a
    five-connector audit. The exclusion list IS the audit surface
    — each entry is an acknowledged operator-visible 422 on the
    listing spelling, awaiting its own follow-up task. The test
    still catches a *new* drift outside that allowlist.
    """
    from meho_backplane.connectors.registry import (
        canonical_product_token,
        registered_product_tokens,
    )
    from meho_backplane.operations._lookup import parse_connector_id

    enum_products = set(registered_product_tokens())
    unreachable: list[tuple[str, str]] = []
    for _registry_product, version, impl_id in sorted(all_connectors_v2().keys()):
        if not version or not impl_id:
            # Wildcard / v1-compat (``(product, "", "")``) rows are
            # dropped by ``_resolve_class_only_natural_key`` before
            # they reach the operator-facing listing, so they don't
            # contribute a listing token to round-trip.
            continue
        connector_id = f"{impl_id}-{version}"
        try:
            parsed_product, parsed_version, parsed_impl_id = parse_connector_id(connector_id)
        except ValueError:
            # Parser-incompatible id shape; the listing drops with a
            # structured log line.
            continue
        if (parsed_version, parsed_impl_id) != (version, impl_id):
            # Lossy parse — the dispatcher couldn't recover the
            # registered triple from the rendered connector_id, so
            # the listing drops the row. Out of scope for the
            # round-trip check.
            continue
        if parsed_product in _KNOWN_LISTING_PRODUCT_DRIFT:
            # Acknowledged adjacent finding — same split shape as
            # the SDDC case but outside the scope of #1355. The
            # allowlist entry asserts the operator-visible 422 is
            # known and awaiting its own reconciliation task.
            continue
        canonical = canonical_product_token(parsed_product)
        if canonical not in enum_products:
            unreachable.append((connector_id, parsed_product))
    assert unreachable == [], (
        f"connector(s) emit a listing ``product`` that neither "
        f"matches a registered product token nor canonicalises to "
        f"one via PRODUCT_ALIASES (and is not in the explicit "
        f"_KNOWN_LISTING_PRODUCT_DRIFT allowlist): {unreachable!r}. "
        f"An operator copying this token into POST /api/v1/targets "
        f"will hit a 422. Either rename the connector class so "
        f"registry and parser agree, add a PRODUCT_ALIASES entry "
        f"per docs/codebase/api-shape-conventions.md §3, or — if "
        f"the split is intentional and the fix is scoped to a "
        f"separate task — add a _KNOWN_LISTING_PRODUCT_DRIFT entry."
    )


def test_known_listing_product_drift_entries_still_drift(
    _registered_connectors: set[str],
) -> None:
    """Every allowlist entry still represents a real split — and only one.

    Two invariants:

    * The listing token in :data:`_KNOWN_LISTING_PRODUCT_DRIFT`
      really fails to round-trip today (otherwise the entry is
      stale and should be deleted — the connector got fixed). A
      stale allowlist erodes the structural-drift signal of the
      sibling test above.
    * The recorded registry spelling matches the live v2 registry
      (otherwise a connector rename would invalidate the allowlist
      without anyone noticing). Pinning both halves catches a
      rename that "fixes" the drift in one direction without
      removing the allowlist row.
    """
    from meho_backplane.connectors.registry import (
        canonical_product_token,
        registered_product_tokens,
    )

    enum_products = set(registered_product_tokens())
    stale: list[str] = []
    misrecorded: list[tuple[str, str, str]] = []
    for listing_token, recorded_registry in _KNOWN_LISTING_PRODUCT_DRIFT.items():
        canonical = canonical_product_token(listing_token)
        if canonical in enum_products:
            stale.append(listing_token)
            continue
        if recorded_registry not in enum_products:
            misrecorded.append(
                (listing_token, recorded_registry, "registry spelling not registered")
            )
    assert stale == [], (
        f"_KNOWN_LISTING_PRODUCT_DRIFT has stale entries {stale!r} that "
        "now round-trip — the underlying connector was reconciled. "
        "Remove the allowlist row."
    )
    assert misrecorded == [], (
        f"_KNOWN_LISTING_PRODUCT_DRIFT misrecords registry spellings: "
        f"{misrecorded!r}. Update the allowlist value to the live "
        "registry product."
    )


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
# (d) spec_info_versions_compatible — G0.16-T6 Finding H (#1312)
# ---------------------------------------------------------------------------


def test_entry_accepts_wildcard_compatibility_specifier() -> None:
    entry = _entry(spec_info_versions_compatible=["9.0.x"])
    assert entry.spec_info_versions_compatible == ("9.0.x",)


def test_entry_accepts_pep440_specifier_set_compatibility_specifier() -> None:
    # Under T5 (#1307)'s strict-superset grammar, PEP 440 specifier sets
    # are accepted alongside glob shapes (any-of semantics across patterns).
    entry = _entry(spec_info_versions_compatible=[">=9.0,<10.0"])
    assert entry.spec_info_versions_compatible == (">=9.0,<10.0",)


def test_entry_rejects_bad_wildcard_prefix() -> None:
    with pytest.raises(ValidationError, match=r"neither a glob.*nor a valid PEP 440 specifier set"):
        _entry(spec_info_versions_compatible=["not-a-version.x"])


def test_entry_rejects_lone_dotx_specifier() -> None:
    with pytest.raises(ValidationError, match=r"neither a glob.*nor a valid PEP 440 specifier set"):
        _entry(spec_info_versions_compatible=[".x"])


def test_entry_rejects_empty_compatibility_list() -> None:
    with pytest.raises(ValidationError, match="non-empty list"):
        _entry(spec_info_versions_compatible=[])


def test_spec_info_version_matches_wildcard_specifier() -> None:
    """The release-tuple-prefix wildcard match (G0.16-T6 Finding H #1312).

    Per ``docs/codebase/api-shape-conventions.md`` §9 the wildcard
    matches anything whose release-tuple starts with the prefix
    before ``.x``. ``"9.0.x"`` accepts ``9.0``, ``9.0.0``,
    ``9.0.0.0``, ``9.0.3``; rejects ``9.1.0`` and ``8.0.0.0``.
    """
    from meho_backplane.operations.ingest.catalog import (
        spec_info_version_matches_compatibility_specifier as match,
    )

    # Wildcard matches.
    assert match("9.0", "9.0.x")
    assert match("9.0.0", "9.0.x")
    assert match("9.0.0.0", "9.0.x")
    assert match("9.0.3", "9.0.x")
    # Wildcard rejects.
    assert not match("9.1.0", "9.0.x")
    assert not match("8.0.0.0", "9.0.x")
    # Bare specifier exact / prefix.
    assert match("9.0.0.0", "9.0")
    assert match("9.0.0.0", "9.0.0.0")
    assert not match("9.1.0", "9.0")
    # Non-PEP-440 spec → ``False`` (not an exception; see helper docstring).
    assert not match("not-a-version", "9.0.x")


def test_shipped_vmware_catalog_declares_9_0_x_compatibility() -> None:
    """vmware/9.0 catalog entry carries ``spec_info_versions_compatible=("9.0.x",)``.

    G0.16-T6 Finding H (#1312). vSphere ships ``info.version="9.0.0.0"``
    while the catalog labels the entry ``version="9.0"``; the
    PEP-440 prefix-match already treats those as "exact" but the
    explicit declaration documents the divergence per
    ``docs/codebase/api-shape-conventions.md`` §9 and pairs with
    T5 (#1307)'s gh-rest entry where the divergence is
    load-bearing.
    """
    vmware = next(
        entry
        for entry in load_catalog().entries
        if (entry.product, entry.version) == ("vmware", "9.0")
    )
    assert vmware.spec_info_versions_compatible == ("9.0.x",)


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


# ---------------------------------------------------------------------------
# spec_info_versions_compatible (G0.16-T5 #1307 label-vs-spec decoupling opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "matching", "non_matching"),
    [
        # Glob: one fixed major, two wildcards.
        ("1.x.x", ["1.0", "1.1.4", "1.99.99"], ["0.9", "2.0", "2.1.0"]),
        # Glob: one fixed major, one wildcard.
        ("9.x", ["9.0", "9.99"], ["8.99", "10.0"]),
        # Glob: fixed major + minor, one wildcard.
        ("9.0.x", ["9.0", "9.0.0.0", "9.0.99"], ["9.1", "9.1.0"]),
        # PEP 440 specifier set passes through verbatim.
        (">=1.0,<2.0", ["1.0", "1.99"], ["0.9", "2.0"]),
        # PEP 440 compatible-release operator.
        ("~=1.4", ["1.4", "1.4.0", "1.99"], ["1.3.99", "2.0"]),
    ],
)
def test_compatibility_glob_and_specifier_shapes_round_trip(
    pattern: str, matching: list[str], non_matching: list[str]
) -> None:
    """Both glob and SpecifierSet entries match expected versions."""
    entry = _entry(spec_info_versions_compatible=[pattern])
    assert entry.spec_info_versions_compatible == (pattern,)
    for v in matching:
        assert info_version_matches_compatibility(v, (pattern,)), f"{v!r} should match {pattern!r}"
    for v in non_matching:
        assert not info_version_matches_compatibility(v, (pattern,)), (
            f"{v!r} should not match {pattern!r}"
        )


def test_compatibility_match_handles_multiple_patterns() -> None:
    """Any-of semantics: a version matches the band if any pattern accepts it."""
    patterns = ("1.x.x", "2.0.x")
    assert info_version_matches_compatibility("1.5.0", patterns)
    assert info_version_matches_compatibility("2.0.99", patterns)
    assert not info_version_matches_compatibility("2.1.0", patterns)


def test_compatibility_match_returns_false_for_non_pep440_version() -> None:
    """Non-PEP-440 ``info.version`` strings short-circuit to False."""
    assert not info_version_matches_compatibility("acme-2024Q3", ("1.x",))


def test_entry_accepts_no_opt_in() -> None:
    """``spec_info_versions_compatible=None`` is the default (no opt-in)."""
    assert _entry().spec_info_versions_compatible is None


@pytest.mark.parametrize(
    "bad_pattern",
    [
        "x",  # no fixed prefix
        "1.x.y",  # non-x trailing wildcard
        "1.2",  # no wildcard at all
        "not-a-version",  # neither glob nor PEP 440
        "",  # blank
    ],
)
def test_entry_rejects_malformed_compatibility_pattern(bad_pattern: str) -> None:
    with pytest.raises(ValidationError):
        _entry(spec_info_versions_compatible=[bad_pattern])


def test_shipped_catalog_gh3_carries_compatibility_opt_in() -> None:
    """The gh/3 row uses the new field to decouple label '3' from info.version '1.x'."""
    catalog = load_catalog()
    gh = catalog.get("gh", "3")
    assert gh is not None
    assert gh.spec_info_versions_compatible == ("1.x.x",)
    # The shipped info.version (1.1.4) must fall inside the declared band.
    assert info_version_matches_compatibility("1.1.4", gh.spec_info_versions_compatible)


def test_shipped_catalog_only_gh_vmware_and_nsx_rows_opt_in() -> None:
    """The opt-in is targeted. ``gh`` opts in to bridge the v3-vs-1.1.4
    divergence (T5 #1307); ``vmware`` opts in as a belt-and-suspenders
    declaration over the existing PEP-440 prefix-match (T6 Finding H
    #1312); ``nsx`` opts in to the ``9.x.x`` band so a VCF-9 appliance
    spec (info.version in the 9.x scheme) clears the spec/label gate
    under the renumbered "9.0" catalog label (#1530). All other rows
    keep ``spec_info_versions_compatible`` null."""
    expected_opt_in = {"gh", "vmware", "nsx"}
    for entry in load_catalog().entries:
        if entry.product in expected_opt_in:
            continue
        assert entry.spec_info_versions_compatible is None, (
            f"{entry.product}/{entry.version} unexpectedly opts in"
        )


# ---------------------------------------------------------------------------
# catalog_ingest classifier (G0.18-T8 #1361 / RDC #789 N8) — declarative
# input the listing's next_step hint reads to avoid over-promising
# --catalog ingest against HTML-portal / fqdn-templated upstreams.
# ---------------------------------------------------------------------------


def test_entry_defaults_to_catalog_ingest_supported() -> None:
    """``catalog_ingest`` defaults to ``"supported"`` for back-compat.

    Pre-G0.18-T8 catalog rows never named the field; their semantics
    were "upstream is fetchable -> --catalog works". The default
    preserves that — only rows that explicitly opt into ``"spec-only"``
    nudge the listing toward the manual-mode verb.
    """
    assert _entry().catalog_ingest == "supported"


def test_entry_accepts_catalog_ingest_spec_only() -> None:
    """An entry can opt into ``"spec-only"`` explicitly."""
    entry = _entry(catalog_ingest="spec-only")
    assert entry.catalog_ingest == "spec-only"


def test_entry_rejects_unknown_catalog_ingest_value() -> None:
    """Pydantic Literal-typing rejects values outside the two-element set."""
    with pytest.raises(ValidationError):
        _entry(catalog_ingest="maybe")  # type: ignore[arg-type]


def test_shipped_catalog_marks_vcf_family_rows_spec_only() -> None:
    """vmware/9.0, sddc-manager/9.0, nsx/4.2 ship ``catalog_ingest: spec-only``.

    G0.18-T8 (#1361, RDC #789 N8). All three upstreams fundamentally
    cannot drive ``meho connector ingest --catalog`` server-side:

    * ``vmware/9.0`` + ``sddc-manager/9.0`` — Broadcom Developer Portal
      ``text/html`` landing pages; the route's existing
      ``catalog_entry_upstream_not_spec`` 422 fires.
    * ``nsx/9.0`` — first upstream is fqdn-templated
      (``<nsx-mgr-fqdn>``); the route's
      ``catalog_entry_templated_upstream`` 422 fires. (Row renumbered
      from ``nsx/4.2`` for the VCF-9 alignment, #1530.)

    Marking these rows ``"spec-only"`` is what lets the listing emit
    an honest ``--spec`` ``next_step`` hint instead of the previous
    "spec available in catalog; run ingest" line that sent operators
    into a 422.
    """
    spec_only_pairs = {("vmware", "9.0"), ("sddc-manager", "9.0"), ("nsx", "9.0")}
    for entry in load_catalog().entries:
        if (entry.product, entry.version) in spec_only_pairs:
            assert entry.catalog_ingest == "spec-only", (
                f"{entry.product}/{entry.version} should be catalog_ingest: spec-only "
                "because its upstream is HTML-portal or fqdn-templated"
            )
        else:
            assert entry.catalog_ingest == "supported", (
                f"{entry.product}/{entry.version} should default to catalog_ingest: supported"
            )
