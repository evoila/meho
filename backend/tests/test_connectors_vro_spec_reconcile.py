# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vro spec-reconcile lane -- hand-coded paths vs the pinned Orchestrator reference lists.

Every vendor call of the Orchestrator reads (``typed_reads.VENDOR_CALLS``)
is checked against the reference list of each pinned version on the spec
shelf. The vendor publishes no API file for the Orchestrator REST API, so
the shelf pins a reference list made from the vendor's reference pages:
one line per operation, ``method<TAB>path<TAB>...``.

Two differences between the lists and MEHO's paths are normalised:

* placeholder names: the pages write ``/workflows/{id}``, MEHO writes
  ``/workflows/{workflowId}``; the compare treats every ``{...}`` alike;
* the 9.1 pages write every path without the ``/vco/api`` start (the
  9.0 pages and the 9.1 page texts include it), so the 9.1 list is read
  with ``/vco/api`` put in front.

The login calls of the ``automation`` login mode are Aria Automation
calls; the vra8 reconcile lane checks them. No reference exists for
Orchestrator 8.18 or 9.1.1 yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meho_backplane.connectors.vro import typed_reads as _typed_reads_module
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

#: (shelf directory, reference list, prefix the list leaves out).
_REFERENCE_LISTS = (
    (
        "vcf-operations-orchestrator-9.0",
        "vcf-operations-orchestrator-9.0-reference-operations.tsv",
        "",
    ),
    (
        "vcf-operations-orchestrator-9.1",
        "vcf-operations-orchestrator-9.1-reference-operations.tsv",
        "/vco/api",
    ),
)

_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _normalise(op_id: str) -> str:
    return _PLACEHOLDER.sub("{}", op_id)


def _declared_op_ids() -> set[str]:
    return {f"{method}:{path}" for method, path in _typed_reads_module.VENDOR_CALLS}


def _reference_op_ids(list_path: Path, label: str, prefix: str) -> set[str]:
    """Read the normalised ``METHOD:/path`` set of a reference list (TSV)."""
    lines = list_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split("\t")[:2] != ["method", "path"]:
        pytest.fail(f"{label}: expected a TSV header starting with 'method<TAB>path'.")
    served = {
        _normalise(f"{cols[0].upper()}:{prefix}{cols[1]}")
        for cols in (line.split("\t") for line in lines[1:])
        if len(cols) >= 2 and cols[1].startswith("/")
    }
    if not served:
        pytest.fail(f"{label}: read zero operations; the list layout moved.")
    return served


def test_vro_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift cannot silently shrink the reconcile."""
    assert _declared_op_ids() == {
        "GET:/vco/api/about",
        "GET:/vco/api/healthstatus",
        "GET:/vco/api/workflows",
        "GET:/vco/api/workflows/{workflowId}",
        "GET:/vco/api/workflows/{workflowId}/content",
        "GET:/vco/api/workflows/{workflowId}/executions",
        "GET:/vco/api/workflows/{workflowId}/executions/{executionId}",
        "GET:/vco/api/workflows/{workflowId}/executions/{executionId}/syslogs",
    }


@pytest.mark.parametrize(
    ("spec_dir", "list_file", "prefix"),
    _REFERENCE_LISTS,
    ids=[spec_dir for spec_dir, _, _ in _REFERENCE_LISTS],
)
def test_vro_paths_are_in_the_reference_list_of_each_version(
    spec_dir: str, list_file: str, prefix: str
) -> None:
    """Every hand-coded Orchestrator call is in the version's reference list."""
    list_path = require_shelf_spec(spec_dir, list_file)
    label = f"{spec_dir}/{list_file}"
    served = _reference_op_ids(list_path, label, prefix)
    declared = {_normalise(op_id) for op_id in _declared_op_ids()}
    assert_op_ids_served(declared, served, spec_label=label)
