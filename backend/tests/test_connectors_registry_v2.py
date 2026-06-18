# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the v2 connector registry (G0.6-T2 #393).

Covers the keyword-only ``register_connector_v2`` entry point keyed on
``(product, version, impl_id)``, the diagnostic ``list_connector_impls``
+ ``all_connectors_v2`` snapshots, and the backward-compat bridge that
makes ``register_connector`` (v1) populate **both** registry layers.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.connectors import (
    Connector,
    FingerprintResult,
    OperationResult,
    ProbeResult,
    all_connectors,
    all_connectors_v2,
    list_connector_impls,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.registry import clear_registry, registered_product_tokens

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    product = "fake"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _AnotherFakeConnector(_FakeConnector):
    product = "fake"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _ensure_registered_v2(cls: type[Connector]) -> None:
    """Idempotently register ``cls`` under its v2 triple.

    Bridges the gap between full-suite and subset-isolation runs of the
    ``test_*_connector_registered_under_v2_triple`` cases below.

    Under the full suite, some earlier test imports the connector
    package; the module-level ``register_connector_v2`` side-effect in
    ``connectors/<product>/__init__.py`` fires once and is cached. By
    the time this file's ``_clean_registry`` autouse runs, the package
    module is in ``sys.modules`` so the test's own ``from ... import``
    is a no-op — the registry is genuinely empty after clear, and the
    test's explicit re-register succeeds.

    Under ``pytest -k <one-test>`` (or `pytest <file>::<one-test>`),
    nothing else imports the connector package first. The autouse
    fixture clears the registry, the test's ``from ... import`` then
    triggers the package's first import in this worker process, the
    module-top ``register_connector_v2`` call fires *inside* the test
    (post-clear), and the test's own follow-up ``register_connector_v2``
    raises ``RuntimeError("connector already registered for v2 key …")``.

    Introspecting first and only registering when absent makes both
    paths green without changing connector packages or
    ``register_connector_v2`` semantics. The duplicate-registration
    guard remains correct for genuine programming bugs; this helper
    just signals "ensure the triple is present" instead of "always
    register, fail on duplicate".
    """
    key = (cls.product, cls.version, cls.impl_id)
    if key not in all_connectors_v2():
        register_connector_v2(
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
            cls=cls,
        )


# ---------------------------------------------------------------------------
# register_connector_v2 — happy paths
# ---------------------------------------------------------------------------


def test_register_v2_writes_to_v2_table_only() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeConnector,
    )
    assert all_connectors_v2() == {("vmware", "9.0", "vmware-rest"): _FakeConnector}
    # v1 table stays empty — v2-only entries are not visible via v1 lookup.
    assert all_connectors() == {}


def test_list_connector_impls_returns_sorted_keys() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeConnector,
    )
    register_connector_v2(
        product="vmware",
        version="7.0",
        impl_id="vmware-pyvmomi",
        cls=_AnotherFakeConnector,
    )
    assert list_connector_impls() == [
        ("vmware", "7.0", "vmware-pyvmomi"),
        ("vmware", "9.0", "vmware-rest"),
    ]


def test_register_v2_distinct_impl_ids_coexist_for_same_product_version() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeConnector,
    )
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-alt",
        cls=_AnotherFakeConnector,
    )
    snapshot = all_connectors_v2()
    assert snapshot[("vmware", "9.0", "vmware-rest")] is _FakeConnector
    assert snapshot[("vmware", "9.0", "vmware-alt")] is _AnotherFakeConnector


# ---------------------------------------------------------------------------
# register_connector_v2 — error paths
# ---------------------------------------------------------------------------


def test_register_v2_duplicate_tuple_raises_runtime_error() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeConnector,
    )
    with pytest.raises(RuntimeError, match="already registered for v2 key"):
        register_connector_v2(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            cls=_AnotherFakeConnector,
        )


def test_register_v2_duplicate_error_message_names_both_classes() -> None:
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeConnector,
    )
    with pytest.raises(RuntimeError) as exc_info:
        register_connector_v2(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            cls=_AnotherFakeConnector,
        )
    msg = str(exc_info.value)
    assert "_FakeConnector" in msg
    assert "_AnotherFakeConnector" in msg
    assert "vmware" in msg


