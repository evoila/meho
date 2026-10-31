# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reconcile lane of the VCFA sign-in ops: each vendor call against a pinned list.

Broadcom publishes no API file for VCF Automation 9.x. The spec shelf pins
reference lists made from the vendor's API reference pages (one line per
call: method, path):

* the ``/cloudapi`` calls (the connection test and the certificate trust)
  must be in the provider list of VCF Automation 9.0, 9.1 and 9.1.1;
* the classic ``/api`` calls (the LDAP and OIDC settings) are in none of
  those lists. VCF Automation keeps the classic API of VMware Cloud
  Director, so they must be in the VMware Cloud Director API 39.1 list
  (``vmware-cloud-director-10.6/api-39.1``), where paths have no ``/api``
  prefix. A live check on VCF Automation 9.x is a separate task.

The lists write a path parameter as a plain word or as ``{name}``; a
``{...}`` segment of MEHO's path matches any one segment. The lane skips
when the shelf is not configured (``tests/_spec_shelf.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from meho_backplane.connectors.vcf_automation import _paths
from tests._spec_shelf import require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

#: The ``/cloudapi`` calls of the sign-in ops.
_CLOUDAPI_CALLS = (
    ("POST", _paths.PROVIDER_TEST_CONNECTION_PATH),
    ("POST", _paths.PROVIDER_TRUSTED_CERTIFICATES_PATH),
    ("DELETE", _paths.PROVIDER_TRUSTED_CERTIFICATE_PATH),
)
#: The classic ``/api`` calls of the sign-in ops.
_CLASSIC_CALLS = (
    ("GET", _paths.PROVIDER_LDAP_SETTINGS_PATH),
    ("PUT", _paths.PROVIDER_LDAP_SETTINGS_PATH),
    ("GET", _paths.PROVIDER_ORG_LDAP_SETTINGS_PATH),
    ("PUT", _paths.PROVIDER_ORG_LDAP_SETTINGS_PATH),
    ("GET", _paths.PROVIDER_ORG_OAUTH_SETTINGS_PATH),
    ("PUT", _paths.PROVIDER_ORG_OAUTH_SETTINGS_PATH),
    ("DELETE", _paths.PROVIDER_ORG_OAUTH_SETTINGS_PATH),
    ("POST", _paths.PROVIDER_ORG_OIDC_PROVIDER_CONFIG_PATH),
)

_PROVIDER_LISTS = (
    (
        "vcf-automation-9.0-reference",
        "vcf-automation-9.0-provider-infrastructure-apis-reference-operations.tsv",
    ),
    (
        "vcf-automation-9.1",
        "vcf-automation-9.1-provider-infrastructure-apis-reference-operations.tsv",
    ),
    (
        "vcf-automation-9.1.1",
        "vcf-automation-9.1.1-provider-infrastructure-apis-reference-operations.tsv",
    ),
)
_CLASSIC_LIST = (
    "vmware-cloud-director-10.6",
    "api-39.1/vmware-cloud-director-api-39.1-reference-operations.tsv",
)


def _listed_calls(list_path: Path) -> set[tuple[str, tuple[str, ...]]]:
    lines = list_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split("\t")[:2] != ["method", "path"]:
        pytest.fail(f"{list_path.name}: expected a header starting with 'method<TAB>path'.")
    calls = {
        (cols[0].upper(), tuple(cols[1].strip("/").split("/")))
        for cols in (line.split("\t") for line in lines[1:])
        if len(cols) >= 2
    }
    if not calls:
        pytest.fail(f"{list_path.name}: read zero calls; the list layout moved.")
    return calls


def _segment_matches(ours: str, listed: str) -> bool:
    return ours == listed or ours.startswith("{")


def _is_listed(method: str, path: str, listed: set[tuple[str, tuple[str, ...]]]) -> bool:
    ours = path.strip("/").split("/")
    return any(
        method == listed_method
        and len(segments) == len(ours)
        and all(_segment_matches(a, b) for a, b in zip(ours, segments, strict=True))
        for listed_method, segments in listed
    )


def test_identity_paths_are_pinned() -> None:
    """Every sign-in path constant is in one of the two call lists above."""
    names = {
        name
        for name in vars(_paths)
        if name.endswith("_PATH")
        and name.startswith(("PROVIDER_LDAP", "PROVIDER_ORG_", "PROVIDER_TEST", "PROVIDER_TRUST"))
    }
    covered = {path for _method, path in (*_CLOUDAPI_CALLS, *_CLASSIC_CALLS)}
    assert {getattr(_paths, name) for name in names} == covered


@pytest.mark.parametrize(("spec_dir", "list_file"), _PROVIDER_LISTS, ids=["9.0", "9.1", "9.1.1"])
def test_cloudapi_calls_are_in_each_provider_list(spec_dir: str, list_file: str) -> None:
    listed = _listed_calls(require_shelf_spec(spec_dir, list_file))
    missing = [f"{m} {p}" for m, p in _CLOUDAPI_CALLS if not _is_listed(m, p, listed)]
    assert not missing, f"not in {spec_dir}/{list_file}: {missing}"


def test_classic_calls_are_in_the_cloud_director_list() -> None:
    listed = _listed_calls(require_shelf_spec(*_CLASSIC_LIST))
    missing = [
        f"{m} {p}" for m, p in _CLASSIC_CALLS if not _is_listed(m, p.removeprefix("/api"), listed)
    ]
    assert not missing, f"not in {'/'.join(_CLASSIC_LIST)}: {missing}"
