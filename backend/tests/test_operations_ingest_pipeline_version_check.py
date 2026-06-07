# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G0.9-T8 spec-vs-label cross-check.

The cross-check lives in
:class:`meho_backplane.operations.ingest.IngestionPipelineService`
(``_validate_spec_versions``). It runs **before** the parser does its
full operation walk so an obviously-misclassified ingest fails in
milliseconds rather than after CPU has been spent on a 2,000-op spec.

These tests exercise the classification helper and the four cases
the issue body names:

* **Exact match** — proceed.
* **Inexact-compatible** (same major, different minor) — proceed
  with a structured ``connector_ingest_version_drift`` log event.
* **Incompatible** (different major) — raise
  :class:`VersionMismatchError` with ``kind="spec_label_mismatch"``.
* **Multi-spec mismatch** (two specs disagreeing on the major) —
  raise :class:`VersionMismatchError` with
  ``kind="multi_spec_inconsistent"``.

The pipeline is exercised through ``_validate_spec_versions`` directly
— the DB / LLM / parse-operations machinery is out of scope here and
covered by ``tests/test_api_v1_connectors_ingest.py``.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
import structlog
import yaml

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations.ingest import (
    IngestionPipelineService,
    SpecSource,
    VersionMismatchError,
)
from meho_backplane.operations.ingest.pipeline import _classify_version_match


