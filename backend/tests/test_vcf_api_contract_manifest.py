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
