# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cloud Builder reconcile lane: hand-coded calls vs the 5.x reference lists.

**Evidenced exclusion — no API file.** Broadcom publishes no OpenAPI or
Swagger file for Cloud Builder 5.0, 5.1 or 5.2 (the "Related Resources" page of
each version offers no download; ``vmware/vcf-api-specs`` has 9.x tags only).
The calls are documented on the vendor's API reference pages, section "Sddc"
of the VMware Cloud Foundation API reference, for example
https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/5.2.0/operation-index/ .

So this lane has two parts:

* the manifest pin (always runs): the exact ``METHOD:/path`` set the
  connector sends, swept from its live constants. A new or changed call must
  update the manifest on purpose;
* the reference lists (runs when the spec shelf is there; CI has it): the
  shelf pins one list per 5.x version, made from the vendor's reference
  pages (``method<TAB>path<TAB>...``). Every call must be in every list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meho_backplane.connectors.vcf_cloud_builder.typed_reads import ABOUT_PATH, READ_CALLS
from meho_backplane.connectors.vcf_cloud_builder.typed_writes import WRITE_CALLS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

#: (shelf directory, reference list) per Cloud Builder version.
_REFERENCE_LISTS = (
    ("cloud-builder-5.0", "cloud-builder-5.0.0-reference-operations.tsv"),
    ("cloud-builder-5.1", "cloud-builder-5.1.0-reference-operations.tsv"),
    ("cloud-builder-5.1", "cloud-builder-5.1.1-reference-operations.tsv"),
    ("cloud-builder-5.2", "cloud-builder-5.2.0-reference-operations.tsv"),
    ("cloud-builder-5.2", "cloud-builder-5.2.1-reference-operations.tsv"),
    ("cloud-builder-5.2", "cloud-builder-5.2.2-reference-operations.tsv"),
    ("cloud-builder-5.2", "cloud-builder-5.2.3-reference-operations.tsv"),
    ("cloud-builder-5.2", "cloud-builder-5.2.4-reference-operations.tsv"),
)

_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _normalise(op_id: str) -> str:
    return _PLACEHOLDER.sub("{}", op_id)


def _declared_op_ids() -> set[str]:
    return set(READ_CALLS | WRITE_CALLS)


def _reference_op_ids(list_path: Path, label: str) -> set[str]:
    lines = list_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split("\t")[:2] != ["method", "path"]:
        pytest.fail(f"{label}: expected a TSV header starting with 'method<TAB>path'.")
    served = {
        _normalise(f"{cols[0].upper()}:{cols[1]}")
        for cols in (line.split("\t") for line in lines[1:])
        if len(cols) >= 2 and cols[1].startswith("/")
    }
    if not served:
        pytest.fail(f"{label}: read zero operations; the list layout moved.")
    return served


def test_cloud_builder_call_manifest_is_pinned() -> None:
    assert _declared_op_ids() == {
        "GET:/v1/sddcs/about",
        "GET:/v1/sddcs/validations",
        "POST:/v1/sddcs/validations",
        "GET:/v1/sddcs/validations/{id}",
        "GET:/v1/sddcs/validations/{validationId}/report",
        "GET:/v1/sddcs",
        "POST:/v1/sddcs",
        "GET:/v1/sddcs/{id}",
        "PATCH:/v1/sddcs/{id}",
        "GET:/v1/sddcs/{id}/detail-report",
    }


def test_version_detection_uses_the_about_call() -> None:
    assert f"GET:{ABOUT_PATH}" in READ_CALLS


@pytest.mark.parametrize(("spec_dir", "list_file"), _REFERENCE_LISTS, ids=lambda v: str(v))
def test_cloud_builder_calls_are_in_the_reference_list_of_each_version(
    spec_dir: str, list_file: str
) -> None:
    list_path = require_shelf_spec(spec_dir, list_file)
    label = f"{spec_dir}/{list_file}"
    served = _reference_op_ids(list_path, label)
    declared = {_normalise(op_id) for op_id in _declared_op_ids()}
    assert_op_ids_served(declared, served, spec_label=label)
