"""Regression guard for the checked-in VCF API compatibility evidence record."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from meho_backplane.connectors.sddc_manager.typed_ops import SDDC_TYPED_OPS
from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "compatibility" / "vcf-api-contract-manifest.yaml"
VALIDATOR = ROOT / "backend" / "scripts" / "validate_vcf_api_contract_manifest.py"
SPEC = importlib.util.spec_from_file_location("vcf_manifest_validator", VALIDATOR)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def test_vcf_api_contract_manifest_is_complete_and_machine_validated() -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--manifest", str(MANIFEST), "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    assert summary["release_rows"] == 16
    assert summary["component_rows"] == 128
    assert summary["artifacts"] >= 1
    assert summary["catalog_profiles"] >= 1


def _manifest() -> dict[str, object]:
    loaded = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _safety_groups(operations: object) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    for operation in operations:
        groups.setdefault(operation.safety_level, set()).add(operation.op_id)
    return groups


def test_vcf_regression_subsets_match_registered_sddc_and_vmware_operations() -> None:
    data = _manifest()
    subsets = data["meho_regression_subsets"]
    assert isinstance(subsets, dict)

    sddc = subsets["sddc-rest"]
    assert isinstance(sddc, dict)
    assert set(sddc["typed_op_ids"]) == {operation.op_id for operation in SDDC_TYPED_OPS}
    assert {
        level: set(op_ids) for level, op_ids in sddc["typed_op_safety"].items()
    } == _safety_groups(SDDC_TYPED_OPS)

    vmware = subsets["vmware-rest"]
    assert isinstance(vmware, dict)
    assert set(vmware["typed_op_ids"]) == {operation.op_id for operation in VMWARE_TYPED_OPS}
    assert {
        level: set(op_ids) for level, op_ids in vmware["typed_composite_ops"].items()
    } == _safety_groups(_COMPOSITES)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["release_rows"].pop(), "enumerate each supported VCF release"),
        (
            lambda data: data["release_rows"][0]["components"]["nsx"].__setitem__(
                "catalog_profile", "missing"
            ),
            "catalog_profile is unknown",
        ),
        (
            lambda data: data["artifacts"]["sddc-9.0"].__setitem__("sha256", "not-a-hash"),
            "must be a lowercase SHA-256",
        ),
        (
            lambda data: data["artifacts"]["vsphere-8.x"].__setitem__(
                "catalog", {"paths": 0, "operations": 0, "schemas": 0}
            ),
            "must not invent catalog counts",
        ),
        (
            lambda data: data["release_rows"][11]["components"]["sddc_manager"].__setitem__(
                "qualification_artifacts", ["sddc-5.x"]
            ),
            "has no qualifying artifact",
        ),
        (
            lambda data: data.__delitem__("offline_comparisons"),
            "offline_comparisons must be a mapping",
        ),
        (
            lambda data: data.__setitem__("meho_regression_subsets", {}),
            "meho_regression_subsets.note",
        ),
        (
            lambda data: data["artifacts"]["sddc-9.0"].__setitem__("source_url", "not-a-url"),
            "must be an HTTPS URL",
        ),
        (
            lambda data: data["catalog_profiles"]["sddc-9.0"]["contract_delta"].__setitem__(
                "auth", ""
            ),
            "contract_delta.auth",
        ),
    ],
)
def test_vcf_api_contract_manifest_rejects_missing_or_fictitious_evidence(
    mutate: object, message: str
) -> None:
    data = deepcopy(_manifest())
    assert callable(mutate)
    mutate(data)

    with pytest.raises(validator.ManifestError, match=message):
        validator.validate_manifest(data)
