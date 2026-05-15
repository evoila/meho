# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test for :func:`parse_openapi` against vSphere's ``vi-json.yaml``.

vi-json.yaml (~2,195 Managed Object operations) uses
``$ref: "#/components/parameters/moId"`` on every operation. Until T11
(#501) extended the parser to resolve ``#/components/parameters/*``
refs, the first operation in the spec raised :class:`UnsupportedSpecError`
and the entire spec failed to ingest. This integration test is the
load-bearing smoke check that the parser-extension landed —
asserts the spec parses end-to-end without raising and returns
>= 2,000 :class:`EndpointDescriptorProto` rows (the ~2,195 figure
varies slightly with spec revs; 2,000 is the load-bearing threshold
the Task ACs name).

Skipped when no spec source is configured, mirroring the
``vcenter.yaml`` integration test next door. Storage + grouping +
retrieval are downstream Tasks (T3 under #227 G3.1); this test only
asserts the parser does not raise.
"""

from __future__ import annotations

import pytest

from meho_backplane.operations.ingest import parse_openapi
from tests.acceptance._vcenter_spec import VCENTER_SPEC_REASON, resolve_vi_json_yaml


@pytest.mark.skipif(
    resolve_vi_json_yaml() is None,
    reason=VCENTER_SPEC_REASON,
)
def test_parse_vi_json_does_not_raise() -> None:
    """vi-json.yaml parses end-to-end after T11's parameter-ref resolver landed."""
    spec_path = resolve_vi_json_yaml()
    assert spec_path is not None  # guarded by skipif above
    rows = parse_openapi(str(spec_path), spec_source="spec:vi-json.yaml")
    assert len(rows) >= 2000, f"got {len(rows)} rows; acceptance threshold is 2000"
    # Spot-check the parameter-ref resolution path: every row that
    # carries an ``moId`` property must have ``x-meho-param-loc="path"``
    # (the shared parameter is declared ``in: path``).
    rows_with_moid = [
        row
        for row in rows
        if isinstance(row.parameter_schema.get("properties"), dict)
        and "moId" in row.parameter_schema["properties"]
    ]
    assert rows_with_moid, "expected at least one operation to reference the shared moId param"
    for row in rows_with_moid[:25]:
        properties = row.parameter_schema["properties"]
        assert isinstance(properties, dict)
        mo_id = properties["moId"]
        assert isinstance(mo_id, dict), f"{row.op_id}: moId property is not a mapping"
        assert mo_id.get("x-meho-param-loc") == "path", (
            f"{row.op_id}: moId x-meho-param-loc is {mo_id.get('x-meho-param-loc')!r}"
        )
    # spec_source threading.
    assert all("spec:vi-json.yaml" in row.tags for row in rows)
