# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The appliance version and build as ``GET /lcm/lcops/api/v2/about`` reports them.

:meth:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector.appliance_version`
reads the call and returns a :class:`FleetApplianceVersion`;
``fleet.api_file.get`` puts its fields next to the file it read. On VCF 9.0
builds the call answers HTTP 500, so the value may carry only an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["FleetApplianceVersion", "parse_appliance_version"]


@dataclass(frozen=True)
class FleetApplianceVersion:
    """One ``about`` read: ``productVersion``, ``buildNumber`` and why a read failed.

    ``error`` names why the read failed; then ``version`` and ``build`` are
    ``None``.
    """

    version: str | None
    build: str | None
    error: str | None


def parse_appliance_version(payload: Any) -> FleetApplianceVersion:
    """Pick the product version and build out of an ``about`` answer."""
    if not isinstance(payload, dict):
        return FleetApplianceVersion(
            version=None, build=None, error="about answer is not a JSON object"
        )
    version = payload.get("productVersion")
    if not isinstance(version, str) or not version:
        return FleetApplianceVersion(
            version=None, build=None, error="about answer has no productVersion"
        )
    build_raw = payload.get("buildNumber")
    build = str(build_raw) if build_raw not in (None, "") else None
    return FleetApplianceVersion(version=version, build=build, error=None)
