# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flag ingested descriptors whose stored ``parameter_schema`` cannot self-resolve (#3102).

Revision ID: 0076
Revises: 0075
Create Date: 2026-08-24

#3095 made the OpenAPI parser bundle every ``#/components/schemas/*``
component a stored ``parameter_schema``'s nested ``$ref``\\s transitively
reference, and added the fail-closed ``_assert_parameter_schema_standalone``
lint — so every row written by the fixed parser validates params standalone
at dispatch. Both guards are **ingest-time**: rows persisted by earlier
ingests keep their dangling-ref schemas and stay undispatchable, failing
every call with the structured ``invalid_op_schema`` error (the live-hit:
the governed content-library file-PULL op on a pre-fix vCenter ingest,
claude-rdc-hetzner-dc's c3gov1 build, 2026-08-24).

The original spec document is **not** recoverable server-side — the DB
stores no spec bytes (``spec_provenance`` records only ``uri`` +
``sha256``; probe-derived and URL ingests live on the network / the
appliance) — so a re-bundling data migration is impossible. What this
migration does instead is the one-time **detect-and-flag** pass:

* DDL — add ``endpoint_descriptor.needs_reingest`` (boolean, NOT NULL,
  default false).
* DML — for every ``source_kind = 'ingested'`` row (any tenant scope,
  same blanket-scope rationale as ``0075``: a broken stored schema is
  broken regardless of scope), walk the stored ``parameter_schema`` the
  way the dispatcher's registry-free ``Draft202012Validator`` resolves
  it, and set ``needs_reingest = true`` when any schema-position
  ``$ref`` fails to resolve as a JSON Pointer within the stored
  document itself.

The remedy the flag names is a **same-spec re-ingest**: the ingest
upsert (``operations/ingest/_upsert.py``) overwrites
``parameter_schema`` in place on both existing-row branches while
leaving ``is_enabled`` / ``group_id`` / operator curation untouched,
and clears ``needs_reingest`` — the freshly parsed schema passed the
standalone lint by construction. Typed / composite rows are excluded:
their schemas are hand-authored in code, the re-ingest remedy does not
apply, and a dangling ref there is a code bug the spec-reconcile CI
lanes own.

``response_schema`` is deliberately not scanned — it is neither bundled
nor linted by #3095 (its only dispatch-time consumer is the JSONFlux
reducer, which never resolves refs), so a dangling ref there is inert.

Frozen-logic discipline
-----------------------

The ref-walk below is a point-in-time port of
``meho_backplane.operations.ingest.refs.iter_schema_refs`` /
``find_unresolvable_local_refs`` — deliberately **copied, not
imported**, per the data-migration self-containment rule
(``docs/codebase/migrations.md``: a migration is a frozen snapshot; app
code evolves). The paired test
(``tests/migrations/test_migration_0076_flag_unresolvable_descriptor_schemas.py``)
carries a drift guard that cross-checks this copy against the live
``refs`` functions over a fixture corpus, the same lock-step discipline
``0070`` uses for its frozen state vocabulary.

Additive-only forward, real back
--------------------------------

