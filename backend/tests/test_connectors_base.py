# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Connector ABC and connector schemas.

Coverage matrix (per Task #240 acceptance criteria):

* Package exists and all public names are importable from the package root.
* :class:`Connector` is an ABC: instantiating it (or a subclass that omits
  any of the three abstract methods) raises :exc:`TypeError`.
* A concrete subclass that implements all three methods can be instantiated.
* Schema round-trip: ``model_dump()`` → ``model_validate()`` is lossless for
  each model.
* All three models are frozen: field reassignment raises
  :exc:`pydantic.ValidationError`; ``extras`` in-place mutation raises
  :exc:`TypeError` (deep immutability via :class:`types.MappingProxyType`).
* :class:`AuthModel` accepts all three canonical string values and rejects
  unknown values with :exc:`ValueError`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.connectors import (
    AuthModel,
    Connector,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.schemas import (
    AuthModel as _AuthModelDirect,
)
from meho_backplane.connectors.schemas import (
    FingerprintResult as _FingerprintDirect,
)
from meho_backplane.connectors.schemas import (
    OperationResult as _OperationDirect,
)
from meho_backplane.connectors.schemas import (
    ProbeResult as _ProbeDirect,
)

# ---------------------------------------------------------------------------
# Package-level import checks
# ---------------------------------------------------------------------------


def test_package_exports_connector() -> None:
    assert Connector is not None


def test_package_exports_schemas() -> None:
    for cls in (AuthModel, FingerprintResult, OperationResult, ProbeResult):
        assert cls is not None


def test_package_root_aliases_match_submodule() -> None:
    assert Connector.__module__ == "meho_backplane.connectors.base"
    assert AuthModel is _AuthModelDirect
    assert FingerprintResult is _FingerprintDirect
    assert OperationResult is _OperationDirect
    assert ProbeResult is _ProbeDirect


# ---------------------------------------------------------------------------
# Connector ABC enforcement
# ---------------------------------------------------------------------------


def test_connector_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        Connector()  # type: ignore[abstract]


def test_connector_subclass_missing_all_methods_raises() -> None:
    class Incomplete(Connector):
        product = "test"

    with pytest.raises(TypeError):
        Incomplete()


def test_connector_subclass_missing_one_method_raises() -> None:
    class MissingExecute(Connector):
        product = "test"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

    with pytest.raises(TypeError):
        MissingExecute()


def test_concrete_connector_instantiates() -> None:
    class FullConnector(Connector):
        product = "test"

        async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
            raise NotImplementedError

        async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
            raise NotImplementedError

    conn = FullConnector()
    assert conn.product == "test"


# ---------------------------------------------------------------------------
# Registry v2 metadata class attrs (G0.6-T3 #394)
# ---------------------------------------------------------------------------


class _MinimalConnector(Connector):
    """Subclass that sets only ``product`` — exercises v1 backward-compat defaults."""

    product = "minimal"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _OverridingConnector(Connector):
    """Subclass that sets every registry-v2 attr — exercises override path."""

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=8.5,<10.0"
    priority = 10

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


def test_connector_class_attr_defaults_preserve_v1_behaviour() -> None:
    # ABC-level defaults — read via the class directly (no instance needed).
    assert Connector.version == ""
    assert Connector.impl_id == ""
    assert Connector.supported_version_range is None
    assert Connector.priority == 0


def test_connector_subclass_without_overrides_inherits_defaults() -> None:
    # The registry-v2 resolver (#393) reads these as class attrs; a v1-style
    # subclass that sets only `product` must read back the documented defaults.
    assert _MinimalConnector.version == ""
    assert _MinimalConnector.impl_id == ""
    assert _MinimalConnector.supported_version_range is None
    assert _MinimalConnector.priority == 0

    inst = _MinimalConnector()
    assert inst.version == ""
    assert inst.impl_id == ""
    assert inst.supported_version_range is None
    assert inst.priority == 0


def test_connector_subclass_overrides_read_back_at_class_level() -> None:
    # Class-level access is what registry v2 reads.
    assert _OverridingConnector.version == "9.0"
    assert _OverridingConnector.impl_id == "vmware-rest"
    assert _OverridingConnector.supported_version_range == ">=8.5,<10.0"
    assert _OverridingConnector.priority == 10


def test_connector_subclass_overrides_read_back_at_instance_level() -> None:
    inst = _OverridingConnector()
    assert inst.version == "9.0"
    assert inst.impl_id == "vmware-rest"
    assert inst.supported_version_range == ">=8.5,<10.0"
    assert inst.priority == 10


def test_shipped_subclasses_advertise_v2_metadata_per_their_initiative() -> None:
    # The shipped connectors' v2-metadata posture differs per Initiative:
    #
    # * :class:`VaultConnector` (#244, refactored under G0.6-T-Refactor-Vault
    #   #390) advertises ``version="1.x"`` / ``impl_id="vault"``; its
    #   :class:`~meho_backplane.connectors.vault.__init__` calls
    #   :func:`register_connector_v2` directly so the v2 resolver hits
    #   the same row the typed-op upsert keys on.
    # * :class:`KubernetesConnector` (skeleton from #321) stays on the v1
    #   defaults (``version=""`` / ``impl_id=""``) until its own G0.6
    #   refactor lands — the v1 ``register_connector`` call dual-writes
    #   the v2 entry as ``("kubernetes", "", "")``.
    #
    # ``supported_version_range`` and ``priority`` keep their type-system
    # defaults on both since neither connector has shipped versioned
    # behaviour yet.
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.vault.connector import VaultConnector

    assert VaultConnector.product == "vault"
    assert VaultConnector.version == "1.x"
    assert VaultConnector.impl_id == "vault"
    assert VaultConnector.supported_version_range is None
    assert VaultConnector.priority == 0

    # G0.6 refactor (#391) flipped the K8s connector from the v1 single-
    # product slug ``"kubernetes"`` to the v2-canonical ``"k8s"`` /
    # ``"1.x"`` / ``"k8s"`` triple. The G3.2-T6 precursor (#326) realigned
    # the impl_id from the library name ``"kubernetes-asyncio"`` to the
    # single-impl ``impl_id == product`` shape that parse_connector_id
    # round-trips for ``"k8s-1.x"``; the library name now lives in the
    # package layout + pyproject.toml dependency, not the registry triple.
    # ``supported_version_range`` and ``priority`` still inherit the ABC
    # defaults from G0.6-T3 (#394).
    assert KubernetesConnector.product == "k8s"
    assert KubernetesConnector.version == "1.x"
    assert KubernetesConnector.impl_id == "k8s"
    assert KubernetesConnector.supported_version_range is None
    assert KubernetesConnector.priority == 0


# ---------------------------------------------------------------------------
# AuthModel enum
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["impersonation", "shared_service_account", "per_user"],
)
def test_auth_model_accepts_valid_values(value: str) -> None:
    result = AuthModel(value)
    assert result.value == value


def test_auth_model_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        AuthModel("unknown_model")


def test_auth_model_str_values() -> None:
    assert AuthModel.IMPERSONATION == "impersonation"
    assert AuthModel.SHARED_SERVICE_ACCOUNT == "shared_service_account"
    assert AuthModel.PER_USER == "per_user"


# ---------------------------------------------------------------------------
# FingerprintResult
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


def _fingerprint_kwargs() -> dict[str, Any]:
    return {
        "vendor": "VMware",
        "product": "vCenter",
        "version": "8.0.3",
        "build": "12345",
        "edition": "Standard",
        "reachable": True,
        "probed_at": _NOW,
        "probe_method": "api",
        "extras": {"cluster_count": 3},
    }


def test_fingerprint_result_round_trip() -> None:
    fr = FingerprintResult(**_fingerprint_kwargs())
    dumped = fr.model_dump()
    restored = FingerprintResult.model_validate(dumped)
    assert restored == fr


def test_fingerprint_result_optional_fields_default() -> None:
    fr = FingerprintResult(
        vendor="Cisco",
        product="switch",
        reachable=False,
        probed_at=_NOW,
        probe_method="tcp",
    )
    assert fr.version is None
    assert fr.build is None
    assert fr.edition is None
    assert dict(fr.extras) == {}


def test_fingerprint_result_is_frozen() -> None:
    fr = FingerprintResult(**_fingerprint_kwargs())
    with pytest.raises(ValidationError):
        fr.vendor = "mutated"  # type: ignore[misc]


def test_fingerprint_result_extras_is_deeply_immutable() -> None:
    fr = FingerprintResult(**_fingerprint_kwargs())
    with pytest.raises(TypeError):
        fr.extras["new_key"] = "value"  # type: ignore[index]


# ---------------------------------------------------------------------------
# ProbeResult
# ---------------------------------------------------------------------------


def test_probe_result_round_trip() -> None:
    pr = ProbeResult(ok=True, reason=None, latency_ms=12.5, probed_at=_NOW)
    dumped = pr.model_dump()
    restored = ProbeResult.model_validate(dumped)
    assert restored == pr


def test_probe_result_optional_fields_default() -> None:
    pr = ProbeResult(ok=False, probed_at=_NOW)
    assert pr.reason is None
    assert pr.latency_ms is None


def test_probe_result_is_frozen() -> None:
    pr = ProbeResult(ok=True, probed_at=_NOW)
    with pytest.raises(ValidationError):
        pr.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OperationResult
# ---------------------------------------------------------------------------


def test_operation_result_round_trip() -> None:
    op = OperationResult(
        status="ok",
        op_id="vsphere.vm.list",
        result=[{"name": "vm-01"}],
        error=None,
        duration_ms=55.3,
        extras={"page": 1},
    )
    dumped = op.model_dump()
    restored = OperationResult.model_validate(dumped)
    assert restored == op


def test_operation_result_optional_fields_default() -> None:
    op = OperationResult(status="error", op_id="vault.kv.read", error="denied", duration_ms=3.1)
    assert op.result is None
    assert dict(op.extras) == {}


def test_operation_result_is_frozen() -> None:
    op = OperationResult(status="ok", op_id="bind9.zone.list", duration_ms=9.0)
    with pytest.raises(ValidationError):
        op.status = "mutated"  # type: ignore[misc]


def test_operation_result_extras_is_deeply_immutable() -> None:
    op = OperationResult(status="ok", op_id="vsphere.vm.list", duration_ms=1.0, extras={"k": "v"})
    with pytest.raises(TypeError):
        op.extras["new_key"] = "value"  # type: ignore[index]


def test_operation_result_result_can_be_list() -> None:
    op = OperationResult(status="ok", op_id="vsphere.vm.list", result=[1, 2, 3], duration_ms=1.0)
    assert op.result == [1, 2, 3]


def test_operation_result_result_can_be_dict() -> None:
    op = OperationResult(
        status="ok", op_id="vault.kv.read", result={"data": "value"}, duration_ms=2.0
    )
    assert op.result == {"data": "value"}
