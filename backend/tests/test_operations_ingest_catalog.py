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
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from packaging.version import Version
from pydantic import ValidationError
from sqlalchemy import select

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
    PROFILE_RESOURCE_PACKAGE,
    SPEC_RESOURCE_PACKAGE,
    CatalogError,
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    info_version_matches_compatibility,
    load_catalog,
    load_profile_resource,
    load_spec_resource,
    parse_catalog,
    validate_catalog_registry_coverage,
    validate_shipped_artifacts,
)

# The nine catalog entries currently shipped. The seven v0.3.0
# connectors plus ``gh/3`` (G3.11-T3 #1223 -- the GitHub REST API
# entry that consumes the GitHubRestConnector class shipped by
# G3.11-T1 #1221) plus the ``_fixture/1.0`` profile-backed shipped-spec
# mechanism row (#1964 T1 #1975 -- exercises the spec_resource /
# profile_resource on-ramp + the boot-time dry-run-parse validator).
# The ``gh`` row's ``version`` field stores the
# digit-prefix ``"3"`` (G3.11-T8 #1242 reconciled the catalog with
# the registry's parse-friendly form -- the dispatcher's
# parse_connector_id pins version to ``^[0-9][A-Za-z0-9._]*$``);
# the upstream "v3" label lives in the row's ``notes`` and in
# docs/cross-repo/github-connector.md.
_EXPECTED_PRODUCT_VERSION = {
    ("vmware", "9.0"),
    ("sddc", "9.0"),
    ("harbor", "2.x"),
    # #1530: NSX-T 4.x was renumbered onto the VCF train at VCF 9.0;
    # the catalog row tracks the VCF-9-aligned "9.0" line (NsxConnector
    # covers both 4.x and 9.x via supported_version_range ">=4.0,<10.0").
    ("nsx", "9.0"),
    ("gh", "3"),
    ("vault", "1.x"),
    ("k8s", "1.x"),
    ("bind9", "9.x"),
    # #1964 T1 #1975: profile-backed shipped-spec mechanism fixture.
    ("_fixture", "1.0"),
}
_TYPED_PRODUCTS = {"vault", "k8s", "bind9"}
# Products whose catalog row carries neither an ``upstream`` nor is a
# hand-coded typed connector: the profile-backed shipped-spec rows whose
# spec ships as package data via ``spec_resource``. The ``_fixture/1.0``
# mechanism row landed with #1975; #1964 T2 (#1976) added the real
# ``vmware/9.0`` + ``sddc/9.0`` rows whose Broadcom upstream the backend
# can't dereference, so their MEHO-authored minimal specs + reviewed
# ExecutionProfiles ship as package data instead of forcing a --spec upload.
_SHIPPED_SPEC_PRODUCTS = {"_fixture", "vmware", "sddc"}


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
    """Typed connectors carry ``upstream: null``; generic ones carry URLs.

    Profile-backed shipped-spec rows (#1975) are a third shape: null
    ``upstream`` (the spec ships as package data, not fetched) but a
    ``spec_resource`` instead of a hand-coded connector, so they're
    exempt from both branches.
    """
    for entry in load_catalog().entries:
        if entry.product in _SHIPPED_SPEC_PRODUCTS:
            assert entry.upstream is None, f"{entry.product} ships its spec (null upstream)"
            assert entry.spec_resource, f"{entry.product} needs a shipped spec_resource"
        elif entry.product in _TYPED_PRODUCTS:
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
        if entry.profile_resource is not None:
            # Profile-backed row (#1975): requires_connector_class names a
            # synthesised ProfiledRestConnector subclass materialised from
            # the profile (T5 #1971), which need not pre-exist in the
            # registry. The validator exempts these rows; the test mirrors
            # that exemption.
            continue
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

    # Profile-backed rows (#1975) name a product whose ProfiledRestConnector
    # subclass is registered when the profile is materialised (T5 #1971),
    # not at catalog-parse time, so their product need not yet be in the
    # TargetCreate enum. Exempt them from the catalog-↔-enum alignment.
    catalog_products = {
        entry.product for entry in load_catalog().entries if entry.profile_resource is None
    }
    enum_products = set(registered_product_tokens())
    missing = catalog_products - enum_products
    assert missing == set(), (
        f"catalog declares product(s) {missing!r} that are not in the "
        "TargetCreate product enum; reconcile the catalog YAML or the "
        "connector class registration per docs/codebase/"
        "api-shape-conventions.md §3."
    )


