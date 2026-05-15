# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.retrieval.eval.corpus`.

Coverage matrix (G4.3-T1 / Task #440 acceptance criteria):

* ``load_corpus("kb")`` returns 10 ``KbCorpusQuery`` rows from the
  shipped ``kb_queries.yaml`` — acceptance #1 + #5 (CI green).
* Pydantic validation rejects malformed YAML with a clear error
  message naming the bad entry — acceptance #2.
* Every shipped kb query's ``expected_hits`` slugs are present in
  the consumer ``kb/`` snapshot — acceptance #3 (slugs aren't
  invented).
* Surface filter works: ``load_corpus("memory")`` and
  ``load_corpus("operations")`` return ``[]`` because their YAML
  files don't ship in T1 — acceptance #4.
* Schema discipline: frozen models reject mutation; ``extra=forbid``
  rejects unknown YAML keys; strict mode rejects type coercion.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from meho_backplane.retrieval.eval.corpus import (
    CorpusValidationError,
    KbCorpusQuery,
    MemoryCorpusQuery,
    OperationCorpusQuery,
    load_corpus,
)

# ---------------------------------------------------------------------------
# Snapshot of the consumer's kb/ slugs at corpus authoring time.
#
# The snapshot is checked in beside this test rather than fetched at
# test time because the eval contract is "MEHO ranking against a
# *known* kb state". A kb update on the consumer side that adds new
# slugs without updating this snapshot does not break the eval (the
# corpus still references valid slugs); a slug rename that breaks
# this list flags the corpus as drifted, which is exactly the signal
# we want.
#
# Maintenance: when the consumer renames or removes a slug that this
# corpus targets, update both the snapshot and the corpus YAML in
# the same PR. The mismatch test below will fail until both sides
# agree.
# ---------------------------------------------------------------------------

CONSUMER_KB_SNAPSHOT_2026_05: frozenset[str] = frozenset(
    {
        "argocd-3.3-helm-oci-url-bug",
        "argocd-3.x-ssa-ignoredifferences-pattern",
        "argocd-3.x-traefik-h2c-cmux-ingress",
        "argocd-app-of-apps-self-manage-appprojects",
        "esxi-9.0-disk-operations",
        "esxi-9.0-esxcli",
        "esxi-9.0-vds-vmkernel-removal-cuts-vcenter",
        "esxi-pnic-rx-ring-tuning",
        "firecrawl-fetcher",
        "gcp-iam-disable-sa-key-creation",
        "gcs-workload-identity-from-private-rke2",
        "harbor-2.x-admin-password-rotation",
        "helm-3-release-secret-extraction",
        "hetzner-addon-procurement",
        "hetzner-colocation",
        "hetzner-host-onboarding-pattern",
        "hetzner-kvm-custom-image-install",
        "hetzner-robot-2026-04-mutation-surface",
        "hetzner-robot-2026-04-overview",
        "hetzner-robot-2026-04-readonly-surface",
        "hetzner-robot-2026-05-firewall-template",
        "hetzner-usage-portal",
        "hetzner-vswitch-mtu",
        "holodeck-9.0-clone-hardening",
        "holodeck-9.0-clone-procedure",
        "holodeck-9.0-deploy-runbook",
        "holodeck-9.0-known-issues",
        "holodeck-9.0-offline-depot",
        "holodeck-9.0-operator-reachability",
        "holodeck-9.0-overview",
        "holodeck-9.0-vcf-9.1-upgrade",
        "holodeck-9.0-vpn-access-registry",
        "kubectl-exec-stdin-piped-secret-leak",
        "nsx-9.0-overview",
        "op-cli-sshkey-field-leak",
        "pfsense-2.7-status",
        "pfsense-version-string-format",
        "rke2-canal-pod-mtu",
        "sddc-manager-9.0-overview",
        "vault-1.21-raft-snapshot",
        "vault-1.x-backup-restore",
        "vault-1.x-overview",
        "vcenter-8.0-overview",
        "vcenter-9.0-govc-coverage",
        "vcenter-9.0-overview",
        "vcf-9.0-in-ui-explorer-survey",
        "vcf-9.0-sso-password-policy",
        "vcf-automation-9.0-overview",
        "vcf-fleet-9.0-overview",
        "vcf-operations-9.0-overview",
        "vks-9.0-overview",
        "vmware-automation-surface-2026",
        "vsphere-9.0-host-detach-from-vds-via-api",
        "vsphere-sso-realm-conventions",
        "vsphere-vss-vds-portgroup-name-collision",
    }
)


