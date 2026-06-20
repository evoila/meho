# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the topology bulk-import row parser (Task #1954).

Pure parse / coerce logic — no DB, no FastAPI request. Covers the document
shapes the console accepts (``edges:`` wrapper + bare list), the two endpoint
forms (bare scalar + ``{name, kind}`` mapping), the ``note`` / ``evidence_url``
coercion, and every parse-failure surface that re-renders the panel's typed
banner before any service call.
"""

from __future__ import annotations

import pytest

from meho_backplane.ui.routes.topology.batch_parse import (
    BULK_MAX_ROWS,
    BulkParseError,
    parse_bulk_rows,
)


def test_parse_edges_wrapper_with_mapping_endpoints() -> None:
    """An ``edges:`` doc with ``{name, kind}`` endpoints coerces to rows."""
    text = (
        "edges:\n"
        "  - from: { name: sa-foo, kind: principal }\n"
        "    kind: authenticates-via\n"
        "    to: { name: role-bar, kind: vault-role }\n"
        "    note: from INVENTORY.md\n"
        "    evidence_url: https://example.test/e\n"
    )
    rows = parse_bulk_rows(text)
    assert len(rows) == 1
    row = rows[0]
    assert row.from_ref.name == "sa-foo"
    assert row.from_ref.kind == "principal"
    assert row.kind == "authenticates-via"
    assert row.to_ref.name == "role-bar"
    assert row.to_ref.kind == "vault-role"
    assert row.note == "from INVENTORY.md"
    assert row.evidence_url == "https://example.test/e"


def test_parse_bare_scalar_endpoints_leave_kind_unset() -> None:
    """A bare-scalar endpoint resolves to a name with no kind pin."""
    text = "edges:\n  - from: svc-a\n    kind: depends-on\n    to: db-1\n"
    rows = parse_bulk_rows(text)
    assert rows[0].from_ref.name == "svc-a"
    assert rows[0].from_ref.kind is None
    assert rows[0].to_ref.name == "db-1"
    assert rows[0].to_ref.kind is None


def test_parse_bare_top_level_list_convenience() -> None:
    """A bare top-level list (no ``edges:`` wrapper) is accepted."""
    text = "- from: svc-a\n  kind: depends-on\n  to: db-1\n"
    rows = parse_bulk_rows(text)
    assert len(rows) == 1
    assert rows[0].kind == "depends-on"


def test_parse_json_superset_input() -> None:
    """JSON input parses (YAML is a JSON superset)."""
    text = '{"edges": [{"from": "svc-a", "kind": "depends-on", "to": "db-1"}]}'
    rows = parse_bulk_rows(text)
    assert len(rows) == 1
    assert rows[0].from_ref.name == "svc-a"


def test_parse_blank_note_and_evidence_coerce_to_none() -> None:
    """Blank / whitespace ``note`` / ``evidence_url`` coerce to ``None``."""
    text = "edges:\n  - from: a\n    kind: depends-on\n    to: b\n    note: '   '\n"
    rows = parse_bulk_rows(text)
    assert rows[0].note is None
    assert rows[0].evidence_url is None


def test_parse_empty_text_raises() -> None:
    with pytest.raises(BulkParseError, match="at least one edge row"):
        parse_bulk_rows("   ")


def test_parse_non_yaml_raises() -> None:
    with pytest.raises(BulkParseError, match="could not parse"):
        parse_bulk_rows("key: value: : broken")


def test_parse_missing_edges_list_raises() -> None:
    with pytest.raises(BulkParseError, match="non-empty `edges:` list"):
        parse_bulk_rows("not_edges: []")


def test_parse_row_missing_required_field_raises() -> None:
    with pytest.raises(BulkParseError, match="needs `from`, `kind`, and `to`"):
        parse_bulk_rows("edges:\n  - from: a\n    to: b\n")


def test_parse_endpoint_mapping_without_name_raises() -> None:
    with pytest.raises(BulkParseError, match="missing a non-empty `name`"):
        parse_bulk_rows("edges:\n  - from: { kind: vm }\n    kind: depends-on\n    to: b\n")


def test_parse_endpoint_wrong_type_raises() -> None:
    with pytest.raises(BulkParseError, match="name string or a"):
        parse_bulk_rows("edges:\n  - from: [1, 2]\n    kind: depends-on\n    to: b\n")


def test_parse_exceeds_row_cap_raises() -> None:
    rows = "\n".join(
        f"  - from: n{i}\n    kind: depends-on\n    to: m{i}" for i in range(BULK_MAX_ROWS + 1)
    )
    with pytest.raises(BulkParseError, match="too many rows"):
        parse_bulk_rows("edges:\n" + rows)
