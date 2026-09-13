#!/usr/bin/env python3
"""Validate the checked-in VCF API contract evidence manifest.

The manifest deliberately records pins and catalog summaries, not vendor API
documents.  Some of those documents are licence-restricted and remain on the
private spec shelf.  This validator is therefore offline: it proves the public
evidence record is internally complete without downloading a vendor artifact.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

EXPECTED_RELEASES = {
    "5.0",
    "5.0.0.1",
    "5.1",
    "5.1.1",
    "5.2",
    "5.2.1",
    "5.2.1.1",
    "5.2.1.2",
    "5.2.2",
    "5.2.3",
    "5.2.4",
    "9.0",
    "9.0.1",
    "9.0.2",
    "9.1",
    "9.1.1",
}
EXPECTED_COMPONENTS = {
    "sddc_manager",
    "vcenter_esxi",
    "nsx",
    "vcf_automation",
    "fleet",
    "vcf_operations",
    "vcf_operations_for_logs",
    "installer",
}
EVIDENCE_LEVELS = {"unqualified", "registered", "spec-fixture-qualified", "live-verified"}
AVAILABILITY = {"public-pinned", "private-shelf-pinned", "official-untagged", "unavailable"}
EXPOSURE_STATES = {
    "typed",
    "generic-enabled",
    "catalogued-not-enabled",
    "not-yet-integrated",
}
REQUIRED_DELTA_KEYS = {"endpoint", "schema", "auth", "feature_state"}


class ManifestError(ValueError):
    """Raised when the evidence record cannot support its stated contract."""


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a mapping")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be non-empty text")
    return value


def _validate_artifacts(artifacts: dict[str, Any]) -> None:
    for artifact_id, artifact in artifacts.items():
        record = _require_mapping(artifact, f"artifact {artifact_id}")
        _require_text(record.get("source_url"), f"artifact {artifact_id}.source_url")
        _require_text(record.get("source_revision"), f"artifact {artifact_id}.source_revision")
        if record.get("availability") not in AVAILABILITY:
            raise ManifestError(f"artifact {artifact_id}.availability is invalid")
        catalog = _require_mapping(record.get("catalog"), f"artifact {artifact_id}.catalog")
        if record["availability"] == "unavailable":
            if any(catalog.get(key) is not None for key in ("paths", "operations", "schemas")):
                raise ManifestError(
                    f"unavailable artifact {artifact_id} must not invent catalog counts"
                )
            _require_text(record.get("evidence_gap"), f"artifact {artifact_id}.evidence_gap")
            continue
        checksum = _require_text(record.get("sha256"), f"artifact {artifact_id}.sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ManifestError(f"artifact {artifact_id}.sha256 must be a lowercase SHA-256")
        for key in ("paths", "operations", "schemas"):
            if catalog.get(key) is None:
                _require_text(record.get("catalog_gap"), f"artifact {artifact_id}.catalog_gap")
            elif not isinstance(catalog.get(key), int) or catalog[key] < 0:
                raise ManifestError(
                    f"artifact {artifact_id}.catalog.{key} must be a non-negative integer"
                )


def _validate_profiles(profiles: dict[str, Any], artifacts: dict[str, Any]) -> None:
    for profile_id, profile in profiles.items():
        record = _require_mapping(profile, f"catalog profile {profile_id}")
        refs = record.get("artifact_refs")
        if not isinstance(refs, list) or not refs or any(ref not in artifacts for ref in refs):
            raise ManifestError(
                f"catalog profile {profile_id}.artifact_refs must name manifest artifacts"
            )
        delta = _require_mapping(
            record.get("contract_delta"), f"catalog profile {profile_id}.contract_delta"
        )
        missing_delta = REQUIRED_DELTA_KEYS - set(delta)
        if missing_delta:
            raise ManifestError(
                f"catalog profile {profile_id} misses delta fields: {sorted(missing_delta)}"
            )
        exposure = _require_mapping(
            record.get("meho_exposure"), f"catalog profile {profile_id}.meho_exposure"
        )
        if set(exposure) != EXPOSURE_STATES:
            raise ManifestError(
                f"catalog profile {profile_id} must separate all MEHO exposure states"
            )


def _validate_component(
    release: str,
    component_name: str,
    component: object,
    profiles: dict[str, Any],
    artifacts: dict[str, Any],
) -> None:
    item = _require_mapping(component, f"release {release}.{component_name}")
    _require_text(item.get("version"), f"release {release}.{component_name}.version")
    _require_text(item.get("build"), f"release {release}.{component_name}.build")
    if item.get("evidence_level") not in EVIDENCE_LEVELS:
        raise ManifestError(f"release {release}.{component_name}.evidence_level is invalid")
    if item.get("catalog_profile") not in profiles:
        raise ManifestError(f"release {release}.{component_name}.catalog_profile is unknown")
    _require_text(item.get("evidence_note"), f"release {release}.{component_name}.evidence_note")
    if item["evidence_level"] == "registered":
        _require_text(
            item.get("advertised_range"), f"release {release}.{component_name}.advertised_range"
        )
    if item["evidence_level"] == "spec-fixture-qualified":
        _require_text(
            item.get("qualification_basis"),
            f"release {release}.{component_name}.qualification_basis",
        )
        profile_artifacts = profiles[item["catalog_profile"]]["artifact_refs"]
        if not any(
            artifacts[artifact]["availability"] != "unavailable" for artifact in profile_artifacts
        ):
            raise ManifestError(f"release {release}.{component_name} has no qualifying artifact")
    if item["evidence_level"] == "live-verified":
        _require_text(
            item.get("live_observation"), f"release {release}.{component_name}.live_observation"
        )


def _validate_releases(
    releases: object, profiles: dict[str, Any], artifacts: dict[str, Any]
) -> tuple[int, int]:
    if not isinstance(releases, list):
        raise ManifestError("release_rows must be a list")
    names = [row.get("vcf_release") for row in releases if isinstance(row, dict)]
    if set(names) != EXPECTED_RELEASES or len(names) != len(EXPECTED_RELEASES):
        raise ManifestError("release_rows must enumerate each supported VCF release exactly once")

    component_rows = 0
    for row in releases:
        record = _require_mapping(row, "release row")
        _require_text(
            record.get("bom_evidence"), f"release {record.get('vcf_release')}.bom_evidence"
        )
        components = _require_mapping(
            record.get("components"), f"release {record.get('vcf_release')}.components"
        )
        if set(components) != EXPECTED_COMPONENTS:
            raise ManifestError(
                f"release {record.get('vcf_release')} must list every required component"
            )
        for component_name, component in components.items():
            _validate_component(
                record["vcf_release"], component_name, component, profiles, artifacts
            )
            component_rows += 1

    return len(releases), component_rows


def validate_manifest(manifest: dict[str, Any]) -> dict[str, int]:
    """Validate schema, release coverage, provenance, and exposure separation."""
    if manifest.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")
    publication = _require_mapping(manifest.get("publication_boundary"), "publication_boundary")
    if publication.get("raw_vendor_artifacts") != "private-spec-shelf-only":
        raise ManifestError(
            "publication_boundary must keep raw_vendor_artifacts private-spec-shelf-only"
        )
    artifacts = _require_mapping(manifest.get("artifacts"), "artifacts")
    _validate_artifacts(artifacts)
    profiles = _require_mapping(manifest.get("catalog_profiles"), "catalog_profiles")
    _validate_profiles(profiles, artifacts)
    release_rows, component_rows = _validate_releases(
        manifest.get("release_rows"), profiles, artifacts
    )
    return {
        "artifacts": len(artifacts),
        "catalog_profiles": len(profiles),
        "release_rows": release_rows,
        "component_rows": component_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Path to the YAML manifest")
    parser.add_argument(
        "--json", action="store_true", help="Emit the validated coverage summary as JSON"
    )
    args = parser.parse_args()
    try:
        loaded = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        summary = validate_manifest(_require_mapping(loaded, "manifest"))
    except (OSError, yaml.YAMLError, ManifestError) as exc:
        print(f"manifest validation failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(
            "manifest validation passed: "
            + ", ".join(f"{key}={value}" for key, value in summary.items())
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