# ---------------------------------------------------------------------------
# Loader behaviour
# ---------------------------------------------------------------------------


def test_load_corpus_kb_returns_ten_typed_rows() -> None:
    """Acceptance #1: kb seed corpus loads 10 ``KbCorpusQuery`` rows."""
    rows = load_corpus("kb")

    assert len(rows) == 10
    assert all(isinstance(row, KbCorpusQuery) for row in rows)
    # Every row must carry a non-empty query and at least one expected hit.
    for row in rows:
        assert row.query.strip(), f"empty query: {row}"
        assert row.expected_hits, f"empty expected_hits: {row.query}"


def test_load_corpus_memory_now_ships_after_t4() -> None:
    """T4 #443 shipped ``memory_queries.yaml``; ``load_corpus("memory")`` is non-empty.

    The deeper assertions (10 rows, ~2 queries per scope, slug regex
    alignment with the live ``SLUG_PATTERN``) live in
    ``test_retrieval_eval_memory_corpus.py``; this test guards the
    loader-level contract that the memory branch of the dispatch now
    returns ``MemoryCorpusQuery`` instances rather than ``[]``.
    """
    rows = load_corpus("memory")

    assert rows, "memory corpus is empty — did memory_queries.yaml ship?"
    assert all(isinstance(row, MemoryCorpusQuery) for row in rows)


def test_load_corpus_operations_now_ships_after_t3() -> None:
    """T3 #442 shipped ``operation_queries.yaml``; ``load_corpus("operations")`` is non-empty.

    The deeper assertions (10 rows, govc_equivalent populated, op_ids
    align with the vcenter snapshot) live in
    ``test_retrieval_eval_operation_corpus.py``; this test guards the
    loader-level contract that the operations branch of the dispatch
    now returns ``OperationCorpusQuery`` instances rather than ``[]``.
    """
    rows = load_corpus("operations")

    assert rows, "operations corpus is empty — did operation_queries.yaml ship?"
    assert all(isinstance(row, OperationCorpusQuery) for row in rows)


def test_kb_corpus_slugs_align_with_consumer_kb_snapshot() -> None:
    """Acceptance #3: every expected_hits slug exists in the consumer kb."""
    rows = load_corpus("kb")

    referenced = {slug for row in rows for slug in row.expected_hits}
    missing = referenced - CONSUMER_KB_SNAPSHOT_2026_05

    assert not missing, (
        f"corpus references slugs absent from the consumer kb snapshot: "
        f"{sorted(missing)}. Either the slug was renamed (update the YAML "
        f"+ this snapshot in the same PR) or the slug was a typo."
    )


def test_kb_corpus_covers_multiple_products() -> None:
    """Per-product mix is the regression-detection property of the corpus.

    Initiative #373 calls out: "10 queries cover the per-product mix
    of the consumer's kb/ ... so a regression in any one product
    surfaces". Encode that as a test: the union of expected slugs
    must touch at least 5 distinct product prefixes (the slug prefix
    before the first ``-``). Catches a corpus that drifts into one
    product because all the recent operator pain landed there.
    """
    rows = load_corpus("kb")

    prefixes = {slug.split("-", 1)[0] for row in rows for slug in row.expected_hits}
    assert len(prefixes) >= 5, (
        f"corpus only covers {len(prefixes)} product prefixes ({sorted(prefixes)}); "
        f"expected ≥5 to catch per-product retrieval regressions"
    )


# ---------------------------------------------------------------------------
# Schema discipline
# ---------------------------------------------------------------------------


def test_kb_corpus_query_is_frozen() -> None:
    """Loaded corpus rows must be immutable (no in-flight mutation during eval)."""
    row = KbCorpusQuery(query="q", expected_hits=["slug-a"])

    with pytest.raises(ValidationError):
        row.query = "modified"  # type: ignore[misc]


def test_kb_corpus_query_rejects_unknown_fields() -> None:
    """``extra=forbid`` catches the copy-paste-from-issue-body footgun."""
    with pytest.raises(ValidationError) as exc:
        KbCorpusQuery(
            query="q",
            expected_hits=["slug-a"],
            expected_slug="bad-key",  # type: ignore[call-arg]
        )

    # Pydantic's default error names the offending field.
    assert "expected_slug" in str(exc.value)


def test_kb_corpus_query_rejects_string_to_list_coercion() -> None:
    """``strict=True`` rejects YAML that hands a single slug as a string."""
    with pytest.raises(ValidationError):
        KbCorpusQuery.model_validate({"query": "q", "expected_hits": "slug-a"})


