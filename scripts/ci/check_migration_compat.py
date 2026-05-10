#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reject Alembic migrations containing destructive patterns.

Backward-compatibility discipline (Goal #11 DoD bullet 3 — ``helm
rollback`` works without manual DB intervention) requires every
migration to be **purely additive**: add tables, add columns
(nullable or with default), create indexes; never DROP, never RENAME,
never add NOT NULL to existing columns without a documented nullable
phase-in. This script is the CI gate that enforces that rule.

Detection runs over the ``upgrade()`` function only. The
``downgrade()`` function is intentionally exempt because:

* Production never invokes ``alembic downgrade`` — rollback in
  Goal #11 is image-revert + forward-compat schema discipline.
* The first migration's ``downgrade()`` legitimately drops the
  table it just created (so the migration is reversible at
  development time, on an empty schema), and that pattern would
  trip a flat scan of the whole module.

Forward-only scanning preserves the "no destructive operations on
production data" contract while leaving development-time rollback
symmetry intact in source.

Banned patterns inside ``upgrade()``:

1. ``op.drop_column(...)``
2. ``op.drop_table(...)``
3. ``op.rename_table(...)``
4. ``op.alter_column(..., new_column_name=...)`` — column rename
5. ``op.alter_column(..., nullable=False)`` — adds NOT NULL
   without a documented nullable-phase-in shim
6. ``op.execute(<str>)`` whose payload contains
   ``DROP COLUMN``, ``DROP TABLE``, ``RENAME TABLE``,
   ``RENAME COLUMN``, or ``ALTER TABLE ... ALTER COLUMN ...
   SET NOT NULL`` (case-insensitive) — backstop for raw-SQL
   smuggling around the Python op-API checks.

The detector uses both an AST pass (rules 1-5 plus the
op.execute-with-string-literal sub-case of rule 6) and a regex pass
(rule 6's text-grep backstop covering f-strings / variable arg
patterns the AST can't follow). Reading the source twice — once as
text, once as AST — is intentional belt-and-braces.

Exit codes
----------

* 0 — no violations
* 1 — at least one violation (each printed to stderr with
  ``<path>:<lineno>: <message>``)
* 2 — internal error (file unreadable, syntax error, etc.)
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

#: Path to the migrations directory, relative to the repo root.
#: The script is invoked from the repo root by both the GitHub
#: Actions workflow and the local pytest suite (which monkeypatches
#: this constant to point at synthetic test fixtures).
DEFAULT_VERSIONS_DIR: pathlib.Path = pathlib.Path("backend/alembic/versions")

#: ``op.<name>`` calls banned outright inside ``upgrade()``.
BANNED_OPS: frozenset[str] = frozenset(
    {
        "drop_column",
        "drop_table",
        "rename_table",
    }
)

#: ``op.alter_column(..., <flag>=...)`` keyword arguments that signal
#: a destructive change (column rename). Adding NOT NULL is handled
#: separately because it is detected by the value, not the keyword
#: name.
ALTER_COLUMN_BANNED_KWARGS: frozenset[str] = frozenset({"new_column_name"})

#: Regex matching destructive SQL inside ``op.execute(<str>)`` payloads
#: and as a final text-grep backstop. The alternation covers:
#:
#: * ``DROP COLUMN`` / ``DROP TABLE``
#: * ``RENAME TO`` / ``RENAME COLUMN`` — PostgreSQL canonical
#:   ``ALTER TABLE <t> RENAME TO <new>`` and
#:   ``ALTER TABLE <t> RENAME COLUMN <c> TO <new>`` shapes; also
#:   matches the legacy ``RENAME TABLE`` / ``RENAME COLUMN`` direct
#:   forms in case a future migration uses them.
#: * ``ALTER TABLE <t> ALTER COLUMN <c> SET NOT NULL`` (PG syntax)
#: * ``ALTER TABLE <t> MODIFY <c> ... NOT NULL`` (MySQL syntax — kept
#:   even though ADR 0004 pins PG, in case a future driver lands)
#:
#: The pattern is intentionally permissive on whitespace
#: (``\s+`` / ``\s*``) and case-insensitive (``re.IGNORECASE``).
RAW_SQL_BANNED_RE: re.Pattern[str] = re.compile(
    r"\b("
    r"DROP\s+(COLUMN|TABLE)"
    r"|RENAME\s+(TABLE|COLUMN|TO)\b"
    r"|ALTER\s+TABLE\s+\S+\s+ALTER\s+COLUMN\s+\S+\s+SET\s+NOT\s+NULL"
    r"|ALTER\s+TABLE\s+\S+\s+MODIFY\s+\S+[^,]*NOT\s+NULL"
    r")",
    re.IGNORECASE,
)


def _is_op_call(node: ast.Call) -> tuple[bool, str | None]:
    """Return ``(is_op_call, attr_name)`` for ``op.<attr>(...)`` calls.

    Returns ``(False, None)`` for any other call shape (including
    chained attribute access like ``foo.op.drop_table()``, which we
    intentionally do not match — Alembic's migration files always
    write the bare ``op.`` prefix).
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return (False, None)
    if not isinstance(func.value, ast.Name):
        return (False, None)
    if func.value.id != "op":
        return (False, None)
    return (True, func.attr)


def _scan_upgrade_body(
    path: pathlib.Path,
    upgrade_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Walk every ``op.<x>(...)`` call inside ``upgrade()`` and
    collect violation strings."""
    violations: list[str] = []
    for node in ast.walk(upgrade_node):
        if not isinstance(node, ast.Call):
            continue
        is_op, attr = _is_op_call(node)
        if not is_op or attr is None:
            continue
        if attr in BANNED_OPS:
            violations.append(
                f"{path}:{node.lineno}: op.{attr}() is not allowed in upgrade() "
                f"(destructive — violates backward-compat discipline)"
            )
            continue
        if attr == "alter_column":
            for kw in node.keywords:
                if kw.arg in ALTER_COLUMN_BANNED_KWARGS:
                    violations.append(
                        f"{path}:{node.lineno}: op.alter_column(..., {kw.arg}=...) "
                        f"is not allowed (column rename is destructive)"
                    )
                if (
                    kw.arg == "nullable"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                ):
                    violations.append(
                        f"{path}:{node.lineno}: op.alter_column(..., nullable=False) "
                        f"requires a nullable-phase-in shim "
                        f"(see backend/docs nullable-phase-in pattern)"
                    )
            continue
        if attr == "execute":
            # Match string-literal payloads — f-strings / variable
            # arguments are caught by the regex backstop below.
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and RAW_SQL_BANNED_RE.search(node.args[0].value)
            ):
                violations.append(
                    f"{path}:{node.lineno}: op.execute(<str>) contains a banned "
                    f"destructive pattern (DROP/RENAME/SET NOT NULL)"
                )
    return violations


