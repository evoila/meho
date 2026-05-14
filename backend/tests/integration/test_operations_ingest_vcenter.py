# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test for :func:`parse_openapi` against vCenter's published spec.

Skipped when the spec isn't reachable on the runner.

Two sources are accepted, in priority order:

1. ``MEHO_VCENTER_OPENAPI`` env var — explicit path or URL. The
   ingestion pipeline (T8 #408) will provision this in CI via the
   consumer's checked-in ``docs/vcenter-9.0/vcenter.yaml`` once that
   ship-ride is set up.
2. ``${MEHO_CONSUMER_DOCS_ROOT}/vcenter-9.0/vcenter.yaml`` — points
   at the consumer repo's ``docs/`` tree. Local-dev convenience for
   anyone with the consumer-needs repo cloned.

When neither is set, the test is skipped (not failed) so CI runs that
predate T8's provisioning stay green. The unit-test fixtures cover
contract behaviour; this test only asserts the parser scales to the
real spec corpus per the Initiative's acceptance criterion ("≥95% of
paths produce a row with non-null parameter_schema").
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meho_backplane.operations.ingest import parse_openapi


def _resolve_vcenter_spec() -> str | None:
    """Resolve the vCenter spec from env vars.

    Returns either an ``http(s)://`` URL or a local filesystem path,
    both of which ``parse_openapi`` accepts. ``None`` when no source
    is configured — the integration test skips in that case.
    """
    explicit = os.getenv("MEHO_VCENTER_OPENAPI")
    if explicit:
        if explicit.startswith(("http://", "https://")):
            return explicit
        candidate = Path(explicit)
        if candidate.exists():
            return str(candidate)
    consumer_docs = os.getenv("MEHO_CONSUMER_DOCS_ROOT")
    if consumer_docs:
        candidate = Path(consumer_docs) / "vcenter-9.0" / "vcenter.yaml"
        if candidate.exists():
            return str(candidate)
    return None


@pytest.mark.skipif(
    _resolve_vcenter_spec() is None,
    reason=(
        "vcenter.yaml unavailable — set MEHO_VCENTER_OPENAPI or MEHO_CONSUMER_DOCS_ROOT. "
        "The unit-test fixtures cover the parser contract; this integration test only "
        "verifies the parser scales to the real spec corpus."
    ),
)
def test_parse_vcenter_meets_path_coverage_threshold() -> None:
    spec_path = _resolve_vcenter_spec()
    assert spec_path is not None  # guarded by skipif above
    rows = parse_openapi(spec_path, spec_source="spec:vcenter.yaml")
    distinct_paths = {row.path for row in rows}
    assert len(rows) >= 950, f"got {len(rows)} rows; acceptance threshold is 950"
    assert len(distinct_paths) >= 950, (
        f"got {len(distinct_paths)} distinct paths; acceptance threshold is 950"
    )
    rows_with_params = [r for r in rows if r.parameter_schema.get("properties")]
    # Spot-checks per acceptance criteria: GET → safe, POST → caution, DELETE → dangerous.
    safe = [r for r in rows if r.method == "GET"]
    caution = [r for r in rows if r.method == "POST"]
    dangerous = [r for r in rows if r.method == "DELETE"]
    assert safe, "spec must have at least one GET"
    assert caution, "spec must have at least one POST"
    assert dangerous, "spec must have at least one DELETE"
    assert all(r.safety_level == "safe" for r in safe[:5])
    assert all(r.safety_level == "caution" for r in caution[:5])
    assert all(r.safety_level == "dangerous" for r in dangerous[:5])
    # Every row with parameters has x-meho-param-loc populated.
    for row in rows_with_params:
        properties = row.parameter_schema["properties"]
        assert isinstance(properties, dict)
        for prop_name, prop_schema in properties.items():
            assert isinstance(prop_schema, dict), f"{row.op_id}: {prop_name} not a dict"
            assert "x-meho-param-loc" in prop_schema, (
                f"{row.op_id}: parameter {prop_name!r} missing x-meho-param-loc"
            )
            assert prop_schema["x-meho-param-loc"] in {"path", "query", "header", "body"}
    # spec_source threading.
    assert all("spec:vcenter.yaml" in row.tags for row in rows)
