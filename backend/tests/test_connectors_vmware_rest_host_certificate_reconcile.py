# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the host certificate and TPM calls against the pinned vendor VI-JSON files.

* An always-on test pins the vendor-call lists of the three changes and the
  two reads, so a change to them is seen.
* The shelf tests read the pinned ``vi-json.yaml`` of vCenter 9.0 and the
  three 8.0 files (U1, U2, U3): every VI-JSON method is a ``POST`` path there,
  the request bodies MEHO sends use only fields of the vendor's request
  types, and the properties the reads ask for exist. They skip when the spec
  shelf is not configured and run in CI.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.composites._host_certificate_ops import (
    HOST_CERTIFICATE_GOVERNED_SUBOPS,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_certificate import READ_VENDOR_CALLS
from tests._spec_shelf import require_shelf_spec

_VI_JSON_FILES = (
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
)

_VIM_CALLS = sorted(
    {
        op_id
        for calls in (*HOST_CERTIFICATE_GOVERNED_SUBOPS.values(), *READ_VENDOR_CALLS.values())
        for op_id in calls
    }
)


@functools.cache
def _spec(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _fields(spec: dict[str, Any], type_name: str) -> set[str]:
    """The fields of a vendor request or data type (one ``allOf`` level deep)."""
    schema = spec["components"]["schemas"][type_name]
    fields = set(schema.get("properties") or {})
    for part in schema.get("allOf") or []:
        fields |= set(part.get("properties") or {})
    return fields


def test_the_vendor_call_lists_are_the_expected_sets() -> None:
    assert set(HOST_CERTIFICATE_GOVERNED_SUBOPS) == {
        "vmware.composite.host.certificate.renew",
        "vmware.composite.host.certificate.csr",
        "vmware.composite.host.certificate.install",
    }
    assert set(READ_VENDOR_CALLS) == {
        "vmware.host.certificate.show",
        "vmware.host.tpm.attestation",
    }
    assert _VIM_CALLS == [
        "POST:/CertificateManager/{moId}/CertMgrRefreshCertificates_Task",
        "POST:/HostCertificateManager/{moId}/GenerateCertificateSigningRequest",
        "POST:/HostCertificateManager/{moId}/InstallServerCertificate",
        "POST:/HostCertificateManager/{moId}/ListCACertificateRevocationLists",
        "POST:/HostCertificateManager/{moId}/ListCACertificates",
        "POST:/HostCertificateManager/{moId}/NotifyAffectedServices",
        "POST:/HostCertificateManager/{moId}/ReplaceCACertificatesAndCRLs",
        "POST:/HostSystem/{moId}/QueryTpmAttestationReport",
        "POST:/PropertyCollector/{moId}/ContinueRetrievePropertiesEx",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
    ]


#: Calls that are in the vSphere API from 8.0 U3 on. The install skips them on
#: an older vCenter (the version check in ``_host_certificate_install.py``).
_SINCE_8_0_3 = {"POST:/HostCertificateManager/{moId}/NotifyAffectedServices"}
_BEFORE_8_0_3 = {"vi-json-8.0u1.yaml", "vi-json-8.0u2.yaml"}


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_every_vi_json_call_is_a_post_path_of_the_pinned_file(
    product_dir: str, file_name: str
) -> None:
    paths = _spec(require_shelf_spec(product_dir, file_name))["paths"]
    expected = set(_VIM_CALLS) - (_SINCE_8_0_3 if file_name in _BEFORE_8_0_3 else set())
    missing = sorted(
        op_id for op_id in expected if "post" not in paths.get(op_id.partition(":")[2], {})
    )
    assert not missing, f"{product_dir}/{file_name} has no POST path for {missing}"
    if file_name in _BEFORE_8_0_3:
        # Still absent: when a file gains it, drop the version check in the install.
        assert all(op_id.partition(":")[2] not in paths for op_id in _SINCE_8_0_3)


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_the_request_bodies_use_vendor_fields(product_dir: str, file_name: str) -> None:
    spec = _spec(require_shelf_spec(product_dir, file_name))
    assert _fields(spec, "CertMgrRefreshCertificatesRequestType") == {"host"}
    assert "useIpAddressAsCommonName" in _fields(
        spec, "GenerateCertificateSigningRequestRequestType"
    )
    assert _fields(spec, "InstallServerCertificateRequestType") == {"cert"}
    assert _fields(spec, "ReplaceCACertificatesAndCRLsRequestType") == {"caCert", "caCrl"}
    if file_name not in _BEFORE_8_0_3:
        # MEHO sends an empty NotifyAffectedServices body: "services" is optional.
        notify = spec["components"]["schemas"]["NotifyAffectedServicesRequestType"]
        assert not notify.get("required")


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_the_properties_the_reads_ask_for_exist(product_dir: str, file_name: str) -> None:
    spec = _spec(require_shelf_spec(product_dir, file_name))
    assert "certificateManager" in _fields(spec, "HostConfigManager")
    assert "certificateManager" in _fields(spec, "ServiceContent")
    assert {"issuer", "subject", "notBefore", "notAfter", "status"} <= _fields(
        spec, "HostCertificateManagerCertificateInfo"
    )
    assert {"tpmSupported", "tpmVersion", "txtEnabled"} <= _fields(spec, "HostCapability")
    assert "tpmAttestation" in _fields(spec, "HostListSummary")
    assert {"time", "status", "message"} <= _fields(spec, "HostTpmAttestationInfo")
    assert {"tpmPcrValues", "tpmEvents", "tpmLogReliable"} <= _fields(
        spec, "HostTpmAttestationReport"
    )
