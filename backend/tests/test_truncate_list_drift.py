# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Drift guard for the integration + acceptance per-test TRUNCATE lists.

Initiative #803 (G11.2 Agent identity + RBAC + approval), Task #1115.

Every ORM model whose ``tenant_id`` column carries a real
``REFERENCES tenant(id)`` ForeignKey must appear in *both* per-test
TRUNCATE lists, because PostgreSQL rejects ``TRUNCATE TABLE tenant``
while any referencing table is omitted from the same statement:

    asyncpg.exceptions.FeatureNotSupportedError:
    cannot truncate a table referenced in a foreign key constraint

Hand-maintained lists drift. The Initiative #803 run paid for this
twice: T3 #1052 (``agent_permission`` missing from
``tests/acceptance/conftest.py``) and T5 #1069 (``approval_request``
missing). Both shipped on a PR that had nothing to do with truncation,
both broke ~150 acceptance tests at fixture setup, both wasted an
auto-review iteration.

This file closes that gap by reading the ORM metadata at import time
and asserting that *every* table with a ``tenant.id`` ForeignKey
appears in the two conftests' truncate lists. When a new tenant-FK
table is added, the test fails on the PR that added the FK -- not on
the next unrelated PR that exercises the fixture path.

Pattern precedent: :mod:`tests.test_db_scheduled_trigger` uses the same
"metadata introspection + frozen literal extraction" shape to assert
that the closed-enum CHECK constraints in migration 0020 agree with
the model enums.

Implementation note: the two conftests use different shapes for the
list -- acceptance has a module-level ``_TRUNCATE_TABLES: tuple[str,
...]`` constant, integration inlines the table set directly in the
``TRUNCATE TABLE ...`` SQL string passed to ``conn.execute(text(...))``
(twice, once per ``pg_engine`` variant). Rather than ``importlib``-
loading each conftest (which would transitively import dozens of
canary-fixture sibling modules and force every env var the integration
suite needs), the drift guard parses each conftest source via the
:mod:`ast` module and pulls the table names out of either the
constant's literal tuple (acceptance) or every string literal that
starts with ``TRUNCATE TABLE`` (integration). Pure-AST extraction is
side-effect-free, fast, and tolerates conftests being uncollectable
in a no-Docker sandbox -- exactly the run shape this drift guard
targets.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sqlalchemy import Table

from meho_backplane.db.models import Base

# ---------------------------------------------------------------------------
# Conftest locations -- resolved off this test file's path so the assertion
# is robust to the repo's checkout location (worktrees, CI clones, etc.).
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_INTEGRATION_CONFTEST = _TESTS_DIR / "integration" / "conftest.py"
_ACCEPTANCE_CONFTEST = _TESTS_DIR / "acceptance" / "conftest.py"


# ---------------------------------------------------------------------------
# Metadata-introspection helper
# ---------------------------------------------------------------------------


def _tables_with_tenant_fk() -> frozenset[str]:
    """Return every mapped table whose column FK targets ``tenant.id``.

    Walks :data:`Base.metadata.tables`; for each column, checks every
    :class:`sqlalchemy.ForeignKey` attached. ``target_fullname`` is the
    ``"schema.column"`` string the FK was declared with (e.g.
    ``"tenant.id"``); using it avoids resolving the target column,
    which would require the referenced table to be present in the
    metadata at evaluation time.
    """
    out: set[str] = set()
    for table_name, table in Base.metadata.tables.items():
        assert isinstance(table, Table)  # narrow for mypy / ruff alike
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.target_fullname == "tenant.id":
                    out.add(table_name)
                    break
    return frozenset(out)


# ---------------------------------------------------------------------------
# AST-based truncate-list extractors
# ---------------------------------------------------------------------------


def _extract_truncate_tables_constant(
    conftest_path: Path,
    *,
    constant_name: str = "_TRUNCATE_TABLES",
) -> frozenset[str]:
    """Pull the string-tuple literal assigned to ``constant_name``.

    Used for the acceptance conftest, where the truncate list is a
    module-level ``_TRUNCATE_TABLES: tuple[str, ...] = (...)``. The
    extractor walks the AST for an ``Assign`` (or :class:`ast.AnnAssign`)
    whose target name matches and whose value is a tuple of string
    constants. Anything outside that shape raises so a future
    refactor cannot silently turn the guard into a no-op.

    Returns a :class:`frozenset` of table names so callers cannot
    mutate the extracted set in place; the assertion in the test
    consumes it as a read-only collection.
    """
    source = conftest_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(conftest_path))

    for node in ast.walk(tree):
        target_value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == constant_name:
                    target_value = node.value
                    break
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == constant_name
        ):
            target_value = node.value

        if target_value is None:
            continue

        if not isinstance(target_value, ast.Tuple):
            raise AssertionError(
                f"{conftest_path}: expected `{constant_name}` to be a tuple literal; "
                f"got {type(target_value).__name__}. Adjust the drift guard "
                f"({__file__}) if the shape changed deliberately."
            )

        tables: set[str] = set()
        for element in target_value.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                raise AssertionError(
                    f"{conftest_path}: `{constant_name}` tuple contains a non-string "
                    f"element ({ast.dump(element)}). Drift guard cannot verify it; "
                    f"adjust the shape or extend the extractor."
                )
            tables.add(element.value)
        return frozenset(tables)

    raise AssertionError(
        f"{conftest_path}: no module-level `{constant_name}` assignment found. "
        f"Drift guard cannot verify the truncate coverage; either restore the "
        f"constant or update the extractor in {__file__}."
    )