def test_memory_corpus_query_pair_typing() -> None:
    """Memory rows carry ``(scope, slug)`` pairs, not bare strings."""
    row = MemoryCorpusQuery(
        query="q",
        expected_hits=[("user", "slug-a"), ("tenant", "slug-b")],
    )

    assert row.expected_hits[0] == ("user", "slug-a")
    # A bare string list must fail validation — wrong shape for memory.
    with pytest.raises(ValidationError):
        MemoryCorpusQuery.model_validate({"query": "q", "expected_hits": ["slug-a"]})


def test_operation_corpus_query_optional_govc_default() -> None:
    """``govc_equivalent`` defaults to None — not every op has a govc analogue."""
    row = OperationCorpusQuery(
        query="q",
        expected_connector_id="vmware-rest-9.0",
        expected_op_ids=["GET:/api/vcenter/vm"],
    )

    assert row.govc_equivalent is None


# ---------------------------------------------------------------------------
# Failure surfacing
# ---------------------------------------------------------------------------


def test_load_corpus_raises_on_malformed_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #2: malformed YAML raises ``CorpusValidationError`` naming the file.

    The failure message must surface the surface name + filename so an
    operator running ``meho retrieval eval`` can find the bad entry
    without grepping the corpus.
    """
    bad = tmp_path / "kb_queries.yaml"
    # Invalid YAML: stray colon inside an unquoted value.
    bad.write_text("- query: q\n  expected_hits: [a, b: c]\n")

    monkeypatch.setattr(
        "meho_backplane.retrieval.eval.corpus.resources.files",
        lambda _pkg: _FakeRoot(tmp_path),
    )

    with pytest.raises(CorpusValidationError) as exc:
        load_corpus("kb")

    assert "kb" in str(exc.value)
    assert "kb_queries.yaml" in str(exc.value)


def test_load_corpus_raises_on_schema_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #2 (mirror): valid YAML / wrong schema also raises."""
    bad = tmp_path / "kb_queries.yaml"
    # Each entry uses ``expected_slug`` instead of ``expected_hits`` —
    # exactly the typo a copy-paste from the issue body would produce.
    bad.write_text("- query: how do I revert a snapshot\n  expected_slug: vcenter-9.0-snapshot\n")

    monkeypatch.setattr(
        "meho_backplane.retrieval.eval.corpus.resources.files",
        lambda _pkg: _FakeRoot(tmp_path),
    )

    with pytest.raises(CorpusValidationError) as exc:
        load_corpus("kb")

    msg = str(exc.value)
    assert "kb" in msg
    # Pydantic's default error message names the offending field path.
    assert "expected_slug" in msg or "expected_hits" in msg


def test_load_corpus_raises_on_top_level_non_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML file whose top-level isn't a list is a hard error."""
    bad = tmp_path / "kb_queries.yaml"
    bad.write_text("query: q\nexpected_hits: [a]\n")  # mapping at top level

    monkeypatch.setattr(
        "meho_backplane.retrieval.eval.corpus.resources.files",
        lambda _pkg: _FakeRoot(tmp_path),
    )

    with pytest.raises(CorpusValidationError) as exc:
        load_corpus("kb")

    assert "list" in str(exc.value).lower()


def test_load_corpus_treats_empty_file_as_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty YAML file (None after parse) → ``[]``, useful for stubbed surfaces."""
    bad = tmp_path / "memory_queries.yaml"
    bad.write_text("")

    monkeypatch.setattr(
        "meho_backplane.retrieval.eval.corpus.resources.files",
        lambda _pkg: _FakeRoot(tmp_path),
    )

    rows = load_corpus("memory")

    assert rows == []


def test_load_corpus_kb_missing_file_is_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kb corpus is mandatory in T1; missing YAML must raise loudly."""
    monkeypatch.setattr(
        "meho_backplane.retrieval.eval.corpus.resources.files",
        lambda _pkg: _FakeRoot(tmp_path),  # tmp_path has no kb_queries.yaml
    )

    with pytest.raises(CorpusValidationError) as exc:
        load_corpus("kb")

    assert "kb" in str(exc.value)
    assert "missing" in str(exc.value).lower() or "T1" in str(exc.value)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeRoot:
    """Stand-in for ``importlib.resources.files(package)`` in tests.

    The real return type is ``importlib.abc.Traversable``; for the
    loader's ``joinpath(filename).is_file() / read_text()`` surface
    a ``pathlib.Path`` matches the same protocol. This shim wraps a
    ``tmp_path`` so each test can write fake YAML files into a fresh
    directory and have the loader pick them up.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def joinpath(self, name: str) -> Path:
        return self._root / name