# Listing-emitted ``product`` tokens whose registered v2 spelling does
# NOT equal the parser-derived listing token. Each entry is a known split
# where the v2-registry ``product`` and the token
# :func:`parse_connector_id` derives from the connector_id differ.
#
# Now empty and structurally enforced so. G0.18-T2 (#1355) reconciled the
# SDDC case via a write-time alias; G0.26-T4 (#1798) reconciled vRLI by
# aligning the connector to ``product="vrli"``; #1814 (Initiative #1810)
# realigned the last four (``hetzner``, ``vcfa``, ``fleet``, ``vrops``) to
# their short, dispatch-canonical registry token and dropped the now-
# redundant ``sddc`` alias; #1816 promoted the registration round-trip
# check to a hard-fail and #1817 retired the write-time alias bridge
# (``PRODUCT_ALIASES`` / ``canonical_product_token``) entirely. Every
# shipped connector's listing token now *equals* its registered product —
# there is no alias hop left — so the allowlist is empty and the
# structural test below catches a *new* divergence directly.
#
# Each row is ``(listing_token, registry_product)``. Adding a token here
# would acknowledge an operator-visible 422 on ``POST /api/v1/targets``
# with the listing spelling; with the alias bridge gone the only fix for
# a real split is to rename the connector registration so it round-trips.
_KNOWN_LISTING_PRODUCT_DRIFT: dict[str, str] = {}


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

    The bridge for that split was once
    :data:`~meho_backplane.connectors.registry.PRODUCT_ALIASES` +
    ``canonical_product_token``; #1814 (Initiative #1810) realigned the
    connectors and #1817 retired the bridge, so the listing token must now
    **equal** a registered product token directly. This test asserts the
    round-trip structurally — every shipped connector's listing token must
    be a registered product token, otherwise the operator's first POST
    fails. A future connector whose listing-emitted product is not a
    registered token trips here at unit-test time, not on the next dogfood
    cycle.

    The five connectors that once carried the same split shape SDDC
    did pre-reconciliation (hetzner-robot, vcf-automation, vcf-fleet,
    vcf-logs, vcf-operations) have all been realigned to their short,
    dispatch-canonical registry token (vcf-logs by #1798; the other
    four by #1814 / Initiative #1810), so
    :data:`_KNOWN_LISTING_PRODUCT_DRIFT` is now empty — every shipped
    connector's listing token round-trips through the create validator.
    The test still catches a *new* drift outside that (empty) allowlist.
    """
    from meho_backplane.connectors.registry import registered_product_tokens
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
            # Acknowledged adjacent finding — a split outside this
            # task's scope. The allowlist entry asserts the
            # operator-visible 422 is known and awaiting a rename task.
            continue
        if parsed_product not in enum_products:
            unreachable.append((connector_id, parsed_product))
    assert unreachable == [], (
        f"connector(s) emit a listing ``product`` that does not match a "
        f"registered product token (and is not in the explicit "
        f"_KNOWN_LISTING_PRODUCT_DRIFT allowlist): {unreachable!r}. "
        f"An operator copying this token into POST /api/v1/targets "
        f"will hit a 422. The write-time alias bridge was retired by "
        f"#1817, so the fix is to rename the connector registration so "
        f"registry and parser agree — or, if the split is intentional "
        f"and scoped to a separate task, add a "
        f"_KNOWN_LISTING_PRODUCT_DRIFT entry."
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
# (d) Shipped-spec / profile mechanism — #1964 T1 #1975
#
# A catalog row may carry a packaged spec_resource / profile_resource;
# the catalog-driven ingest route loads the spec bytes inline (bypassing
# an un-fetchable upstream), the validator exempts profile-backed rows
# from the class-presence + triple checks, and every shipped artifact is
# dry-run-parsed at startup (boot crashes on a malformed one).
# ---------------------------------------------------------------------------


def test_profile_backed_row_exempt_from_class_presence() -> None:
    """A profile-backed row passes the validator with no registered class.

    The synthesised ProfiledRestConnector subclass (T5 #1971) need not
    exist when the boot-time validator runs, so a row carrying
    ``profile_resource`` must NOT trip the unregistered-class assertion
    even against an empty registry.
    """
    cat = ConnectorSpecCatalog(
        entries=(
            _entry(
                product="prof",
                version="1.0",
                impl_id="prof-rest",
                requires_connector_class="ProfiledRestConnector_prof_1_0",
                profile_resource="some_profile.yaml",
            ),
        ),
    )
    # Must not raise even though the class / triple are unregistered.
    validate_catalog_registry_coverage(cat)


def test_non_profile_row_still_enforces_class_presence() -> None:
    """The exemption is profile-gated: a plain row still fails on a bogus class."""
    cat = ConnectorSpecCatalog(
        entries=(_entry(requires_connector_class="NoSuchConnector999"),),
    )
    with pytest.raises(CatalogError, match="unregistered connector class"):
        validate_catalog_registry_coverage(cat)


def test_resource_name_rejects_path_traversal() -> None:
    """spec_resource / profile_resource must be a single safe segment."""
    for bad in ("../escape.yaml", "sub/dir.yaml", "a\\b.yaml", "  "):
        with pytest.raises(ValidationError):
            _entry(spec_resource=bad)
        with pytest.raises(ValidationError):
            _entry(profile_resource=bad)


def test_load_spec_resource_reads_shipped_fixture() -> None:
    """The shipped fixture spec resolves to its package-data text."""
    text = load_spec_resource("_fixture_minimal.yaml")
    assert "openapi:" in text
    assert "/things" in text


def test_load_profile_resource_reads_shipped_fixture() -> None:
    text = load_profile_resource("_fixture_minimal.yaml")
    assert "scheme: basic" in text


def test_load_spec_resource_missing_raises_catalog_error() -> None:
    with pytest.raises(CatalogError, match="not found under"):
        load_spec_resource("definitely_not_a_real_spec.yaml")


def test_resource_packages_are_importable() -> None:
    """The shipped resource packages (in-package data) exist and are addressable."""
    from importlib.resources import files

    assert files(SPEC_RESOURCE_PACKAGE).joinpath("_fixture_minimal.yaml").is_file()
    assert files(PROFILE_RESOURCE_PACKAGE).joinpath("_fixture_minimal.yaml").is_file()


def test_validate_shipped_artifacts_passes_for_shipped_catalog() -> None:
    """Every shipped spec/profile dry-run-parses cleanly at boot."""
    validate_shipped_artifacts()  # must not raise


# ---------------------------------------------------------------------------
# (#1964 T2 #1976) The real vmware/sddc shipped specs + profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "spec_resource", "profile_resource", "expected_scheme", "expected_ops"),
    [
        (
            "vmware",
            "vmware_rest_minimal.yaml",
            "vmware_rest_minimal.yaml",
            "session_login_basic",
            9,
        ),
        ("sddc", "sddc_manager_minimal.yaml", "sddc_manager_minimal.yaml", "basic", 9),
    ],
)
def test_shipped_vmware_sddc_rows_are_profile_backed(
    product: str,
    spec_resource: str,
    profile_resource: str,
    expected_scheme: str,
    expected_ops: int,
) -> None:
    """The real vmware/sddc rows ship a spec + profile and retire the upstream.

    #1964 T2 (#1976): each row carries a null ``upstream`` (the Broadcom
    portal is HTML the backend can't dereference), names the MEHO-authored
    ``spec_resource`` + ``profile_resource``, and advertises
    ``catalog_ingest: supported`` because the forced-upload friction is gone.
    """
    entry = load_catalog().get(product, "9.0")
    assert entry is not None
    assert entry.upstream is None
    assert entry.spec_resource == spec_resource
    assert entry.profile_resource == profile_resource
    assert entry.catalog_ingest == "supported"


@pytest.mark.parametrize(
    ("spec_resource", "expected_ops", "needle_path"),
    [
        ("vmware_rest_minimal.yaml", 9, "GET:/api/vcenter/vm"),
        ("sddc_manager_minimal.yaml", 9, "GET:/v1/sddc-managers"),
    ],
)
def test_shipped_vmware_sddc_specs_parse_with_the_ingest_parser(
    spec_resource: str,
    expected_ops: int,
    needle_path: str,
) -> None:
    """Each shipped spec parses under the SAME parser the live ingest uses.

    Asserts the boot-validator contract directly: OpenAPI 3.x, self-contained,
    local-``$ref`` only, every op carrying a ``METHOD:path`` op_id.
    """
    from meho_backplane.operations.ingest.openapi import parse_openapi

    content = load_spec_resource(spec_resource)
    rows = parse_openapi(
        f"spec:{spec_resource}",
        spec_source=f"spec:{spec_resource}",
        content=content,
    )
    assert len(rows) == expected_ops
    assert all(row.method == "GET" for row in rows)
    assert needle_path in {row.op_id for row in rows}


@pytest.mark.parametrize(
    ("profile_resource", "expected_scheme"),
    [
        ("vmware_rest_minimal.yaml", "session_login_basic"),
        ("sddc_manager_minimal.yaml", "basic"),
    ],
)
def test_shipped_vmware_sddc_profiles_validate_with_named_scheme(
    profile_resource: str,
    expected_scheme: str,
) -> None:
    """Each shipped profile validates against the closed named-auth catalog."""
    import yaml

    from meho_backplane.connectors.profile import (
        ExecutionProfile,
        validate_execution_profile,
    )

    raw = yaml.safe_load(load_profile_resource(profile_resource))
    profile = ExecutionProfile.model_validate(raw)
    validate_execution_profile(profile)  # must not raise
    assert profile.auth.scheme == expected_scheme
    # The declarative fingerprint version_key must be a literal top-level key
    # (no dotted paths / array indexing — #1177); model_validate already
    # enforced it, this is the regression anchor for the shipped artifact.
    assert "." not in profile.fingerprint.version_key


def test_shipped_vmware_sddc_resources_are_addressable() -> None:
    """The real specs/profiles resolve as in-package wheel data (artifacts)."""
    from importlib.resources import files

    for name in ("vmware_rest_minimal.yaml", "sddc_manager_minimal.yaml"):
        assert files(SPEC_RESOURCE_PACKAGE).joinpath(name).is_file()
        assert files(PROFILE_RESOURCE_PACKAGE).joinpath(name).is_file()


def test_validate_shipped_artifacts_crashes_on_missing_spec() -> None:
    cat = ConnectorSpecCatalog(
        entries=(
            _entry(
                product="prof",
                version="1.0",
                impl_id="prof-rest",
                requires_connector_class="ProfiledRestConnector_prof",
                spec_resource="missing_spec.yaml",
            ),
        ),
    )
    with pytest.raises(CatalogError, match="not found under"):
        validate_shipped_artifacts(cat)


def test_validate_shipped_artifacts_crashes_on_malformed_spec(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shipped spec that parses as YAML but isn't a valid OpenAPI doc crashes boot.

    Patches ``load_spec_resource`` to return a YAML document with no
    ``paths`` key — well-formed YAML, invalid OpenAPI — and asserts the
    boot guard surfaces it via the real ``parse_openapi`` (not a cheap
    well-formedness check), wrapped in a ``CatalogError``.
    """
    import meho_backplane.operations.ingest.catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "load_spec_resource",
        lambda _name: "openapi: '3.1.0'\ninfo:\n  title: bad\n  version: '1.0'\n",
    )
    cat = ConnectorSpecCatalog(
        entries=(
            _entry(
                product="prof",
                version="1.0",
                impl_id="prof-rest",
                requires_connector_class="ProfiledRestConnector_prof",
                spec_resource="_fixture_minimal.yaml",
            ),
        ),
    )
    with pytest.raises(CatalogError, match="failed dry-run parse"):
        validate_shipped_artifacts(cat)


def test_validate_shipped_artifacts_crashes_on_malformed_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shipped profile naming a reserved/unknown auth scheme crashes boot."""
    import meho_backplane.operations.ingest.catalog as catalog_mod

    bad_profile = (
        "product: prof\n"
        "version: '1.0'\n"
        "auth:\n"
        "  scheme: not_a_real_scheme\n"
        "  secret_fields: [token]\n"
        "fingerprint:\n"
        "  path: /v\n"
        "  version_key: version\n"
        "probe: delegate\n"
        "pagination:\n"
        "  strategy: none\n"
        "  items_key: items\n"
    )
    monkeypatch.setattr(catalog_mod, "load_profile_resource", lambda _name: bad_profile)
    cat = ConnectorSpecCatalog(
        entries=(
            _entry(
                product="prof",
                version="1.0",
                impl_id="prof-rest",
                requires_connector_class="ProfiledRestConnector_prof",
                profile_resource="_fixture_minimal.yaml",
            ),
        ),
    )
    with pytest.raises(CatalogError, match="failed dry-run parse"):
        validate_shipped_artifacts(cat)


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
    """nsx/9.0 ships ``catalog_ingest: spec-only``; vmware/sddc no longer do.

    G0.18-T8 (#1361, RDC #789 N8) originally marked all three VCF-family
    rows ``spec-only`` because none could drive ``meho connector ingest
    --catalog`` server-side. #1964 T2 (#1976) changed that for two of them:

    * ``vmware/9.0`` + ``sddc/9.0`` — now PROFILE-BACKED. Their MEHO-
      authored minimal specs ship as package data (``spec_resource``),
      so catalog-driven ingest loads the bytes inline (no fetch, no
      ``catalog_entry_upstream_not_spec`` 422) and the row advertises
      ``catalog_ingest: supported``. The forced ``--spec`` upload friction
      is gone.
    * ``nsx/9.0`` — STILL ``spec-only``: its first upstream is
      fqdn-templated (``<nsx-mgr-fqdn>``) and no shipped spec exists yet,
      so the route's ``catalog_entry_templated_upstream`` 422 fires and
      the listing emits the honest ``--spec`` ``next_step`` hint. (Row
      renumbered from ``nsx/4.2`` for the VCF-9 alignment, #1530.)
    """
    spec_only_pairs = {("nsx", "9.0")}
    for entry in load_catalog().entries:
        if (entry.product, entry.version) in spec_only_pairs:
            assert entry.catalog_ingest == "spec-only", (
                f"{entry.product}/{entry.version} should be catalog_ingest: spec-only "
                "because its upstream is fqdn-templated and no spec ships yet"
            )
        else:
            assert entry.catalog_ingest == "supported", (
                f"{entry.product}/{entry.version} should be catalog_ingest: supported"
            )


# ---------------------------------------------------------------------------
# next_step.verb round-trips to a dispatchable ingest (claude-rdc-hetzner-dc#1136)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_next_step_verb_round_trips_to_dispatchable_ingest() -> None:
    """The ``vcfa`` registered-row ``next_step.verb`` ingests dispatchably.

    The claude-rdc-hetzner-dc#1136 false-success was: an operator copying
    the verb ingested under a product the dispatcher never queried, so the
    catalog kept reporting ``registered, 0 ops``. #1814 (Initiative #1810)
    closed the underlying split by realigning ``VcfAutomationConnector`` to
    register under the short, dispatch-canonical ``product="vcfa"`` (vRLI
    was aligned earlier by G0.26-T4 #1798), and #1817 retired the
    register-time row reconciliation now that nothing diverges. This test
    pins the verb round-trip end-to-end against the realigned connector:

    1. The verb for the ``vcfa`` registered row emits the **registry**
       ``--product`` (``vcfa``), which equals the parser-derived listing
       product (it round-trips its connector_id).
    2. Ingesting under exactly that ``--product`` persists rows under
       ``vcfa`` and yields a connector the dispatch/query surface resolves
       (``connector_exists`` True) — no reconciliation hop needed.
    """
    import re
    from unittest.mock import AsyncMock

    from meho_backplane.connectors.registry import _eager_import_connectors
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations._lookup import connector_exists, parse_connector_id
    from meho_backplane.operations.ingest import register_ingested_operations
    from meho_backplane.operations.ingest.list_connectors import (
        _load_catalog_or_none,
        _maybe_build_class_only_item,
    )
    from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

    # Real connectors registered (the session-level baseline import is a
    # no-op when already populated; defensive against collection order).
    _eager_import_connectors()

    item = _maybe_build_class_only_item(
        registry_product="vcfa",
        registry_version="9.0",
        registry_impl_id="vcfa-rest",
        db_triples=set(),
        catalog=_load_catalog_or_none(),
    )
    assert item is not None
    assert item.state == "registered"
    # Post-#1814 the registry product equals the parser-derived listing
    # product — both are the short, dispatch-canonical ``vcfa``.
    assert item.product == "vcfa"
    assert item.next_step is not None
    verb = item.next_step.verb
    match = re.search(r"--product (\S+)", verb)
    assert match is not None, f"verb has no --product flag: {verb!r}"
    verb_product = match.group(1)
    assert verb_product == "vcfa", (
        f"next_step.verb emits --product {verb_product!r}; expected the "
        f"registry product 'vcfa' (the realigned, round-tripping token). "
        f"Full verb: {verb!r}"
    )

    # Round-trip: ingest under the verb's --product and assert the rows
    # persist under the dispatch-canonical key and dispatch.
    stub = AsyncMock()
    stub.encode_one.return_value = [0.25] * 384
    stub.encode.return_value = [[0.25] * 384]
    stub.dimension = 384
    result = await register_ingested_operations(
        product=verb_product,  # exactly what the verb told the operator to use
        version="9.0",
        impl_id="vcfa-rest",
        spec_source="vcfa.yaml",
        operations=[
            EndpointDescriptorProto(
                op_id="GET:/iaas/api/about",
                method="GET",
                path="/iaas/api/about",
                summary="about",
                description="appliance version",
                tags=["vcfa"],
                parameter_schema={"type": "object", "properties": {}},
                response_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
            )
        ],
        embedding_service=stub,
    )
    assert result.inserted_count == 1

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (await fresh.execute(select(EndpointDescriptor))).scalars().all()
    assert rows and rows[0].product == "vcfa"

    probe_product, probe_version, probe_impl_id = parse_connector_id(item.connector_id)
    exists = await connector_exists(
        tenant_id=uuid.uuid4(),
        product=probe_product,
        version=probe_version,
        impl_id=probe_impl_id,
    )
    assert exists is True, "verb round-trip did not yield a dispatchable connector"
