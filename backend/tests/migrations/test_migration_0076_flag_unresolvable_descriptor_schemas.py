# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0076_flag_unresolvable_descriptor_schemas``.

Task #3102. The migration adds ``endpoint_descriptor.needs_reingest``
and runs the one-time detect pass over ``source_kind = 'ingested'``
rows, flagging those whose stored ``parameter_schema`` carries a
schema-position ``$ref`` that does not resolve as a JSON Pointer within
the stored document — the pre-#3095 dangling-ref shape that fails every
dispatch with ``invalid_op_schema``.

Pinned properties:

* A pre-fix-shaped ingested row (nested component ref, no bundle) IS
  flagged; a post-#3095 bundled row, a ref-free row, and a row whose
  only ``$ref``-shaped object sits in a data-value position (``enum``)
  are NOT (:func:`test_detect_pass_flags_exactly_the_broken_rows`).
* Tenant-scoped rows are in scope — the live-hit rows are consumer
  tenant ingests (:func:`test_tenant_scoped_dangling_row_flagged`).
* ``source_kind = 'typed'`` rows are exempt: the re-ingest remedy does
  not apply to hand-coded schemas
  (:func:`test_typed_row_with_dangling_ref_not_flagged`).
* ``downgrade()`` really drops the column
  (:func:`test_downgrade_drops_column`).
* The migration's **frozen** ref-walk agrees with the live
  ``operations.ingest.refs`` functions over a corpus — the same
  lock-step drift-guard discipline ``0070`` uses for its frozen state
  vocabulary (:func:`test_frozen_walk_matches_live_refs_module`).

Revision pin: every test drives the forward pass with
``command.upgrade(cfg, "0076")`` (this migration's own revision, NOT
``head``). Sync-test constraint identical to the sibling migration
tests: ``alembic.command`` drives env.py's async cookbook via
``asyncio.run``, so the test functions stay sync.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.operations.ingest.refs import find_unresolvable_local_refs
from meho_backplane.settings import get_settings

#: This migration's own revision -- the forward-pass target (NOT ``head``).
_THIS_REVISION: Final[str] = "0076"
#: The immediately preceding head this migration revises.
_DOWN_REVISION: Final[str] = "0075"

#: Stable seed timestamp -- keeps the raw-SQL seeds deterministic.
_SEED_TS: Final[str] = "2026-01-01T00:00:00+00:00"

#: The pre-#3095 stored shape: a nested component ref with no bundle.
_DANGLING_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "body": {
            "type": "object",
            "properties": {
                "file_spec": {"$ref": "#/components/schemas/TransferEndpoint"},
            },
            "x-meho-param-loc": "body",
        },
    },
    "required": ["body"],
}

#: The post-#3095 shape for the same op: identical plus the bundled closure.
_BUNDLED_SCHEMA: Final[dict[str, Any]] = {
    **_DANGLING_SCHEMA,
    "components": {
        "schemas": {
            "TransferEndpoint": {
                "type": "object",
                "properties": {"uri": {"type": "string"}},
            },
        },
    },
}

#: No refs at all -- the common healthy row.
_REF_FREE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"name": {"type": "string", "x-meho-param-loc": "query"}},
}

#: A ``$ref``-shaped object in a data-value position: literal data the
#: validator never dereferences. Flagging it would false-positive a
#: healthy row -- the walk must skip ``enum`` members.
_REF_IN_ENUM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {
            "x-meho-param-loc": "query",
            "enum": [{"$ref": "#/components/schemas/NotASchemaRef"}, "plain"],
        },
    },
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same harness as the sibling migration tests: sync fixture
    (``alembic.command`` calls ``asyncio.run`` internally), per-test
    SQLite file under ``tmp_path``, settings + engine caches reset on
    both sides so the alembic env reads *this* ``DATABASE_URL``.
    """
    db_path = tmp_path / "migration_0076.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _insert_descriptor_row(
    sync_url: str,
    *,
    parameter_schema: dict[str, Any],
    tenant_id: UUID | None = None,
    op_id: str = "POST:/api/library/upload-file",
    source_kind: str = "ingested",
) -> UUID:
    """Insert one ``endpoint_descriptor`` row at the migration base.

    Raw SQL (not the ORM) keeps the seed pinned to the schema the
    migration runs against; ``parameter_schema`` is serialised the way
    the portable JSON column stores it on SQLite. UUID binds use
    ``.hex`` per ``docs/codebase/migrations.md``.
    """
    row_id = uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO endpoint_descriptor (
                        id, tenant_id, product, version, impl_id, op_id,
                        source_kind, parameter_schema, created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, 'vmware', '9.0', 'vmware-rest-probe', :op_id,
                        :source_kind, :parameter_schema, :ts, :ts
                    )
                    """,
                ),
                {
                    "id": row_id.hex,
                    "tenant_id": tenant_id.hex if tenant_id is not None else None,
                    "op_id": op_id,
                    "source_kind": source_kind,
                    "parameter_schema": json.dumps(parameter_schema),
                    "ts": _SEED_TS,
                },
            )
    finally:
        sync_eng.dispose()
    return row_id


def _needs_reingest(sync_url: str, row_id: UUID) -> bool:
    """Read one row's ``needs_reingest`` back as a bool (SQLite stores 0/1)."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            value = conn.execute(
                text("SELECT needs_reingest FROM endpoint_descriptor WHERE id = :id"),
                {"id": row_id.hex},
            ).scalar_one()
            return bool(value)
    finally:
        sync_eng.dispose()


def _column_names(sync_url: str) -> set[str]:
    """Return the live column names of ``endpoint_descriptor``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        return {col["name"] for col in sa_inspect(sync_eng).get_columns("endpoint_descriptor")}
    finally:
        sync_eng.dispose()


