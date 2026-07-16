# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bulk-import row parsing for the topology console batch surface.

Initiative #1941 (G10.17 Topology console writes), Task #1954 (T2). Split
from :mod:`~meho_backplane.ui.routes.topology.batch` (the route registration
+ handlers) so the pure parse / coerce logic is unit-testable without a
FastAPI :class:`Request` fixture and neither module exceeds the chassis
~600-line cap — mirrors the
:mod:`~meho_backplane.ui.routes.connectors.import_router` /
:mod:`~meho_backplane.ui.routes.connectors.import_view` split for the
``targets.yaml`` import surface.

The accepted document shape mirrors ``meho topology bulk-import``: a YAML (or
JSON, since YAML is a JSON superset) document with a top-level ``edges:``
list of ``{from, kind, to, note?, evidence_url?}`` rows, where ``from`` /
``to`` are a bare name string or a ``{name, kind}`` mapping. The parser
resolves the paste-vs-upload precedence, decodes the bytes, and coerces every
row into a :class:`~meho_backplane.topology.bulk_import.BulkImportRow`; the
``kind`` is forwarded verbatim so the service's per-row ``invalid_bulk``
aggregation surfaces a malformed kind slug alongside every other bad row.
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import UploadFile

from meho_backplane.topology.annotate import NodeRef
from meho_backplane.topology.bulk_import import BulkImportRow

__all__ = [
    "BulkParseError",
    "parse_bulk_rows",
    "resolve_bulk_text",
]

#: Bound on the row count a single batch accepts, mirroring the REST
#: ``_BULK_IMPORT_MAX_EDGES`` boundary so the console rejects an oversized
#: batch with the same legible message rather than handing 10k rows to the
#: per-row validation loop.
BULK_MAX_ROWS = 1000


class BulkParseError(ValueError):
    """The pasted / uploaded doc could not be parsed into import rows.

    Carries a single operator-facing message; raised before any service
    call so the panel re-renders with a typed parse banner rather than a
    500. Distinct from
    :class:`~meho_backplane.topology.bulk_import.BulkImportValidationError`
    (which is the *service's* per-row validation aggregate); this is the
    pre-service "your file is not even shaped like an edges doc" failure.
    """


async def resolve_bulk_text(pasted: str | None, upload: UploadFile | None) -> str:
    """Resolve the submitted rows to a single string.

    An uploaded file takes precedence over a non-empty paste (the file is
    the more deliberate gesture). Bytes decode as UTF-8 with
    ``errors="replace"`` so a stray non-UTF-8 byte surfaces as a parse
    error in the panel rather than a 500 — mirrors the ``targets.yaml``
    import surface's ``_resolve_yaml_text``.
    """
    if upload is not None and upload.filename:
        raw = await upload.read()
        return raw.decode("utf-8", errors="replace")
    return pasted or ""


def _endpoint_to_ref(value: Any, *, side: str, index: int) -> NodeRef:
    """Build a :class:`NodeRef` from one row's ``from`` / ``to`` value.

    Accepts the same two shapes the CLI's YAML decoder accepts: a bare
    scalar (a name with no kind pin — the common case) or a ``{name, kind}``
    mapping (when an endpoint is ambiguous and the operator pins the kind).
    ``from`` is never threaded as a Python keyword — the value arrives as a
    parsed YAML node and is converted here, so the reserved-word footgun
    never reaches the service.

    Raises:
        BulkParseError: The endpoint is neither a scalar nor a mapping
            with a non-empty ``name``.
    """
    if isinstance(value, str):
        name = value.strip()
        if not name:
            raise BulkParseError(f"row {index}: {side} endpoint is empty")
        return NodeRef(name=name, kind=None)
    if isinstance(value, dict):
        raw_name = value.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise BulkParseError(
                f"row {index}: {side} endpoint mapping is missing a non-empty `name`"
            )
        raw_kind = value.get("kind")
        kind = raw_kind.strip() if isinstance(raw_kind, str) and raw_kind.strip() else None
        return NodeRef(name=raw_name.strip(), kind=kind)
    raise BulkParseError(
        f"row {index}: {side} endpoint must be a name string or a {{name, kind}} mapping"
    )


def _row_from_mapping(row: Any, *, index: int) -> BulkImportRow:
    """Project one parsed ``edges:`` entry into a :class:`BulkImportRow`.

    Validates the row is a mapping carrying ``from`` / ``kind`` / ``to`` and
    coerces the endpoints + optional ``note`` / ``evidence_url``. The
    ``kind`` is forwarded verbatim — the service validates it against the
    open slug grammar
    (:data:`~meho_backplane.db.models.KIND_SLUG_PATTERN`) and aggregates a
    malformed kind into the per-row ``invalid_bulk`` error, so the
    panel surfaces it alongside every other bad row rather than aborting here
    on the first one.

    Raises:
        BulkParseError: The row is not a ``{from, kind, to}`` mapping.
    """
    if not isinstance(row, dict):
        raise BulkParseError(f"row {index}: each edge must be a mapping with from / kind / to")
    if "from" not in row or "to" not in row or "kind" not in row:
        raise BulkParseError(f"row {index}: each edge needs `from`, `kind`, and `to`")
    raw_kind = row.get("kind")
    if not isinstance(raw_kind, str) or not raw_kind.strip():
        raise BulkParseError(f"row {index}: `kind` must be a non-empty string")
    note = row.get("note")
    evidence_url = row.get("evidence_url")
    return BulkImportRow(
        from_ref=_endpoint_to_ref(row["from"], side="from", index=index),
        kind=raw_kind.strip(),
        to_ref=_endpoint_to_ref(row["to"], side="to", index=index),
        note=note.strip() if isinstance(note, str) and note.strip() else None,
        evidence_url=(
            evidence_url.strip() if isinstance(evidence_url, str) and evidence_url.strip() else None
        ),
    )


def parse_bulk_rows(text: str) -> list[BulkImportRow]:
    """Parse a pasted / uploaded ``edges:`` doc into import rows.

    The accepted shape mirrors the CLI's bulk-import file: a YAML (or JSON,
    since YAML is a JSON superset) document with a top-level ``edges:`` list
    of ``{from, kind, to, note?, evidence_url?}`` rows. ``from`` / ``to``
    accept a bare name string or a ``{name, kind}`` mapping. A bare list at
    the top level (no ``edges:`` wrapper) is also accepted as a convenience
    so an operator pasting just the rows is not punished.

    Raises:
        BulkParseError: The text is empty, not parseable YAML, not an
            ``edges:`` list / bare list, or exceeds the row cap.
    """
    if not text.strip():
        raise BulkParseError("paste or upload at least one edge row")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # ``yaml`` exposes a problem mark on parse errors; keep the
        # operator-facing message terse and drop the multi-line dump.
        raise BulkParseError(f"could not parse the rows as YAML/JSON: {exc}") from exc
    # A mapping carries the rows under ``edges:``; a bare top-level list is
    # accepted as a convenience (an operator pasting just the rows).
    edges = doc.get("edges") if isinstance(doc, dict) else doc
    if not isinstance(edges, list) or not edges:
        raise BulkParseError(
            "the document must carry a non-empty `edges:` list (or be a bare list of rows)"
        )
    if len(edges) > BULK_MAX_ROWS:
        raise BulkParseError(
            f"too many rows ({len(edges)}); the console caps a single batch at {BULK_MAX_ROWS}"
        )
    return [_row_from_mapping(row, index=index) for index, row in enumerate(edges)]