def _scan_text(path: pathlib.Path, src: str) -> list[str]:
    """Regex backstop over the raw migration source.

    Catches destructive SQL embedded in f-strings, variables, or any
    construction the AST pass cannot statically resolve. Restricted
    to the upgrade() block via a coarse text slice so the regex does
    not flag downgrade()-only patterns.
    """
    upgrade_start = src.find("def upgrade(")
    if upgrade_start == -1:
        return []
    # Stop at the next top-level ``def`` (matches ``def downgrade(`` and
    # any later helper). ``\n\ndef`` is the conventional boundary
    # rendered by ``black`` / ``ruff format`` and the alembic template.
    after = src[upgrade_start:]
    next_def = re.search(r"\n\ndef\s+\w+\(", after[len("def upgrade(") :])
    upgrade_block = after if next_def is None else after[: len("def upgrade(") + next_def.start()]
    violations: list[str] = []
    for match in RAW_SQL_BANNED_RE.finditer(upgrade_block):
        # Compute the line number relative to the original file by
        # counting newlines up to and including the absolute match
        # offset.
        absolute_offset = upgrade_start + match.start()
        lineno = src.count("\n", 0, absolute_offset) + 1
        violations.append(
            f"{path}:{lineno}: raw SQL in upgrade() contains a banned destructive "
            f"pattern ({match.group(0).strip()!r})"
        )
    return violations


def check_file(path: pathlib.Path) -> list[str]:
    """Return all violations for a single migration file.

    Combines the AST pass over the ``upgrade()`` body with the
    text-grep backstop. Duplicate-line violations from the two
    detectors are deduplicated to keep the operator-facing report
    readable.
    """
    src = path.read_text()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno or 1}: syntax error: {exc.msg}"]
    upgrade_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upgrade":
            upgrade_node = node
            break
    violations: list[str] = []
    if upgrade_node is not None:
        violations.extend(_scan_upgrade_body(path, upgrade_node))
    violations.extend(_scan_text(path, src))
    # Deduplicate while preserving order — the AST and regex passes
    # often emit the same finding for the same line of raw SQL.
    seen: set[str] = set()
    deduped: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def check_versions_dir(versions_dir: pathlib.Path) -> list[str]:
    """Scan every ``*.py`` migration file in ``versions_dir``.

    ``__init__.py`` and any non-Python file is skipped silently.
    Files whose name starts with ``_`` (Alembic uses these for
    helpers / templates) are also skipped — they are not migration
    revisions.
    """
    if not versions_dir.exists():
        return []
    all_violations: list[str] = []
    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        all_violations.extend(check_file(path))
    return all_violations


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    Optional positional arg: path to a versions directory (defaults
    to :data:`DEFAULT_VERSIONS_DIR`). The argument exists so the
    pytest suite can point the guard at synthetic fixtures without
    monkeypatching module state.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print(
            "usage: check_migration_compat.py [versions_dir]",
            file=sys.stderr,
        )
        return 2
    target = pathlib.Path(args[0]) if args else DEFAULT_VERSIONS_DIR
    try:
        violations = check_versions_dir(target)
    except OSError as exc:
        print(f"check_migration_compat: cannot read {target}: {exc}", file=sys.stderr)
        return 2
    if violations:
        print("Migration compatibility check FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
