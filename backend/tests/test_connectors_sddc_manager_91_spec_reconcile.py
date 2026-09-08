# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""sddc-manager **9.1** write-path real-spec reconcile lane (#3497) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the connector's curated
workload-domain **write** ops dispatch is served by the pinned
``sddc-manager-9.1`` spec on the shelf (``vmware/vcf-api-specs``' OpenAPI
3.0.1 document at ``@3949fc33`` — Apache-2.0, provenance in the shelf's
``MANIFEST.md``). Parse-only set compare — no DB, no embeddings, no
containers — so it lives in the required unit sweep per
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when the
shelf is unconfigured (``tests/_spec_shelf.py`` contract).

Why a **second, 9.1** lane rather than folding the writes into the 9.0
lane. A 9.1 target resolves to the same ``sddc-rest-9.0`` connector (range
``>=9.0,<10.0``; no path was removed 9.0 → 9.1, so 9.0-era call sites keep
resolving), and the Envision estate the write path is authored for runs
9.1. The reads stay guarded against ``sddc-manager-9.0`` by the sibling
lane (:mod:`tests.test_connectors_sddc_manager_spec_reconcile`); the new
write path constants are guarded here against the pinned 9.1 spec. The
declared set is introspected from the connector's live constants (the
#2944 pattern — never a hardcoded mirror):

* :mod:`~meho_backplane.connectors.sddc_manager.typed_writes` — the WLD
  submit primitives' ``TYPED_WRITE_DECLARED_OP_IDS`` (``POST /v1/network-pools``,
  ``POST /v1/hosts/validations``, ``POST /v1/hosts``,
  ``POST /v1/domains/validations``, ``POST /v1/domains``), each carrying its
  own HTTP method.

A red lane here is the guard surfacing a real finding (a fictional or
renamed write path), not harness noise.
"""

from __future__ import annotations

from meho_backplane.connectors.sddc_manager import typed_writes as _typed_writes
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "sddc-manager-9.1"
_SPEC_FILE = "sddc-manager-openapi.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the WLD write ops dispatch."""
    return set(_typed_writes.TYPED_WRITE_DECLARED_OP_IDS)


def test_typed_write_declared_op_ids_are_pinned() -> None:
    """Guard: the WLD write declared set can't go vacuous.

    Pinning the exact ``METHOD:/path`` set means dropping or renaming a write
    path constant can't silently shrink the reconciled set to a passing subset.
    """
    assert (
        frozenset(
            {
                "POST:/v1/network-pools",
                "POST:/v1/hosts/validations",
                "POST:/v1/hosts",
                "POST:/v1/domains/validations",
                "POST:/v1/domains",
            }
        )
        == _typed_writes.TYPED_WRITE_DECLARED_OP_IDS
    )


def test_declared_write_op_ids_are_served_by_the_pinned_91_spec() -> None:
    """Every hand-coded write op_id is served by the pinned sddc-manager-9.1 spec."""
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
