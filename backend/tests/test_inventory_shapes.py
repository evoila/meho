# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The calculation result shape, the sample interval of an inventory, and the
checked-in JSON Schema files.

``docs/schemas/`` holds the estate inventory and calculation result shapes
as versioned JSON Schema files, for tools outside the backplane. A model
change without new files fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from scripts.generate_inventory_schemas import SCHEMA_FILES, render_schema

from meho_backplane.connectors.inventory.contracts import (
    CalculationResult,
    Finding,
    InventoryEnvelope,
    ServerRecord,
    Utilization,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TIME = "2026-10-20T08:00:00Z"


# --- calculation result -----------------------------------------------------


def _result(
    figure: dict[str, Any],
    *,
    assumptions: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "calculation_version": "sizing-0.1",
        "inputs": {
            "inventories": [
                {
                    "handle_id": "h-1",
                    "connector_id": "vmware-rest-9.0",
                    "target": "vc-a",
                    "collected_at": _TIME,
                }
            ],
            "generated_at": _TIME,
        },
        "assumptions": assumptions
        if assumptions is not None
        else [
            {"name": "hours_per_month", "value": 730, "unit": "hours"},
            {"name": "currency", "value": "EUR"},
            {"name": "price_date", "value": "2026-10-01", "source": "https://example.com/prices"},
        ],
        "items": [
            {
                "source_id": "vmware:x:vm-1",
                "name": "app-01",
                "figures": {"monthly_cost": figure},
                "findings": findings or [],
            }
        ],
        "unknowns": [{"what": "disk throughput of db-01", "reason": "not_measured"}],
    }


_FIGURE: dict[str, Any] = {
    "value": 140.16,
    "unit": "EUR/month",
    "basis": "allocated",
    "inputs": {"size": "medium", "price_per_hour": 0.192},
    "assumptions": ["hours_per_month", "currency"],
    "calculation_version": "sizing-0.1",
}


def test_figure_cites_named_assumptions() -> None:
    result = CalculationResult.model_validate(_result(_FIGURE))
    figure = result.items[0].figures["monthly_cost"]
    assert figure.inputs["size"] == "medium"
    assert figure.calculation_version == "sizing-0.1"
    hours = result.assumptions[0]
    assert (hours.name, hours.value, hours.unit) == ("hours_per_month", 730, "hours")
    assert result.assumptions[2].source == "https://example.com/prices"


def test_figure_that_cites_an_assumption_that_is_not_set_fails() -> None:
    unset = {**_FIGURE, "assumptions": ["reservation"]}
    with pytest.raises(ValidationError, match=r"cites assumptions \['reservation'\]"):
        CalculationResult.model_validate(_result(unset))
    with pytest.raises(ValidationError, match=r"cites assumptions \['currency'"):
        CalculationResult.model_validate(_result(_FIGURE, assumptions=[]))


def test_assumption_names_do_not_repeat() -> None:
    twice = [{"name": "currency", "value": "EUR"}, {"name": "currency", "value": "USD"}]
    with pytest.raises(ValidationError, match=r"assumption names repeat: \['currency'\]"):
        CalculationResult.model_validate(_result({**_FIGURE, "assumptions": []}, assumptions=twice))


def test_unknown_figure_needs_a_reason() -> None:
    unknown: dict[str, Any] = {"value": None, "unit": "EUR/month", "calculation_version": "1"}
    with pytest.raises(ValidationError, match="unknown value needs an unknown_reason"):
        CalculationResult.model_validate(_result(unknown))
    unknown["unknown_reason"] = "no price for this size"
    CalculationResult.model_validate(_result(unknown))
    for reason in ("no price for this size", ""):
        with pytest.raises(ValidationError, match="a known value has no unknown_reason"):
            CalculationResult.model_validate(_result({**_FIGURE, "unknown_reason": reason}))


def _finding(**change: Any) -> dict[str, Any]:
    return {
        "check": "boot type is supported",
        "state": "pass",
        "reason": "the server boots with UEFI",
        "inputs": {"boot_type": "uefi"},
        "source": "https://example.com/supported-boot-types",
        "source_checked_on": "2026-10-28",
        **change,
    }


def test_finding_with_data_carries_its_state_reason_and_source() -> None:
    result = CalculationResult.model_validate(_result(_FIGURE, findings=[_finding()]))
    finding = result.items[0].findings[0]
    assert finding.state == "pass"
    assert finding.inputs == {"boot_type": "uefi"}
    assert finding.source == "https://example.com/supported-boot-types"
    assert finding.source_checked_on.isoformat() == "2026-10-28"


def test_finding_with_missing_data_is_unknown_with_a_reason() -> None:
    missing = {"boot_type": None}
    for state in ("pass", "fail"):
        with pytest.raises(ValidationError, match=r"inputs \['boot_type'\] are missing"):
            Finding.model_validate(_finding(state=state, inputs=missing))
    unknown = Finding.model_validate(
        _finding(state="unknown", reason="the source does not report the boot type", inputs=missing)
    )
    assert unknown.state == "unknown"
    with pytest.raises(ValidationError):
        Finding.model_validate(_finding(state="unknown", reason="", inputs=missing))


def test_finding_needs_its_source_and_date() -> None:
    no_source = _finding()
    del no_source["source"]
    with pytest.raises(ValidationError, match="source"):
        Finding.model_validate(no_source)
    no_date = _finding()
    del no_date["source_checked_on"]
    with pytest.raises(ValidationError, match="source_checked_on"):
        Finding.model_validate(no_date)


# --- sample interval ----------------------------------------------------------


def _server(utilization: dict[str, Any], missing: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "kind": "server",
        "source_id": "vmware:x:vm-1",
        "identity_hints": {
            "bios_uuid": "4203a1b2-1111-4000-8000-0000000000a1",
            "host_name": "app-01",
        },
        "name": "app-01",
        "server_type": "virtual",
        "hypervisor": "VMware",
        "cores": 2,
        "memory_configured_mb": 4096,
        "memory_assigned_mb": 4096,
        "disks": [],
        "snapshot_count": 0,
        "os": {
            "name": "Ubuntu Linux",
            "version": "24.04",
            "family": "linux",
            "architecture": "x64",
        },
        "boot_type": "uefi",
        "secure_boot": True,
        "power_state": "on",
        "nics": [],
        "utilization": utilization,
        "collected_at": _TIME,
        "missing": missing or {},
    }


def _utilization(**extra: Any) -> dict[str, Any]:
    stat = {"average": 10.0, "p95": 20.0, "maximum": 30.0}
    return {
        "window_start": "2026-09-20T08:00:00Z",
        "window_end": _TIME,
        "samples": 360,
        "source": "vcenter-performance",
        "cpu_percent": stat,
        "memory_percent": stat,
        "network_in_mb_per_s": stat,
        "network_out_mb_per_s": stat,
        **extra,
    }


def _inventory(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "source": {"connector_id": "vmware-rest-9.0", "target": "vc-a", "collected_at": _TIME},
        "records": [record],
        "counts": {"server": 1, "application": 0, "kubernetes_workload": 0},
    }


def test_utilization_carries_the_sample_interval() -> None:
    record = ServerRecord.model_validate(_server(_utilization(sample_interval_minutes=120)))
    assert record.utilization is not None
    assert record.utilization.sample_interval_minutes == 120
    assert "utilization.sample_interval_minutes" not in record.missing


def test_inventory_stored_before_the_interval_still_loads() -> None:
    envelope = InventoryEnvelope.model_validate(_inventory(_server(_utilization())))
    record = envelope.records[0]
    assert isinstance(record, ServerRecord)
    assert record.utilization is not None
    assert record.utilization.sample_interval_minutes is None
    assert record.missing["utilization.sample_interval_minutes"] == "not_reported_by_source"
    # Stored again and read back, it stays the same.
    again = InventoryEnvelope.model_validate_json(envelope.model_dump_json())
    assert again == envelope


def test_older_inventory_keeps_its_own_reason() -> None:
    record = ServerRecord.model_validate(
        _server(_utilization(), missing={"utilization": "collection_failed"})
    )
    assert record.missing == {"utilization": "collection_failed"}


def test_a_null_interval_from_a_producer_needs_a_reason() -> None:
    utilization = Utilization.model_validate(_utilization())
    built = {**_server({}), "utilization": utilization}
    with pytest.raises(ValidationError, match=r"utilization.sample_interval_minutes"):
        ServerRecord.model_validate(built)
    with pytest.raises(ValidationError, match="greater than 0"):
        Utilization.model_validate(_utilization(sample_interval_minutes=0))


# --- schema files -----------------------------------------------------------


@pytest.mark.parametrize("relpath", sorted(SCHEMA_FILES))
def test_schema_file_matches_the_models(relpath: str) -> None:
    committed = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert committed == render_schema(SCHEMA_FILES[relpath]), (
        f"{relpath} does not match the models. Make it again with "
        "`uv run python scripts/generate_inventory_schemas.py` from backend/ "
        "and commit the result."
    )


@pytest.mark.parametrize("relpath", sorted(SCHEMA_FILES))
def test_schema_file_is_a_valid_schema_with_its_version(relpath: str) -> None:
    schema = json.loads((_REPO_ROOT / relpath).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert relpath.endswith(f"-v{schema['properties']['schema_version']['const']}.schema.json")


def _result_schema_file() -> Draft202012Validator:
    """The committed result schema, read as a tool outside the backplane would."""
    relpath = next(path for path in SCHEMA_FILES if "calculation-result" in path)
    schema = json.loads((_REPO_ROOT / relpath).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def test_result_schema_file_checks_a_result_without_backplane_code() -> None:
    validator = _result_schema_file()
    assert not list(validator.iter_errors(_result(_FIGURE, findings=[_finding()])))
    wrong = _result(_FIGURE, findings=[_finding(state="maybe")])
    assert list(validator.iter_errors(wrong))


def test_result_schema_file_rejects_a_finding_with_missing_data_that_is_not_unknown() -> None:
    validator = _result_schema_file()
    missing = {"boot_type": None, "secure_boot": True}
    for state in ("pass", "fail"):
        wrong = _result(_FIGURE, findings=[_finding(state=state, inputs=missing)])
        assert list(validator.iter_errors(wrong)), state
    unknown = _result(_FIGURE, findings=[_finding(state="unknown", inputs=missing)])
    assert not list(validator.iter_errors(unknown))
    no_inputs = _result(_FIGURE, findings=[_finding(inputs={})])
    assert not list(validator.iter_errors(no_inputs))


def test_result_schema_file_rejects_an_unknown_figure_without_a_reason() -> None:
    validator = _result_schema_file()
    unknown: dict[str, Any] = {"value": None, "unit": "EUR/month", "calculation_version": "1"}
    assert list(validator.iter_errors(_result(unknown)))
    for reason in (None, ""):
        assert list(validator.iter_errors(_result({**unknown, "unknown_reason": reason}))), reason
    with_reason = {**unknown, "unknown_reason": "no price for this size"}
    assert not list(validator.iter_errors(_result(with_reason)))
    known_with_reason = {**_FIGURE, "unknown_reason": "no price for this size"}
    assert list(validator.iter_errors(_result(known_with_reason)))
    assert not list(validator.iter_errors(_result({**_FIGURE, "unknown_reason": None})))