def test_detect_pass_flags_exactly_the_broken_rows(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Only the dangling-ref ingested row is flagged; healthy shapes stay false.

    The three negative shapes each pin a distinct false-positive class:
    a post-#3095 bundled document (the ref resolves into the bundle), a
    ref-free schema, and a ``$ref``-shaped object inside ``enum`` (data,
    not a schema position).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    dangling = _insert_descriptor_row(sync_url, parameter_schema=_DANGLING_SCHEMA, op_id="POST:/a")
    bundled = _insert_descriptor_row(sync_url, parameter_schema=_BUNDLED_SCHEMA, op_id="POST:/b")
    ref_free = _insert_descriptor_row(sync_url, parameter_schema=_REF_FREE_SCHEMA, op_id="GET:/c")
    ref_in_enum = _insert_descriptor_row(
        sync_url, parameter_schema=_REF_IN_ENUM_SCHEMA, op_id="GET:/d"
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _needs_reingest(sync_url, dangling) is True, (
        "the pre-#3095 dangling-ref row must be flagged"
    )
    assert _needs_reingest(sync_url, bundled) is False, (
        "a bundled document self-resolves and must stay unflagged"
    )
    assert _needs_reingest(sync_url, ref_free) is False
    assert _needs_reingest(sync_url, ref_in_enum) is False, (
        "a $ref-shaped enum member is data the validator never resolves"
    )


def test_tenant_scoped_dangling_row_flagged(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The pass spans every tenant scope -- the live-hit rows are tenant ingests."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    tenant_row = _insert_descriptor_row(
        sync_url,
        parameter_schema=_DANGLING_SCHEMA,
        tenant_id=uuid4(),
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _needs_reingest(sync_url, tenant_row) is True


def test_typed_row_with_dangling_ref_not_flagged(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``source_kind='typed'`` is exempt -- re-ingest is not the remedy there.

    A dangling ref in a hand-coded schema is a code bug the
    spec-reconcile CI lanes own; flagging it ``needs_reingest`` would
    name a remedy that cannot repair it.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)

    typed_row = _insert_descriptor_row(
        sync_url,
        parameter_schema=_DANGLING_SCHEMA,
        op_id="vault.kv.read",
        source_kind="typed",
    )

    command.upgrade(cfg, _THIS_REVISION)

    assert _needs_reingest(sync_url, typed_row) is False


def test_downgrade_drops_column(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The downgrade is a real reversal: the column is gone at 0075."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _THIS_REVISION)
    assert "needs_reingest" in _column_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert "needs_reingest" not in _column_names(sync_url)


def _load_migration_0076() -> Any:
    """Load migration ``0076`` as a module via its file path.

    Alembic version files are digit-prefixed and not importable as
    normal dotted modules; loading by file path with
    :mod:`importlib.util` is the robust way to reach the migration's
    frozen ref-walk (same shape as ``test_migration_0059``).
    """
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "alembic"
        / "versions"
        / "0076_flag_unresolvable_descriptor_schemas.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0076", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param(_DANGLING_SCHEMA, id="dangling-component-ref"),
        pytest.param(_BUNDLED_SCHEMA, id="bundled-closure"),
        pytest.param(_REF_FREE_SCHEMA, id="ref-free"),
        pytest.param(_REF_IN_ENUM_SCHEMA, id="ref-in-enum-data-position"),
        pytest.param({"$ref": "#/$defs/Missing"}, id="absolute-defs-pointer-dangling"),
        pytest.param(
            {"$defs": {"X": {"type": "string"}}, "$ref": "#/$defs/X"},
            id="absolute-defs-pointer-resolving",
        ),
        pytest.param({"$ref": "#anchor-fragment"}, id="plain-name-anchor"),
        pytest.param({"$ref": "https://other.doc/schema.json#/a"}, id="cross-document"),
        pytest.param({"$ref": "#"}, id="whole-document-self-ref"),
        pytest.param(
            {"allOf": [{"type": "object"}], "$ref": "#/allOf/0"},
            id="array-index-pointer",
        ),
        pytest.param(
            {
                "properties": {"a~b/c": {"type": "string"}},
                "$ref": "#/properties/a~0b~1c",
            },
            id="rfc6901-escaped-segments",
        ),
        pytest.param(
            {"x-vendor": {"$ref": "#/components/schemas/SkippedExtension"}},
            id="ref-under-x-extension",
        ),
        pytest.param(
            {
                "components": {"schemas": {"A": {"$ref": "#/components/schemas/MissingSibling"}}},
            },
            id="dangling-ref-inside-bundle",
        ),
    ],
)
def test_frozen_walk_matches_live_refs_module(schema: dict[str, Any]) -> None:
    """Drift guard: the migration's frozen walk == the live ``refs`` walk.

    The migration deliberately carries a **copy** of
    ``find_unresolvable_local_refs`` (data-migration self-containment,
    ``docs/codebase/migrations.md``). This corpus pins the copy to the
    live module's verdicts at authoring time -- if a future ``refs.py``
    behaviour change makes the two disagree, this fails and forces a
    deliberate decision (new migration vs. corpus update), instead of
    silent divergence.
    """
    migration = _load_migration_0076()
    assert migration._has_unresolvable_local_refs(schema) == bool(
        find_unresolvable_local_refs(schema)
    )
