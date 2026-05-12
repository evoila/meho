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
* All three models are frozen: mutation after construction raises
  :exc:`pydantic.ValidationError`.
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
    assert fr.extras == {}


def test_fingerprint_result_is_frozen() -> None:
    fr = FingerprintResult(**_fingerprint_kwargs())
    with pytest.raises(ValidationError):
        fr.vendor = "mutated"  # type: ignore[misc]


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
    assert op.extras == {}


def test_operation_result_is_frozen() -> None:
    op = OperationResult(status="ok", op_id="bind9.zone.list", duration_ms=9.0)
    with pytest.raises(ValidationError):
        op.status = "mutated"  # type: ignore[misc]


def test_operation_result_result_can_be_list() -> None:
    op = OperationResult(status="ok", op_id="vsphere.vm.list", result=[1, 2, 3], duration_ms=1.0)
    assert op.result == [1, 2, 3]


def test_operation_result_result_can_be_dict() -> None:
    op = OperationResult(
        status="ok", op_id="vault.kv.read", result={"data": "value"}, duration_ms=2.0
    )
    assert op.result == {"data": "value"}