# Matches ``TRUNCATE TABLE <names>`` at the start of a string literal.
# ``re.IGNORECASE`` keeps the matcher tolerant to ``truncate table`` /
# ``Truncate Table`` styles a future refactor might introduce; the
# ``re.DOTALL`` flag lets a SQL string that spans line-continued
# literals match (Python implicitly concatenates adjacent string
# literals at compile time, but ``ast.Constant.value`` may still carry
# embedded newlines if a future author uses a triple-quoted string).
_TRUNCATE_PREFIX_RE = re.compile(
    r"^\s*TRUNCATE\s+TABLE\s+(?P<tables>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _extract_truncate_tables_inline_sql(conftest_path: Path) -> frozenset[str]:
    """Union the table names from every inline ``TRUNCATE TABLE ...`` literal.

    Used for the integration conftest, which embeds the table set in
    each ``conn.execute(text("TRUNCATE TABLE a, b, ..."))`` call rather
    than a module-level constant. There are currently two such
    literals (``pg_engine`` and ``pg_engine_empty_tenant``); both must
    list every tenant-FK table, so the drift guard unions them and
    asserts the union covers the metadata. (If the two literals
    diverge, a per-call drift catches it at the integration-suite
    level when PG rejects the truncate.)

    Anchored on ``^TRUNCATE TABLE`` (with whitespace tolerance) so the
    extractor ignores conftest docstrings or comments that *mention*
    truncation. The ``WHERE``/``UPDATE``/``INSERT`` shapes would not
    match either.

    A conftest with zero matching literals raises -- a silent
    no-match would re-create the gap this drift guard exists to
    close.
    """
    source = conftest_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(conftest_path))

    union: set[str] = set()
    matched = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        m = _TRUNCATE_PREFIX_RE.match(node.value)
        if not m:
            continue
        matched += 1
        raw = m.group("tables")
        # The captured group may end at the literal boundary or at a
        # trailing clause (CASCADE / RESTART IDENTITY etc.). Split on
        # comma and strip whitespace; the conftest's shape today is a
        # simple comma list, but the stripping keeps the extractor
        # honest if a CASCADE is appended later.
        for part in raw.split(","):
            name = part.strip()
            # Defensive: filter trailing keywords (CASCADE / RESTART
            # IDENTITY / CONTINUE IDENTITY) if they ride on the last
            # name. PG accepts ``TRUNCATE TABLE foo, bar CASCADE``;
            # the last entry would then be ``"bar CASCADE"``.
            name = name.split()[0] if name else name
            if name:
                union.add(name)

    if matched == 0:
        raise AssertionError(
            f"{conftest_path}: no inline `TRUNCATE TABLE ...` SQL literal found. "
            f"Drift guard cannot verify the truncate coverage; either restore the "
            f"literal or update the extractor in {__file__}."
        )

    return frozenset(union)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _format_missing(
    missing: frozenset[str],
    *,
    conftest_path: Path,
    truncate_source: str,
) -> str:
    """Render the failure message that names offending tables + the file.

    The message format is deliberately single-line-friendly so a CI log
    surfaces the fix in one grep: tables, conftest path, source shape.
    """
    sorted_missing = sorted(missing)
    return (
        f"New tenant-FK table(s) {sorted_missing} not listed in "
        f"{conftest_path} ({truncate_source}). "
        "Every ORM table whose `tenant_id` column has `ForeignKey('tenant.id')` "
        "must appear in this conftest's TRUNCATE list, else PG rejects the "
        "per-test reset with `cannot truncate a table referenced in a foreign "
        "key constraint`. Add the missing name(s) and re-run."
    )


def test_acceptance_conftest_covers_every_tenant_fk_table() -> None:
    """Every ``ForeignKey('tenant.id')`` table is in the acceptance truncate list."""
    fk_tables = _tables_with_tenant_fk()
    truncate_tables = _extract_truncate_tables_constant(_ACCEPTANCE_CONFTEST)
    missing = fk_tables - truncate_tables
    assert not missing, _format_missing(
        missing,
        conftest_path=_ACCEPTANCE_CONFTEST,
        truncate_source="_TRUNCATE_TABLES constant",
    )


def test_integration_conftest_covers_every_tenant_fk_table() -> None:
    """Every ``ForeignKey('tenant.id')`` table is in every integration TRUNCATE literal."""
    fk_tables = _tables_with_tenant_fk()
    truncate_tables = _extract_truncate_tables_inline_sql(_INTEGRATION_CONFTEST)
    missing = fk_tables - truncate_tables
    assert not missing, _format_missing(
        missing,
        conftest_path=_INTEGRATION_CONFTEST,
        truncate_source="inline `TRUNCATE TABLE ...` SQL literal",
    )


def test_drift_guard_actually_finds_tenant_fk_tables() -> None:
    """Sanity check: the metadata walk discovers at least one tenant-FK table.

    Without this, a regression that broke the FK-walk (e.g. a model
    refactor that moved every tenant_id FK to a deferred constraint
    shape ``target_fullname`` doesn't reach) would silently turn both
    coverage tests into no-ops -- an empty set is a subset of every
    set. Pinning the floor at one keeps the guard load-bearing.
    """
    fk_tables = _tables_with_tenant_fk()
    assert fk_tables, (
        "metadata walk found zero tables with `ForeignKey('tenant.id')`. "
        "Either every tenant-FK column was removed (unlikely) or the "
        "introspection in `_tables_with_tenant_fk` regressed."
    )