def test_register_v2_non_connector_raises_type_error() -> None:
    class NotAConnector:
        pass

    with pytest.raises(TypeError, match="must subclass Connector"):
        register_connector_v2(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            cls=NotAConnector,  # type: ignore[arg-type]
        )


def test_register_v2_keyword_only_arguments() -> None:
    """All four parameters are keyword-only so positional ordering bugs surface immediately."""
    with pytest.raises(TypeError):
        register_connector_v2(  # type: ignore[call-arg,misc]
            "vmware",
            "9.0",
            "vmware-rest",
            _FakeConnector,
        )


# ---------------------------------------------------------------------------
# Product↔impl_id round-trip invariant — G0.26-T4 (#1798), hard-fail G0.27/T2 (#1816)
#
# register_connector_v2 RAISES (RuntimeError) when the declared product
# does not equal the product parse_connector_id derives from the
# connector_id. #1798 introduced this as an advisory WARN (it could not
# raise while the five _PRODUCT_SPLITS connectors still diverged at boot);
# #1814 (Initiative #1810) realigned those five to their short token (and
# #1798 realigned vRLI), so nothing diverges anymore and #1816 promotes
# the check to a hard-fail: a future divergent registration crashes
# _eager_import_connectors at boot instead of silently shadowing the
# connector behind an auto-shim. There is no allowlist.
# ---------------------------------------------------------------------------


def test_register_v2_raises_on_divergent_product() -> None:
    """A registration whose product != parser-derived product RAISES.

    ``product="vcf-logs"`` with ``impl_id="vrli-rest"`` parses to
    ``"vrli"`` — the historical split shape that shadowed VcfLogsConnector
    behind an auto-shim. Post-#1816 this is a hard fail: register_connector_v2
    raises RuntimeError naming the connector, the declared product, and the
    parser-derived product, and the divergent triple is NOT registered.
    """
    with pytest.raises(RuntimeError) as exc_info:
        register_connector_v2(
            product="vcf-logs",
            version="9.0",
            impl_id="vrli-rest",
            cls=_FakeConnector,
        )

    msg = str(exc_info.value)
    # The message names the connector class and both product spellings, and
    # gives the actionable remediation.
    assert "_FakeConnector" in msg
    assert "vcf-logs" in msg
    assert "vrli" in msg
    assert "Align product" in msg

    # The divergent registration was rejected — nothing landed in the table.
    assert ("vcf-logs", "9.0", "vrli-rest") not in all_connectors_v2()