``upgrade()`` is one ``add_column`` plus UPDATEs — additive, clears
``scripts/ci/check_migration_compat.py``, and an older image simply
never reads the new column (the ``helm rollback`` forward-compat
contract holds). The UPDATE is naturally idempotent (re-running flags
the same rows). ``downgrade()`` drops the column — a real reversal,
same shape as ``0062``.
"""

import json
from collections.abc import Iterator, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Frozen ref-walk (port of operations/ingest/refs.py @ 0076) ----------

#: JSON Schema keywords whose values are **named maps of subschemas** —
#: child keys are arbitrary names, every child value is a schema.
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"}
)

#: JSON Schema keywords whose values are **data payloads**, not
#: subschemas. A ``$ref``-shaped object inside them is literal data the
#: validator never resolves — collecting it would false-flag a healthy
#: row.
_DATA_VALUE_KEYWORDS = frozenset({"enum", "const", "default", "example", "examples"})


def _iter_schema_refs(node: Any) -> Iterator[str]:
    """Yield every ``$ref`` string in *node* that sits in a schema position.

    Frozen copy of ``refs.iter_schema_refs``: generic keywords recurse
    structurally, named-map keywords recurse through their values, a
    bundled ``components.schemas`` map is walked as a named map, and
    data-valued keywords plus vendor ``x-*`` extensions are skipped.
    """
    if isinstance(node, list):
        for item in node:
            yield from _iter_schema_refs(item)
        return
    if not isinstance(node, dict):
        return
    ref = node.get("$ref")
    if isinstance(ref, str):
        yield ref
    for key, value in node.items():
        if key == "$ref":
            continue
        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            for subschema in value.values():
                yield from _iter_schema_refs(subschema)
            continue
        if key == "components" and isinstance(value, dict):
            schemas_bucket = value.get("schemas")
            if isinstance(schemas_bucket, dict):
                for subschema in schemas_bucket.values():
                    yield from _iter_schema_refs(subschema)
            continue
        if key in _DATA_VALUE_KEYWORDS or key.startswith("x-"):
            continue
        yield from _iter_schema_refs(value)


def _unescape_pointer_segment(segment: str) -> str:
    """Apply RFC 6901 unescaping (``~1`` → ``/``, then ``~0`` → ``~``)."""
    return segment.replace("~1", "/").replace("~0", "~")


def _has_unresolvable_local_refs(schema: Any) -> bool:
    """Return whether any schema-position ``$ref`` fails to self-resolve.

    Frozen copy of ``refs.find_unresolvable_local_refs`` reduced to a
    boolean: each ``#/<json-pointer>`` fragment is walked against
    *schema* itself — exactly what the dispatcher's registry-free
    jsonschema validator does at call time. ``#`` resolves trivially;
    any non-fragment (cross-document) ref and any ``#<anchor>``
    plain-name fragment counts as unresolvable.
    """
    for ref in _iter_schema_refs(schema):
        if ref == "#":
            continue
        if not ref.startswith("#/"):
            return True
        node: Any = schema
        for raw_segment in ref[2:].split("/"):
            segment = _unescape_pointer_segment(raw_segment)
            if isinstance(node, dict) and segment in node:
                node = node[segment]
                continue
            if (
                isinstance(node, list)
                and segment.isascii()
                and segment.isdigit()
                and int(segment) < len(node)
            ):
                node = node[int(segment)]
                continue
            return True
    return False


def _coerce_json(raw: object) -> Any:
    """Normalise a ``parameter_schema`` column value to a Python object.

    Belt-and-suspenders: the typed ``sa.JSON()`` column on the reflected
    table already deserialises on both dialects, but a value that
    arrives as a raw JSON string (driver / dialect drift) is decoded
    here rather than silently skipped. Undecodable content returns
    ``None`` — the row is left unflagged, matching the walk's posture of
    never false-flagging what it cannot read.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def _descriptor_table() -> sa.TableClause:
    """Reflect the four ``endpoint_descriptor`` columns this migration touches."""
    return sa.table(
        "endpoint_descriptor",
        sa.column("id", sa.Uuid()),
        sa.column("source_kind", sa.Text()),
        sa.column("parameter_schema", sa.JSON()),
        sa.column("needs_reingest", sa.Boolean()),
    )


def upgrade() -> None:
    """Add ``needs_reingest`` and flag ingested rows whose schema can't self-resolve."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.add_column(
        "endpoint_descriptor",
        sa.Column(
            "needs_reingest",
            sa.Boolean(),
            nullable=False,
            # Same dialect-branched boolean-literal shape as 0005's
            # ``requires_approval`` — constant literals, never
            # interpolated ``text()``.
            server_default=sa.text("false") if is_postgres else sa.text("0"),
        ),
    )

    descriptor = _descriptor_table()
    rows = bind.execute(
        sa.select(descriptor.c.id, descriptor.c.parameter_schema).where(
            descriptor.c.source_kind == "ingested"
        )
    ).all()

    flagged_ids = [
        row_id
        for row_id, raw_schema in rows
        if (schema := _coerce_json(raw_schema)) is not None and _has_unresolvable_local_refs(schema)
    ]
    if not flagged_ids:
        return

    # The typed ``sa.Uuid()`` column carries the dialect-correct bind
    # processing (``.hex`` CHAR(32) on SQLite, native uuid on PG), so the
    # read-back ids round-trip without the manual ``_uuid_param`` dance
    # raw ``sa.text`` migrations need (docs/codebase/migrations.md).
    bind.execute(
        sa.update(descriptor)
        .where(descriptor.c.id == sa.bindparam("row_id"))
        .values(needs_reingest=True),
        [{"row_id": row_id} for row_id in flagged_ids],
    )


def downgrade() -> None:
    """Drop ``needs_reingest`` — a real reversal; the flag is derived state."""
    op.drop_column("endpoint_descriptor", "needs_reingest")