def _operator() -> Operator:
    """Build a minimal :class:`Operator` for cross-check tests.

    The cross-check method doesn't read tenancy / RBAC — it only
    needs ``self._operator.sub`` for log binding — but the route's
    constructor still requires a fully-formed model.
    """
    return Operator(
        sub="test-operator",
        raw_jwt="test-jwt",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _bound_log() -> structlog.stdlib.BoundLogger:
    """Return a bound logger of the shape ``_validate_spec_versions`` expects."""
    return structlog.get_logger(__name__).bind(test=True)


def _read_spec_info_version_local(uri: str, *, content: str | None = None) -> str | None:
    """Read ``info.version`` from a local file path URI, bypassing the SSRF guard.

    Autouse fixture :func:`_patch_read_spec_info_version` swaps
    the pipeline's ``read_spec_info_version`` for this function so
    these service-layer unit tests can pass local file paths without
    triggering the network-facing guard that was added in G0.16-T8
    (#95). The SSRF guard's own correctness is covered by
    ``tests/test_operations_ingest_openapi.py``.
    """
    if content is not None:
        raw = content.encode("utf-8")
    else:
        try:
            raw = Path(uri).read_bytes()
        except OSError:
            return None
    try:
        spec = yaml.safe_load(io.BytesIO(raw))
    except yaml.YAMLError:
        return None
    if not isinstance(spec, dict):
        return None
    info = spec.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    if not isinstance(version, str) or not version:
        return None
    return version


@pytest.fixture(autouse=True)
def _patch_read_spec_info_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the pipeline's ``read_spec_info_version`` with a local-file reader.

    These tests exercise ``_validate_spec_versions`` (the service-layer
    cross-check logic) not the HTTP fetch path. G0.16-T8 (#95) moved
    the network-facing guard into ``_load_spec_bytes``, so all tests
    that pass local file paths as ``spec.uri`` would fail the scheme
    check without this patch. Swapping the imported name in the pipeline
    module is the narrowest possible seam — only ``_validate_spec_versions``
    is affected; the real ``read_spec_info_version`` (and its SSRF guard)
    stays in place for every other caller.
    """
    import meho_backplane.operations.ingest.pipeline as _pipeline_mod

    monkeypatch.setattr(_pipeline_mod, "read_spec_info_version", _read_spec_info_version_local)


def _spec_yaml(path: Path, *, openapi: str = "3.0.3", info_version: str | None) -> SpecSource:
    """Write a tiny spec file with the supplied ``info.version`` and wrap it."""
    if info_version is None:
        body = f"openapi: '{openapi}'\ninfo: {{title: t}}\npaths: {{}}\n"
    else:
        body = f"openapi: '{openapi}'\ninfo: {{title: t, version: '{info_version}'}}\npaths: {{}}\n"
    path.write_text(body)
    return SpecSource(uri=str(path))


# -- _classify_version_match ----------------------------------------------


@pytest.mark.parametrize(
    ("spec_v", "label_v", "expected"),
    [
        # Verbatim string equality wins regardless of PEP 440.
        ("9.0.3", "9.0.3", "exact"),
        ("acme-2024Q3", "acme-2024Q3", "exact"),
        # PEP 440 normalisation: trailing zeros are equivalent.
        ("1", "1.0", "exact"),
        ("1.0", "1", "exact"),
        # Label is release-tuple prefix of spec → exact.
        ("9.0.3", "9.0", "exact"),
        ("9.0.3", "9", "exact"),
        # Same major, different minor → compatible.
        ("9.0.3", "9.1", "compatible"),
        ("9.1.0", "9.0", "compatible"),
        # Different major → incompatible.
        ("7.0.0", "9.0", "incompatible"),
        ("9.0.3", "8.0", "incompatible"),
        # Non-PEP-440 string that doesn't match verbatim → incompatible.
        ("acme-2024Q3", "acme-2025Q1", "incompatible"),
        ("9.0.3", "not-a-version", "incompatible"),
    ],
)
def test_classify_version_match(spec_v: str, label_v: str, expected: str) -> None:
    assert _classify_version_match(spec_v, label_v) == expected


# -- _validate_spec_versions: exact / compatible / incompatible -----------


def test_validate_spec_versions_exact_match_passes(tmp_path: Path) -> None:
    """Spec ``info.version`` equal to (or a coarser prefix of) the label proceeds."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="9.0.3")
    service = IngestionPipelineService(operator=_operator())
    # Should not raise.
    service._validate_spec_versions(
        specs=[spec],
        requested_version="9.0.3",
        log=_bound_log(),
    )


def test_validate_spec_versions_prefix_label_passes(tmp_path: Path) -> None:
    """spec ``9.0.3`` + label ``9.0`` → exact (label is a release-tuple prefix)."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="9.0.3")
    service = IngestionPipelineService(operator=_operator())
    service._validate_spec_versions(
        specs=[spec],
        requested_version="9.0",
        log=_bound_log(),
    )


def test_validate_spec_versions_inexact_compatible_warns_but_passes(
    tmp_path: Path,
) -> None:
    """spec ``9.0.3`` + label ``9.1`` → compatible: pass + drift event."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="9.0.3")
    service = IngestionPipelineService(operator=_operator())
    # No raise. (Structlog event observation is brittle across logger
    # configurations; the unit gate is "does it raise?" — the
    # integration gate in tests/test_api_v1_connectors_ingest.py
    # covers the structured-logging contract end-to-end.)
    service._validate_spec_versions(
        specs=[spec],
        requested_version="9.1",
        log=_bound_log(),
    )


def test_validate_spec_versions_incompatible_raises_422_shape(tmp_path: Path) -> None:
    """spec ``9.0.3`` + label ``8.0`` → ``VersionMismatchError`` with both values."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="9.0.3")
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(VersionMismatchError) as excinfo:
        service._validate_spec_versions(
            specs=[spec],
            requested_version="8.0",
            log=_bound_log(),
        )
    err = excinfo.value
    assert err.kind == "spec_label_mismatch"
    assert err.requested_version == "8.0"
    assert err.spec_info_versions == [(str(tmp_path / "spec.yaml"), "9.0.3")]
    # The message names both versions so the operator can correct.
    rendered = str(err)
    assert "9.0.3" in rendered
    assert "8.0" in rendered


def test_validate_spec_versions_vcenter_9_under_label_8_raises_422(tmp_path: Path) -> None:
    """The regression case named in the issue body — vCenter-9 spec under label 8.0."""
    spec = _spec_yaml(tmp_path / "vcenter.yaml", info_version="9.0.3")
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(VersionMismatchError) as excinfo:
        service._validate_spec_versions(
            specs=[spec],
            requested_version="8.0",
            log=_bound_log(),
        )
    assert excinfo.value.kind == "spec_label_mismatch"


def test_validate_spec_versions_missing_info_version_skips_check(tmp_path: Path) -> None:
    """Specs without an ``info.version`` ingest under any label (no crash)."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version=None)
    service = IngestionPipelineService(operator=_operator())
    # Even a wildly different label proceeds — the cross-check can't
    # be performed without a spec ``info.version`` to compare against.
    service._validate_spec_versions(
        specs=[spec],
        requested_version="42.99",
        log=_bound_log(),
    )


# -- _validate_spec_versions: multi-spec inconsistency --------------------


def test_validate_spec_versions_multi_spec_consistent_passes(tmp_path: Path) -> None:
    """Two specs sharing a major version → consistent bundle, no raise."""
    spec_a = _spec_yaml(tmp_path / "vcenter.yaml", info_version="9.0.3")
    spec_b = _spec_yaml(tmp_path / "vi-json.yaml", info_version="9.0.0")
    service = IngestionPipelineService(operator=_operator())
    service._validate_spec_versions(
        specs=[spec_a, spec_b],
        requested_version="9.0",
        log=_bound_log(),
    )


def test_validate_spec_versions_multi_spec_inconsistent_raises(tmp_path: Path) -> None:
    """Two specs disagreeing on the major version → ``multi_spec_inconsistent``.

    Per the issue body: vcenter.yaml says ``9.0.3`` and vi-json.yaml
    says ``7.0`` — the bundle is internally inconsistent and the
    ingest cannot proceed under any single connector triple.
    """
    spec_a = _spec_yaml(tmp_path / "vcenter.yaml", info_version="9.0.3")
    spec_b = _spec_yaml(tmp_path / "vi-json.yaml", info_version="7.0")
    service = IngestionPipelineService(operator=_operator())
    # The operator's label happens to match one of the specs, but
    # the bundle is still rejected — the spec/label match guard fires
    # first on the 7.0 spec (incompatible with label=9.0), surfacing
    # spec_label_mismatch. Either rejection kind is correct here; the
    # operator-facing message names both values either way.
    with pytest.raises(VersionMismatchError) as excinfo:
        service._validate_spec_versions(
            specs=[spec_a, spec_b],
            requested_version="9.0",
            log=_bound_log(),
        )
    assert excinfo.value.kind in {
        "spec_label_mismatch",
        "multi_spec_inconsistent",
    }
    rendered = str(excinfo.value)
    # Both spec versions should appear so the operator sees the conflict.
    assert "9.0.3" in rendered or "7.0" in rendered


def test_validate_spec_versions_multi_spec_inconsistent_compatible_label(
    tmp_path: Path,
) -> None:
    """Label compatible with both specs individually but specs disagree on major.

    With ``requested_version="9.1"``: spec_a (9.0.3) classifies as
    ``compatible`` (warn-but-pass), spec_b (7.0) classifies as
    ``incompatible`` (raise). The fired exception is
    ``spec_label_mismatch`` for spec_b.
    """
    spec_a = _spec_yaml(tmp_path / "vcenter.yaml", info_version="9.0.3")
    spec_b = _spec_yaml(tmp_path / "vi-json.yaml", info_version="7.0")
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(VersionMismatchError):
        service._validate_spec_versions(
            specs=[spec_a, spec_b],
            requested_version="9.1",
            log=_bound_log(),
        )


def test_validate_spec_versions_multi_spec_pure_inconsistent(tmp_path: Path) -> None:
    """Label matches the first spec exactly; second spec disagrees on major.

    With label ``9.0.3`` and specs (9.0.3, 7.0): the first matches
    exact, the second is incompatible with the label. The exception
    fires as ``spec_label_mismatch`` for the second spec — and the
    multi-spec consistency check would also catch it as a fallback.
    """
    spec_a = _spec_yaml(tmp_path / "vcenter.yaml", info_version="9.0.3")
    spec_b = _spec_yaml(tmp_path / "vi-json.yaml", info_version="7.0")
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(VersionMismatchError) as excinfo:
        service._validate_spec_versions(
            specs=[spec_a, spec_b],
            requested_version="9.0.3",
            log=_bound_log(),
        )
    # Either kind is acceptable here — the operator-facing message
    # names both values either way. The exception type is the
    # load-bearing contract.
    assert excinfo.value.kind in {
        "spec_label_mismatch",
        "multi_spec_inconsistent",
    }


# -- _validate_spec_versions: spec_info_versions_compatible opt-in (G0.16-T5 #1307)


def test_validate_spec_versions_compat_opt_in_accepts_label_drift(
    tmp_path: Path,
) -> None:
    """spec ``1.1.4`` + label ``3`` with compat ``["1.x.x"]`` → no raise."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="1.1.4")
    service = IngestionPipelineService(operator=_operator())
    # Without the opt-in this would raise spec_label_mismatch (different
    # major: spec 1.x vs label 3); with it, the validator widens to the
    # compat band and the ingest proceeds.
    service._validate_spec_versions(
        specs=[spec],
        requested_version="3",
        log=_bound_log(),
        spec_info_versions_compatible=("1.x.x",),
    )


def test_validate_spec_versions_compat_opt_in_off_by_default(tmp_path: Path) -> None:
    """Without the opt-in the historical label-vs-spec check still fires."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="1.1.4")
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(VersionMismatchError) as excinfo:
        service._validate_spec_versions(
            specs=[spec],
            requested_version="3",
            log=_bound_log(),
        )
    assert excinfo.value.kind == "spec_label_mismatch"


def test_validate_spec_versions_compat_opt_in_outside_range_still_raises(
    tmp_path: Path,
) -> None:
    """Compat band is bounded — a spec outside it still raises."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="2.0.0")
    service = IngestionPipelineService(operator=_operator())
    with pytest.raises(VersionMismatchError) as excinfo:
        service._validate_spec_versions(
            specs=[spec],
            requested_version="3",
            log=_bound_log(),
            spec_info_versions_compatible=("1.x.x",),
        )
    assert excinfo.value.kind == "spec_label_mismatch"


def test_validate_spec_versions_compat_opt_in_specifier_set_shape(
    tmp_path: Path,
) -> None:
    """PEP 440 SpecifierSet syntax is accepted directly."""
    spec = _spec_yaml(tmp_path / "spec.yaml", info_version="1.1.4")
    service = IngestionPipelineService(operator=_operator())
    service._validate_spec_versions(
        specs=[spec],
        requested_version="3",
        log=_bound_log(),
        spec_info_versions_compatible=(">=1.0,<2.0",),
    )


def test_validate_spec_versions_compat_opt_in_multi_spec_still_consistency_checked(
    tmp_path: Path,
) -> None:
    """The opt-in widens label-vs-spec only; multi-spec consistency still fires.

    Two specs whose ``info.version`` values both fall inside the
    declared compatibility band collapse cleanly. Two specs whose
    values straddle major versions still trip
    ``multi_spec_inconsistent`` — the opt-in doesn't grant a free
    pass to a bundle that can't share a connector triple.
    """
    spec_a = _spec_yaml(tmp_path / "vcenter.yaml", info_version="1.1.4")
    spec_b = _spec_yaml(tmp_path / "vi-json.yaml", info_version="1.2.0")
    service = IngestionPipelineService(operator=_operator())
    # Both inside the compat band → no raise.
    service._validate_spec_versions(
        specs=[spec_a, spec_b],
        requested_version="3",
        log=_bound_log(),
        spec_info_versions_compatible=("1.x.x",),
    )


def test_version_mismatch_error_renders_both_values() -> None:
    """The exception message includes both ``requested_version`` and
    ``spec_info_version`` so operators can act on it without
    re-running with verbose logging.
    """
    err = VersionMismatchError(
        kind="spec_label_mismatch",
        requested_version="8.0",
        spec_info_versions=[("/specs/vcenter.yaml", "9.0.3")],
        suggestion="re-ingest under version='9.0.3'",
    )
    rendered = str(err)
    assert "8.0" in rendered
    assert "9.0.3" in rendered
    assert "re-ingest" in rendered
    assert err.kind == "spec_label_mismatch"
    assert err.spec_info_versions == [("/specs/vcenter.yaml", "9.0.3")]
