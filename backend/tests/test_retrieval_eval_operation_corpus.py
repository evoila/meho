# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the operations eval corpus (G4.3-T3, Task #442).

Coverage matrix:

* ``load_corpus("operations")`` returns 10 ``OperationCorpusQuery``
  rows from the shipped ``operation_queries.yaml`` — acceptance #1 + #3
  + #5.
* Every row carries ``expected_connector_id="vmware-rest-9.0"`` — the
  vSphere REST surface the v0.2 corpus targets exclusively (vi-json
  + composites not yet ingested per the canary's findings).
* Every row carries a populated ``govc_equivalent`` — acceptance #2.
  The corpus's load-bearing pre-MEHO operator baseline; T2's runner
  can't infer it.
* Every ``expected_op_ids`` entry is present in a snapshot of vcenter
  spec op_ids at corpus-authoring time — slug-existence guard mirroring
  T1's ``test_kb_corpus_slugs_align_with_consumer_kb_snapshot``.
* The path-family coverage spans ≥5 distinct families — regression
  detection so the corpus can't silently drift into one product area.
"""

from __future__ import annotations

from meho_backplane.retrieval.eval.corpus import (
    OperationCorpusQuery,
    load_corpus,
)

# ---------------------------------------------------------------------------
# Snapshot of vcenter.yaml op_ids referenced by the operations corpus.
#
# Holds every op_id the shipped ``operation_queries.yaml`` references
# (NOT the full ~1275-op vcenter.yaml inventory). The narrow snapshot
# is intentional: T3 ships ground truth for 10 queries, and the eval's
# guarantee is "these specific op_ids exist". Drift on an unreferenced
# op is irrelevant to this corpus; drift on a referenced op (spec
# rename or version bump) must surface as a test failure here so the
# YAML + snapshot update together in one PR.
#
# Source of truth at authoring time (2026-05-15): the canary's
# ``GOVC_PARITY_BENCHMARK`` tuple in
# ``backend/tests/acceptance/test_g07_vsphere_canary.py`` —
# acceptance criterion 4 of #442 ties the two lists together.
# ---------------------------------------------------------------------------

VCENTER_OP_ID_SNAPSHOT_2026_05: frozenset[str] = frozenset(
    {
        "GET:/vcenter/vm",
        "GET:/vcenter/cluster",
        "GET:/vcenter/datacenter",
        "GET:/vcenter/datastore",
        "GET:/vcenter/network",
        "GET:/vcenter/host",
        "GET:/vcenter/vm/{vm}",
        "POST:/vcenter/vm/{vm}/power?action=start",
        "POST:/vcenter/vm/{vm}/power?action=stop",
        "POST:/session",
    }
)

#: Connector identifier every row in the v0.2 operations corpus must
#: target. vi-json + pyvmomi impls don't exist yet (parser gap + G3.1
#: not started); when they land the constraint becomes per-row instead
#: of corpus-wide.
EXPECTED_CONNECTOR_ID: str = "vmware-rest-9.0"


# ---------------------------------------------------------------------------
# Loader behaviour
# ---------------------------------------------------------------------------


def test_load_corpus_operations_returns_ten_typed_rows() -> None:
    """Acceptance #1 + #3: operations corpus loads 10 ``OperationCorpusQuery`` rows."""
    rows = load_corpus("operations")

    assert len(rows) == 10
    assert all(isinstance(row, OperationCorpusQuery) for row in rows)
    for row in rows:
        assert row.query.strip(), f"empty query: {row}"
        assert row.expected_op_ids, f"empty expected_op_ids: {row.query}"


def test_operations_corpus_targets_vmware_rest_9_0() -> None:
    """Every row in the v0.2 corpus targets the vSphere REST connector.

    vi-json + pyvmomi impls don't exist yet (Finding 1 of canary #408;
    G3.1 not started). When those land, drop or relax this assertion
    in the same PR that adds vi-json / composite ground-truth queries.
    """
    rows = load_corpus("operations")

    for row in rows:
        assert row.expected_connector_id == EXPECTED_CONNECTOR_ID, (
            f"row {row.query!r} targets {row.expected_connector_id!r}; "
            f"v0.2 corpus expects {EXPECTED_CONNECTOR_ID!r}"
        )


def test_operations_corpus_every_row_has_govc_equivalent() -> None:
    """Acceptance #2: every row maps to a govc workflow.

    The corpus's load-bearing pre-MEHO operator baseline. T2's eval
    runner can't infer it; the corpus carries it so a future
    ``meho retrieval eval --baseline govc`` mode (the operations
    analogue of the kb grep baseline) can compare against the
    operator's actual fallback CLI without re-deriving it.
    """
    rows = load_corpus("operations")

    missing = [row.query for row in rows if not row.govc_equivalent]
    assert not missing, f"rows without govc_equivalent: {missing}"


def test_operations_corpus_op_ids_present_in_vcenter_snapshot() -> None:
    """Every expected_op_ids entry is a real vcenter.yaml op_id.

    Mirrors T1's ``test_kb_corpus_slugs_align_with_consumer_kb_snapshot``
    — a spec rename that breaks this list flags the corpus as drifted,
    which is the regression-detection signal we want.
    """
    rows = load_corpus("operations")

    referenced = {op_id for row in rows for op_id in row.expected_op_ids}
    missing = referenced - VCENTER_OP_ID_SNAPSHOT_2026_05

    assert not missing, (
        f"corpus references op_ids absent from the vcenter.yaml snapshot: "
        f"{sorted(missing)}. Either the spec renamed an op (update the YAML "
        f"+ this snapshot in the same PR) or the op_id was a typo."
    )


def test_operations_corpus_covers_multiple_path_families() -> None:
    """Path-family mix is the regression-detection property of the corpus.

    Mirrors T1's per-product mix test. Encodes the Initiative #373
    "covers inventory + VM + cluster + datastore + network + host …"
    intent as a structural check: the union of expected op_ids must
    touch ≥5 distinct path-family prefixes (the segment after
    ``METHOD:/vcenter/``) so a regression in any one vSphere surface
    surfaces because at least one query targets it.

    The check is over expected op_ids, not query strings, so a
    rephrased query that maps to the same op_id doesn't inflate
    family count.
    """
    rows = load_corpus("operations")

    families: set[str] = set()
    for row in rows:
        for op_id in row.expected_op_ids:
            # op_id shape: "METHOD:/path[?query]". Extract the first
            # path segment after "/vcenter/" (or the path root for
            # non-vcenter ops like "POST:/session").
            _method, _, path = op_id.partition(":")
            stripped = path.lstrip("/")
            if stripped.startswith("vcenter/"):
                family = stripped.removeprefix("vcenter/").split("/", 1)[0]
            else:
                family = stripped.split("/", 1)[0]
            families.add(family)

    assert len(families) >= 5, (
        f"corpus only covers {len(families)} path families ({sorted(families)}); "
        f"expected ≥5 to catch per-surface retrieval regressions"
    )
