# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Drift-guard: every Alembic migration must use ``value.hex`` (not ``str(value)``) for UUID binds.

Initiative #956 (G0.11 CI + test-infra hardening), Task #1095. PR
#1045's fix to ``0018_seed_rdc_internal_conventions.py`` replaced
``str(value)`` with ``value.hex`` in ``_uuid_param`` after 88 test
failures caused by ORM FK lookups failing bytewise against a row
the migration had stored in 36-char canonical form.

The root cause: SQLAlchemy's ``Uuid(as_uuid=True)`` column type binds
UUID values as 32-char hex (``value.hex``) on SQLite. A data migration
that writes ``str(uuid_value)`` (36-char ``fc8c7b96-89f9-...``) stores
a string the ORM's FK lookup never matches (it looks for
``fc8c7b9689f9...``). The mismatch is silently invisible at the
migration level but cascades into FK failures at test time.

This module provides two guards:

1. **AST scan** — :func:`check_migration_file` inspects every
   ``backend/alembic/versions/*.py`` file at the AST level, looking
   for ``str(<expr>)`` call patterns inside assignment-like contexts
   (dict values, function arguments) that are named in a way consistent
   with UUID binding (variable names ending in ``_id``, ``_uuid``,
   containing ``uuid``, or the argument itself being a ``uuid.*`` call).
   This is a conservative scan: it flags on names likely to be UUIDs;
   it does *not* flag arbitrary ``str(...)`` calls.

2. **Regression fixture** — :func:`test_regression_fixture_fails` injects
   a synthetic migration that commits the bug (``str(uuid_value)``) and
   asserts the AST scan catches it, proving the guard is live.

Audit results as of Task #1095 (2026-05-26)
--------------------------------------------

Migrations 0001-0024 inspected:

* 0001-0010: DDL-only or no UUID bind parameters (no hand-rolled INSERT
  with UUID values).
* 0011: backfill migration (UPDATE via SQLAlchemy Core); no UUID bind
  params -- the WHERE clause uses text columns.
* 0012-0017, 0019-0024: DDL-only schema migrations; no data seeding.
* 0018: the only data migration with hand-rolled UUID binding.
  Already fixed in PR #1045: ``_uuid_param`` returns ``value.hex``
  on SQLite, ``uuid.UUID`` object on PostgreSQL. **No further fix
  needed.**

No migration currently uses ``str(uuid_value)`` in a UUID bind context.
The drift-guard below fails if a future migration introduces the pattern.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Any

import pytest

#: Directory containing the migration files to audit.
#: parents[0] = backend/tests, parents[1] = backend
_VERSIONS_DIR: Path = Path(__file__).resolve().parents[1] / "alembic" / "versions"

# ---------------------------------------------------------------------------
# AST-based scanner
# ---------------------------------------------------------------------------

#: Variable name substrings that suggest a UUID value is being bound.
#: Conservative: we flag when the name *ends with* one of these, or
#: when it *contains* ``uuid`` (case-insensitive).
_UUID_NAME_HINTS: frozenset[str] = frozenset(
    {"_id", "_uuid", "tenant_id", "convention_id", "run_id", "principal_id"}
)


def _looks_like_uuid_name(name: str) -> bool:
    """Return True when *name* suggests a UUID value (heuristic)."""
    lower = name.lower()
    if "uuid" in lower:
        return True
    return any(lower.endswith(hint) for hint in _UUID_NAME_HINTS)


def _is_str_call(node: ast.expr) -> bool:
    """Return True when *node* is a ``str(...)`` call (builtin str)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and not node.keywords
    )


def _arg_looks_like_uuid(arg: ast.expr) -> bool:
    """Heuristic: does the ``str(...)`` argument look like a UUID value?

    True when the argument is:
    * A name that matches a UUID hint (e.g. ``str(tenant_id)``).
    * An attribute on a name that matches a UUID hint
      (e.g. ``str(tenant.id)``).
    * A ``uuid.uuid4()`` call.
    * An ``attr`` access on a uuid module call (e.g. ``str(uuid.uuid4())``).
    """
    if isinstance(arg, ast.Name) and _looks_like_uuid_name(arg.id):
        return True
    if isinstance(arg, ast.Attribute):
        # str(obj.id) or str(something.uuid)
        if _looks_like_uuid_name(arg.attr):
            return True
        # str(value.something) where value is a UUID-hinted name
        if isinstance(arg.value, ast.Name) and _looks_like_uuid_name(arg.value.id):
            return True
    # str(uuid.uuid4()) or str(uuid4())
    if isinstance(arg, ast.Call):
        func = arg.func
        if isinstance(func, ast.Attribute) and func.attr in ("uuid4", "UUID", "uuid1"):
            return True
        if isinstance(func, ast.Name) and func.id in ("uuid4", "UUID"):
            return True
    return False


def _find_str_uuid_violations(tree: ast.AST) -> list[int]:
    """Walk *tree* and return line numbers of ``str(<uuid-ish>)`` patterns.

    Specifically looks for dict values (the ``{...: str(uuid_val), ...}``
    pattern used in migration ``execute()`` binds) and keyword arguments.
    """
    violations: list[int] = []
    for node in ast.walk(tree):
        # Pattern 1: dict literal value -- {key: str(uuid_val)}
        if isinstance(node, ast.Dict):
            for value in node.values:
                if not isinstance(value, ast.Call):
                    continue
                if _is_str_call(value) and _arg_looks_like_uuid(value.args[0]):
                    violations.append(value.lineno)
        # Pattern 2: keyword argument -- func(param=str(uuid_val))
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                kw_val = kw.value
                if not isinstance(kw_val, ast.Call):
                    continue
                if _is_str_call(kw_val) and _arg_looks_like_uuid(kw_val.args[0]):
                    violations.append(kw_val.lineno)
    return violations


def check_migration_file(path: Path) -> list[str]:
    """Scan *path* for ``str(<uuid-ish>)`` bind patterns.

    Returns a (possibly empty) list of human-readable violation strings.
    Returns an empty list when the file is clean.
    Raises ``SyntaxError`` if the file cannot be parsed (broken migration).
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    line_numbers = _find_str_uuid_violations(tree)
    return [
        f"{path.name}:{lineno}: str(<uuid-value>) found — use value.hex on SQLite "
        f"(see PR #1045 incident and docs/codebase/migrations.md)"
        for lineno in line_numbers
    ]


# ---------------------------------------------------------------------------
# Live audit: all shipped migrations must be clean
# ---------------------------------------------------------------------------


def test_no_str_uuid_binding_in_any_migration() -> None:
    """All migrations in ``backend/alembic/versions/`` are free of ``str(uuid-ish)`` binds.

    This test fails when a new migration introduces the pattern that
    caused PR #1045's 88-test cascade: storing a UUID as the 36-char
    canonical ``str(value)`` form in a column that SQLAlchemy's
    ``Uuid(as_uuid=True)`` type stores as 32-char hex on SQLite.

    Audit summary (Task #1095, 2026-05-26): migrations 0001-0024 were
    inspected; none use the forbidden pattern. Migration 0018 fixed the
    pattern in PR #1045; subsequent migrations follow the convention
    established there (``_uuid_param`` helper / ``value.hex`` on SQLite).
    """
    migration_files = sorted(_VERSIONS_DIR.glob("*.py"))
    assert migration_files, (
        f"no migration files found in {_VERSIONS_DIR} — check the path is correct"
    )

    all_violations: list[str] = []
    for migration_file in migration_files:
        violations = check_migration_file(migration_file)
        all_violations.extend(violations)

    assert not all_violations, (
        "UUID bind consistency violations found in Alembic migrations.\n"
        "Use ``value.hex`` (SQLite) / ``uuid.UUID`` object (PG) via a dialect-aware "
        "helper like ``_uuid_param`` — not ``str(uuid_value)``.\n"
        "See docs/codebase/migrations.md for the convention.\n\n" + "\n".join(all_violations)
    )


# ---------------------------------------------------------------------------
# Regression fixture: prove the guard is live
# ---------------------------------------------------------------------------


def test_regression_fixture_detected_by_guard(tmp_path: Path) -> None:
    """The AST guard catches a synthetic migration that commits the bug.

    A temporary migration is written into ``tmp_path`` with the exact
    pattern that broke PR #1045: a dict value ``str(tenant_id)`` in
    a bind parameter dict. The guard must flag it; if it returns clean,
    the guard has a false-negative and this test fails.

    This test does *not* run ``alembic upgrade`` — it exercises only
    the AST scanner, so it runs fast and without a live DB.
    """
    bad_migration = tmp_path / "9999_bad_uuid_binding.py"
    bad_migration.write_text(
        textwrap.dedent(
            """\
            # Synthetic regression fixture — NOT a real migration.
            import uuid
            import sqlalchemy as sa
            from alembic import op

            revision = "9999"
            down_revision = None


            def upgrade() -> None:
                tenant_id = uuid.uuid4()
                bind = op.get_bind()
                bind.execute(
                    sa.text("INSERT INTO tenant (id, slug) VALUES (:id, :slug)"),
                    {
                        "id": str(tenant_id),   # BUG: should be tenant_id.hex
                        "slug": "test-tenant",
                    },
                )


            def downgrade() -> None:
                pass
            """
        ),
        encoding="utf-8",
    )

    violations = check_migration_file(bad_migration)
    assert violations, (
        "The AST drift-guard should have flagged str(tenant_id) in the "
        "regression fixture but returned no violations. "
        "The guard has a false-negative — review _find_str_uuid_violations()."
    )
    # Confirm the violation message names the offending file.
    assert any("9999_bad_uuid_binding.py" in v for v in violations), (
        f"expected the violation to name the file; got: {violations!r}"
    )


def test_correct_hex_binding_is_not_flagged(tmp_path: Path) -> None:
    """A migration that uses ``value.hex`` is not flagged by the guard.

    Asserts the guard has no false-positives on the correct pattern
    (``tenant_id.hex`` as a dict value), which is what migration 0018
    does after the PR #1045 fix.
    """
    good_migration = tmp_path / "9998_correct_uuid_binding.py"
    good_migration.write_text(
        textwrap.dedent(
            """\
            # Synthetic correct migration — NOT a real migration.
            import uuid
            import sqlalchemy as sa
            from alembic import op

            revision = "9998"
            down_revision = None

            IS_POSTGRES = False  # simplified for fixture


            def upgrade() -> None:
                tenant_id = uuid.uuid4()
                bind = op.get_bind()
                bind.execute(
                    sa.text("INSERT INTO tenant (id, slug) VALUES (:id, :slug)"),
                    {
                        "id": tenant_id if IS_POSTGRES else tenant_id.hex,
                        "slug": "test-tenant",
                    },
                )


            def downgrade() -> None:
                pass
            """
        ),
        encoding="utf-8",
    )

    violations = check_migration_file(good_migration)
    assert not violations, (
        f"The correct ``value.hex`` pattern should not be flagged; got: {violations!r}"
    )


# ---------------------------------------------------------------------------
# Parametrised per-migration audit for granular failure reporting
# ---------------------------------------------------------------------------


def _collect_migration_params() -> list[Any]:
    """Return a pytest.param per migration file for parametrised testing.

    Evaluated at collection time so the param list is visible in
    ``pytest --collect-only`` output. Returns an empty list when the
    versions directory doesn't exist (boot-strap environment).
    """
    if not _VERSIONS_DIR.exists():
        return []
    return [
        pytest.param(p, id=p.name)
        for p in sorted(_VERSIONS_DIR.glob("*.py"))
        if p.name != "__init__.py"
    ]


@pytest.mark.parametrize("migration_file", _collect_migration_params())
def test_individual_migration_clean(migration_file: Path) -> None:
    """Each migration file passes the UUID bind consistency check individually.

    Parametrised so a failure names the specific file rather than
    lumping all violations into the aggregate test above. Both tests
    run: the aggregate provides a summary, the parametrised form
    provides per-file granularity in CI's test-results matrix.
    """
    violations = check_migration_file(migration_file)
    assert not violations, f"UUID bind violation in {migration_file.name}:\n" + "\n".join(
        violations
    )
