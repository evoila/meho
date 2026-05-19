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
from meho_backplane.connectors.registry import clear_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConnector(Connector):
    product = "fake"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
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
    """SddcManagerConnector package registers under (sddc-manager, 9.0, sddc-rest) at import.

    The autouse _clean_registry fixture clears the registry before this test,
    so we re-import the package (which is a no-op if already imported) and
    then manually re-register to assert the triple resolves correctly. Mirrors
    the pattern in test_connectors_nsx_auth.py.
    """
    from meho_backplane.connectors.sddc_manager import SddcManagerConnector

    register_connector_v2(
        product=SddcManagerConnector.product,
        version=SddcManagerConnector.version,
        impl_id=SddcManagerConnector.impl_id,
        cls=SddcManagerConnector,
    )
    snapshot = all_connectors_v2()
    key = ("sddc-manager", "9.0", "sddc-rest")
    assert key in snapshot
    assert snapshot[key] is SddcManagerConnector