def test_register_v2_aligned_product_registers_without_raising() -> None:
    """A registration whose product round-trips parse_connector_id is accepted.

    ``product="vmware"`` with ``impl_id="vmware-rest"`` parses back to
    ``"vmware"`` — the aligned shape (and the shape vRLI now takes under
    ``product="vrli"`` / ``impl_id="vrli-rest"``). No raise; the triple
    registers.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeConnector,
    )

    assert ("vmware", "9.0", "vmware-rest") in all_connectors_v2()


def test_register_v2_aligned_single_segment_product_registers_without_raising() -> None:
    """A single-segment impl_id (``vault`` / ``vault``) round-trips and is accepted."""
    register_connector_v2(
        product="vault",
        version="1.x",
        impl_id="vault",
        cls=_FakeConnector,
    )

    assert ("vault", "1.x", "vault") in all_connectors_v2()


def test_register_v2_wildcard_row_is_exempt() -> None:
    """The ``(product, "", "")`` wildcard / v1-compat row never raises.

    An empty version/impl_id renders a parser-incompatible connector_id
    with no derived product to compare; the check skips it so the dual
    registration pattern (versioned + wildcard) some connectors use does
    not fail closed on the wildcard leg — even when the wildcard product
    would diverge from a hypothetical parse.
    """
    register_connector_v2(
        product="vcf-logs",
        version="",
        impl_id="",
        cls=_FakeConnector,
    )

    assert ("vcf-logs", "", "") in all_connectors_v2()


def test_register_v2_vrli_alignment_does_not_raise() -> None:
    """The real VcfLogsConnector, aligned to product=\"vrli\", registers cleanly.

    Direct regression pin for the vRLI half of #1798: registering the
    shipped connector under its (now canonical) triple must not raise,
    proving the divergence the hard-fail catches is gone for vRLI.
    """
    from meho_backplane.connectors.vcf_logs import VcfLogsConnector

    register_connector_v2(
        product=VcfLogsConnector.product,
        version=VcfLogsConnector.version,
        impl_id=VcfLogsConnector.impl_id,
        cls=VcfLogsConnector,
    )

    assert VcfLogsConnector.product == "vrli"
    assert (VcfLogsConnector.product, VcfLogsConnector.version, VcfLogsConnector.impl_id) in (
        all_connectors_v2()
    )


def test_realigned_splits_register_without_raising() -> None:
    """The five formerly-split connectors now register WITHOUT raising.

    #1814 (Initiative #1810) realigned sddc-manager → ``sddc``,
    vcf-automation → ``vcfa``, vcf-fleet → ``fleet``, vcf-operations →
    ``vrops`` and hetzner-robot → ``hetzner``, so each connector's
    declared ``product`` now equals the token ``parse_connector_id``
    derives from its ``impl_id``/``version``. Registering each real class
    under its real triple must therefore NOT trip the hard-fail —
    together with vRLI (#1798) the whole hand-coded family round-trips.
    (Before #1814 these five diverged; under #1816's hard-fail a single
    surviving divergence would crash this registration, so this test
    doubles as the proof the realignment is complete.)

    Registering the real classes directly (rather than calling
    :func:`_eager_import_connectors`, which is a ``sys.modules``-cached
    no-op after the session-scoped force-import in conftest) makes the
    assertion robust against import-history coupling.
    """
    from meho_backplane.connectors.hetzner_robot.connector import HetznerRobotConnector
    from meho_backplane.connectors.sddc_manager import SddcManagerConnector
    from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
    from meho_backplane.connectors.vcf_fleet import VcfFleetConnector
    from meho_backplane.connectors.vcf_logs import VcfLogsConnector
    from meho_backplane.connectors.vcf_operations import VcfOperationsConnector

    realigned_classes = [
        SddcManagerConnector,
        VcfAutomationConnector,
        VcfFleetConnector,
        VcfOperationsConnector,
        HetznerRobotConnector,
    ]

    # The short, dispatch-canonical tokens #1814 realigned each class to.
    expected_products = {
        "SddcManagerConnector": "sddc",
        "VcfAutomationConnector": "vcfa",
        "VcfFleetConnector": "fleet",
        "VcfOperationsConnector": "vrops",
        "HetznerRobotConnector": "hetzner",
    }
    for cls in realigned_classes:
        assert cls.product == expected_products[cls.__name__], (
            f"{cls.__name__} should register under {expected_products[cls.__name__]!r}, "
            f"got {cls.product!r}"
        )

    # The hard-fail raises on any divergence, so a clean pass through the
    # whole family is the assertion: every class round-trips.
    for cls in (*realigned_classes, VcfLogsConnector):
        register_connector_v2(
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
            cls=cls,
        )

    # Every class registered.
    snapshot = all_connectors_v2()
    for cls in (*realigned_classes, VcfLogsConnector):
        assert snapshot[(cls.product, cls.version, cls.impl_id)] is cls
    assert VcfLogsConnector.product == "vrli"


def test_eager_import_connectors_boots_clean_with_all_connectors_aligned() -> None:
    """Every shipped connector self-registers via _eager_import_connectors without raising.

    The acceptance pin for the hard-fail: importing every
    ``connectors/<product>/`` subpackage (each of which calls
    ``register_connector`` / ``register_connector_v2`` at module
    top-level) — the same path the chassis lifespan runs at boot — must
    complete without tripping the product↔impl_id hard-fail. With #1814 +
    #1798 having realigned the whole family, none diverges; if ANY
    connector still did, this would raise :exc:`RuntimeError`, which is
    exactly the early-warning the promotion buys.

    To make the assertion exercise the registration side effects
    *deterministically regardless of import history*, the connector
    subpackage modules are evicted from ``sys.modules`` and the registry
    is cleared before the eager import, so every module body re-executes
    its ``register_connector*`` call under the hard-fail in this test —
    rather than being a no-op against the conftest-cached imports. The
    modules are restored afterward so eviction does not leak into other
    tests (autouse ``_clean_registry`` + conftest's
    ``_isolate_global_registries`` restore the registry contents; this
    restores the import cache the eviction touched).
    """
    import sys

    import meho_backplane.connectors as conn_pkg
    from meho_backplane.connectors.registry import _eager_import_connectors

    prefix = f"{conn_pkg.__name__}."
    # Evict the per-connector subpackage modules (not the registry / base /
    # adapters modules) so their top-level register_connector* side effects
    # re-fire on re-import.
    evicted = {
        name: module
        for name, module in list(sys.modules.items())
        if name.startswith(prefix)
        and not name.startswith(
            (
                f"{prefix}registry",
                f"{prefix}base",
                f"{prefix}resolver",
                f"{prefix}schemas",
                f"{prefix}adapters",
            )
        )
    }
    for name in evicted:
        del sys.modules[name]
    clear_registry()
    try:
        # Raises RuntimeError if any connector diverges — a clean return is
        # the assertion. Every shipped connector's module-level registration
        # re-runs here against the freshly-cleared registry.
        _eager_import_connectors()

        # The registry is populated (the lifespan path produced a usable
        # table) and the canonical split-family tokens are present under
        # their short, round-tripping spelling — the realignment that makes
        # the hard-fail safe.
        tokens = registered_product_tokens()
        assert tokens, "expected connectors to self-register at import"
        assert {"sddc", "vcfa", "fleet", "vrops", "hetzner", "vrli"} <= tokens
    finally:
        # Restore the import cache so the eviction is invisible to later tests;
        # the registry contents are restored by the autouse fixtures.
        sys.modules.update(evicted)
        # _eager_import_connectors re-imported the evicted subpackages, creating
        # FRESH module objects that it also bound as child attributes on their
        # parent packages. The update() above restores the ORIGINAL objects into
        # sys.modules, but the parent-package attrs still point at the fresh ones
        # — a desync that makes a later importlib.reload of any of these modules
        # fail with "ImportError: module ... not in sys.modules". Rebind each
        # original onto its parent package to fully undo the re-import.
        for _name, _module in evicted.items():
            _parent_name, _, _child = _name.rpartition(".")
            _parent = sys.modules.get(_parent_name)
            if _parent is not None and _child:
                setattr(_parent, _child, _module)


# ---------------------------------------------------------------------------
# v1 backward-compat bridge — register_connector populates both tables
# ---------------------------------------------------------------------------


def test_v1_register_populates_v2_with_empty_version_impl_id() -> None:
    register_connector("vault", _FakeConnector)
    assert all_connectors() == {"vault": _FakeConnector}
    assert all_connectors_v2() == {("vault", "", ""): _FakeConnector}


def test_v1_register_emits_v1_compat_log_line(capfd: pytest.CaptureFixture[str]) -> None:
    from meho_backplane.logging import configure_logging

    configure_logging()
    register_connector("vault", _FakeConnector)
    out, _ = capfd.readouterr()
    assert "connector_registered_v1_compat" in out
    assert "vault" in out
    assert "register_connector_v2" in out  # the deprecation hint names the migration target


def test_v1_then_v2_with_same_product_works_when_keys_differ() -> None:
    register_connector("vault", _FakeConnector)
    register_connector_v2(
        product="vault",
        version="2.0",
        impl_id="vault-hcp",
        cls=_AnotherFakeConnector,
    )
    snapshot = all_connectors_v2()
    assert snapshot[("vault", "", "")] is _FakeConnector
    assert snapshot[("vault", "2.0", "vault-hcp")] is _AnotherFakeConnector


def test_v1_register_blocks_v2_collision_on_empty_tuple() -> None:
    """A v2 entry at (product, '', '') prevents the v1 bridge from overwriting it.

    Reverse direction (v2 first, then v1) — the v1 path should also fail
    closed because the v2 table already holds (product, '', '').
    """
    register_connector_v2(
        product="vault",
        version="",
        impl_id="",
        cls=_FakeConnector,
    )
    with pytest.raises(RuntimeError, match="already registered for v2 key"):
        register_connector("vault", _AnotherFakeConnector)


def test_clear_registry_clears_both_layers() -> None:
    register_connector("vault", _FakeConnector)
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_AnotherFakeConnector,
    )
    clear_registry()
    assert all_connectors() == {}
    assert all_connectors_v2() == {}
    assert list_connector_impls() == []


def test_all_connectors_v2_returns_copy() -> None:
    register_connector("vault", _FakeConnector)
    snapshot = all_connectors_v2()
    snapshot[("injected", "", "")] = _FakeConnector
    assert ("injected", "", "") not in all_connectors_v2()


# ---------------------------------------------------------------------------
# Shipped connector triples — assert each hand-rolled connector resolves
# ---------------------------------------------------------------------------


def test_sddc_manager_connector_registered_under_v2_triple() -> None:
    """SddcManagerConnector package registers under (sddc, 9.0, sddc-rest) at import.

    Uses :func:`_ensure_registered_v2` so the test passes whether the
    connector package was imported (and self-registered) before
    ``_clean_registry`` cleared the registry, or fresh inside this test
    body under subset isolation. See the helper's docstring for the
    pytest-xdist subset-isolation rationale.
    """
    from meho_backplane.connectors.sddc_manager import SddcManagerConnector

    _ensure_registered_v2(SddcManagerConnector)
    snapshot = all_connectors_v2()
    key = ("sddc", "9.0", "sddc-rest")
    assert key in snapshot
    assert snapshot[key] is SddcManagerConnector


def test_harbor_connector_registered_under_v2_triple() -> None:
    """HarborConnector package registers under (harbor, 2.x, harbor-rest) at import.

    Same idempotent-registration pattern as the SDDC Manager test above.
    """
    from meho_backplane.connectors.harbor import HarborConnector

    _ensure_registered_v2(HarborConnector)
    snapshot = all_connectors_v2()
    key = ("harbor", "2.x", "harbor-rest")
    assert key in snapshot
    assert snapshot[key] is HarborConnector


def test_argocd_connector_registered_under_v2_triple_and_wildcard() -> None:
    """ArgoCdConnector package registers under (argocd, 3.x, argocd-api) + wildcard.

    G3.12-T1 (#1390) ships dual registration from day one per G0.15-T6:
    the versioned triple ``("argocd", "3.x", "argocd-api")`` and the
    wildcard fallback ``("argocd", "", "")`` both resolve to the connector.
    Same idempotent-registration pattern as the Harbor test above; the
    wildcard leg is registered via a second ``_ensure_registered_v2``-style
    guard since the connector class only carries the versioned triple in
    its class attributes.
    """
    from meho_backplane.connectors.argocd import ArgoCdConnector

    _ensure_registered_v2(ArgoCdConnector)
    wildcard = ("argocd", "", "")
    if wildcard not in all_connectors_v2():
        register_connector_v2(product="argocd", version="", impl_id="", cls=ArgoCdConnector)

    snapshot = all_connectors_v2()
    versioned = ("argocd", "3.x", "argocd-api")
    assert snapshot[versioned] is ArgoCdConnector
    assert snapshot[wildcard] is ArgoCdConnector


def test_vcf_automation_connector_registered_under_v2_triple() -> None:
    """VcfAutomationConnector package registers under (vcfa, 9.0, vcfa-rest).

    Same idempotent-registration pattern as the SDDC Manager / Harbor
    tests above.
    """
    from meho_backplane.connectors.vcf_automation import VcfAutomationConnector

    _ensure_registered_v2(VcfAutomationConnector)
    snapshot = all_connectors_v2()
    key = ("vcfa", "9.0", "vcfa-rest")
    assert key in snapshot
    assert snapshot[key] is VcfAutomationConnector


def test_vcf_operations_connector_registered_under_v2_triple() -> None:
    """VcfOperationsConnector package registers under (vrops, 9.0, vrops-rest).

    Same idempotent-registration pattern as the SDDC Manager / Harbor /
    VCF Automation tests above.
    """
    from meho_backplane.connectors.vcf_operations import VcfOperationsConnector

    _ensure_registered_v2(VcfOperationsConnector)
    snapshot = all_connectors_v2()
    key = ("vrops", "9.0", "vrops-rest")
    assert key in snapshot
    assert snapshot[key] is VcfOperationsConnector


def test_vcf_logs_connector_registered_under_v2_triple() -> None:
    """VcfLogsConnector package registers under (vrli, 9.0, vrli-rest).

    G0.26-T4 (#1798) aligned the registry product to the
    dispatch-canonical ``"vrli"`` token (round-trips parse_connector_id),
    retiring the historical ``"vcf-logs"`` split. Same idempotent-
    registration pattern as the SDDC Manager / Harbor / VCF Automation
    tests above.
    """
    from meho_backplane.connectors.vcf_logs import VcfLogsConnector

    _ensure_registered_v2(VcfLogsConnector)
    snapshot = all_connectors_v2()
    key = ("vrli", "9.0", "vrli-rest")
    assert key in snapshot
    assert snapshot[key] is VcfLogsConnector


def test_vcf_fleet_connector_registered_under_v2_triple() -> None:
    """VcfFleetConnector package registers under (fleet, 9.0, fleet-rest).

    Same idempotent-registration pattern as the SDDC Manager / Harbor /
    VCF Automation tests above.
    """
    from meho_backplane.connectors.vcf_fleet import VcfFleetConnector

    _ensure_registered_v2(VcfFleetConnector)
    snapshot = all_connectors_v2()
    key = ("fleet", "9.0", "fleet-rest")
    assert key in snapshot
    assert snapshot[key] is VcfFleetConnector


def test_gcloud_connector_registered_under_v2_triple() -> None:
    """GcloudConnector package registers under (gcloud, 1.0, gcloud-rest) at import.

    Same idempotent-registration pattern as the SDDC Manager / Harbor /
    VCF Fleet tests above.
    """
    from meho_backplane.connectors.gcloud import GcloudConnector

    _ensure_registered_v2(GcloudConnector)
    snapshot = all_connectors_v2()
    key = ("gcloud", "1.0", "gcloud-rest")
    assert key in snapshot
    assert snapshot[key] is GcloudConnector


def test_pfsense_connector_registered_under_v2_triple() -> None:
    """PfSenseConnector package registers under (pfsense, 2.7, pfsense-ssh).

    Same idempotent-registration pattern as the SDDC Manager / Harbor /
    VCF Automation tests above.
    """
    from meho_backplane.connectors.pfsense import PfSenseConnector

    _ensure_registered_v2(PfSenseConnector)
    snapshot = all_connectors_v2()
    key = ("pfsense", "2.7", "pfsense-ssh")
    assert key in snapshot
    assert snapshot[key] is PfSenseConnector


def test_hetzner_robot_connector_registered_under_v2_triple() -> None:
    """HetznerRobotConnector package registers under (hetzner, 2026.04, hetzner-rest).

    Same idempotent-registration pattern as the SDDC Manager / Harbor
    tests above.
    """
    from meho_backplane.connectors.hetzner_robot.connector import HetznerRobotConnector

    _ensure_registered_v2(HetznerRobotConnector)
    snapshot = all_connectors_v2()
    key = ("hetzner", "2026.04", "hetzner-rest")
    assert key in snapshot
    assert snapshot[key] is HetznerRobotConnector


def test_holodeck_connector_registered_under_v2_triple() -> None:
    """HolodeckConnector package registers under (holodeck, 9.0, holodeck-ssh).

    G3.8-T1 (#853) skeleton. Same idempotent-registration pattern as
    the SDDC Manager / Harbor / pfSense tests above.
    """
    from meho_backplane.connectors.holodeck import HolodeckConnector

    _ensure_registered_v2(HolodeckConnector)
    snapshot = all_connectors_v2()
    key = ("holodeck", "9.0", "holodeck-ssh")
    assert key in snapshot
    assert snapshot[key] is HolodeckConnector


def test_keycloak_connector_registered_under_v2_triple() -> None:
    """KeycloakConnector package registers under (keycloak, 26.x, keycloak-admin).

    G3.13-T1 (#1393) substrate. Same idempotent-registration pattern as
    the SDDC Manager / Harbor / pfSense tests above.
    """
    from meho_backplane.connectors.keycloak import KeycloakConnector

    _ensure_registered_v2(KeycloakConnector)
    snapshot = all_connectors_v2()
    key = ("keycloak", "26.x", "keycloak-admin")
    assert key in snapshot
    assert snapshot[key] is KeycloakConnector


# ---------------------------------------------------------------------------
# registered_product_tokens — G0.14-T3 #1144
# ---------------------------------------------------------------------------


def test_registered_product_tokens_returns_empty_set_for_empty_registry() -> None:
    """An empty registry → an empty product-tokens set.

    Pins the source-of-truth invariant: callers (the
    :func:`create_target` validator, the OpenAPI enum hook) can
    distinguish "registry is empty" from "registry is populated but
    no products advertised" via the empty set return. The empty
    state is what tests with isolated registries see; production
    sees a full set after the lifespan's eager-import call.
    """
    assert registered_product_tokens() == set()


def test_registered_product_tokens_returns_product_axis_of_v2_registry() -> None:
    """The token set is the union of v2 ``product`` fields.

    Registering connectors under distinct ``(product, version, impl_id)``
    triples produces one entry per *product* (the version and impl_id
    axes are collapsed). Mirrors the resolver's "valid product"
    judgement at probe / dispatch time.
    """
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_FakeConnector,
    )
    register_connector_v2(
        product="vmware",
        version="7.0",
        impl_id="vmware-pyvmomi",
        cls=_AnotherFakeConnector,
    )
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=_FakeConnector,
    )
    # Two ``vmware`` triples collapse to one entry; ``k8s`` adds a
    # second.
    assert registered_product_tokens() == {"vmware", "k8s"}


def test_registered_product_tokens_includes_v1_compat_entries() -> None:
    """v1 ``register_connector`` registrations show up under their product token.

    The v1 entry point writes a ``(product, "", "")`` row into the v2
    registry; the helper must surface the product token from that
    padded triple just like any v2-native entry. Without this
    contract a deploy that mixes v1- and v2-registered connectors
    would only see the v2 set, and the v1 products would silently
    miss the discoverability enum.
    """
    register_connector(product="vault", cls=_FakeConnector)
    assert registered_product_tokens() == {"vault"}


def test_registered_product_tokens_filters_empty_product_defensively() -> None:
    """An empty ``product`` slug is filtered (defensive).

    Direct ``_REGISTRY_V2`` mutation simulates a hypothetical bug-
    state (the public registrars don't reject empty ``product``
    today but no real connector ships with one). The helper drops
    the empty entry rather than surfacing a meaningless token to
    the operator's discoverability layer.
    """
    from meho_backplane.connectors.registry import _REGISTRY_V2

    _REGISTRY_V2[("", "1.x", "ghost")] = _FakeConnector
    _REGISTRY_V2[("k8s", "1.x", "k8s")] = _FakeConnector
    assert registered_product_tokens() == {"k8s"}


def test_registered_product_tokens_returns_fresh_set_each_call() -> None:
    """Callers can mutate the returned set without affecting the registry.

    The helper returns a fresh ``set`` so a caller that sorts /
    extends / filters the result cannot accidentally corrupt the
    canonical registry. Defensive return-by-value invariant for any
    snapshot accessor over a mutable internal collection.
    """
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=_FakeConnector,
    )
    snapshot = registered_product_tokens()
    snapshot.add("phantom")
    assert "phantom" not in registered_product_tokens()
