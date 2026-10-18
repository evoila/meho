#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail when the Alembic migration chain has more than one head.

Migrations in ``backend/alembic/versions/`` are numbered by hand. Two
pull requests made at the same time often take the same next number and
the same parent. Git sees no conflict, because the file names differ.
The app does: ``get_current_head()`` raises on two heads, so every
database-backed job in the merge queue fails with hundreds of
``Multiple head revisions`` errors.

This script finds that clash with no database and no Alembic install.
It reads ``revision`` and ``down_revision`` from every migration file
with the ``ast`` module, then checks three things:

1. every ``revision`` id is used by exactly one file,
2. every ``down_revision`` points at a revision that exists,
3. exactly one revision is a head (no other file names it as parent).

``--main DIR`` adds the migration files that are on ``main`` to the
check. A file in the checked directory wins over a file with the same
name on ``main``. The union is what the merge result will contain, so
the check gives the same answer on a branch that is behind ``main`` as
it would after the merge. Knowing ``main`` also lets the failure message
say exactly what to do: which number ``main`` is at, which number to use
next, and which ``down_revision`` to set.

Exit codes
----------

* 0 — one head, no duplicate ids, no dangling parents
* 1 — at least one problem (printed to stderr)
* 2 — internal error (directory unreadable, bad arguments)
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from dataclasses import dataclass

#: Path to the migrations directory, relative to the repo root. The
#: workflow runs the script from the repo root.
DEFAULT_VERSIONS_DIR: pathlib.Path = pathlib.Path("backend/alembic/versions")


@dataclass(frozen=True)
class Migration:
    """The two module-level names Alembic reads from a migration file."""

    path: pathlib.Path
    revision: str
    down_revisions: tuple[str, ...]


def _migration_files(versions_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """Map file name to path for every migration file in *versions_dir*.

    ``__init__.py`` and files starting with ``_`` are skipped, the same
    rule :mod:`check_migration_compat` uses.
    """
    if not versions_dir.is_dir():
        return {}
    return {
        path.name: path
        for path in sorted(versions_dir.glob("*.py"))
        if path.name != "__init__.py" and not path.name.startswith("_")
    }


def _string_values(node: ast.expr) -> tuple[str, ...] | None:
    """Return the string(s) a ``revision`` / ``down_revision`` value holds.

    Alembic accepts a string, ``None``, or a tuple of strings (a merge
    migration has two parents). Anything else returns ``None`` so the
    caller can report the file as unreadable.
    """
    if isinstance(node, ast.Constant):
        if node.value is None:
            return ()
        if isinstance(node.value, str):
            return (node.value,)
        return None
    if isinstance(node, ast.Tuple | ast.List):
        values: list[str] = []
        for element in node.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                return None
            values.append(element.value)
        return tuple(values)
    return None


def parse_migration(path: pathlib.Path) -> Migration | str:
    """Read ``revision`` and ``down_revision`` from one migration file.

    Returns a :class:`Migration`, or an error string when the file does
    not parse or does not assign both names to plain string values.
    """
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return f"{path.name}: cannot parse file ({exc})"
    revision: tuple[str, ...] | None = None
    down_revisions: tuple[str, ...] | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        if target.id == "revision":
            revision = _string_values(value)
        elif target.id == "down_revision":
            down_revisions = _string_values(value)
    if revision is None or len(revision) != 1:
        return f"{path.name}: 'revision' must be set to one string"
    if down_revisions is None:
        return f"{path.name}: 'down_revision' must be a string, None, or a tuple of strings"
    return Migration(path=path, revision=revision[0], down_revisions=down_revisions)


def _parse_dir(files: dict[str, pathlib.Path]) -> tuple[dict[str, Migration], list[str]]:
    parsed: dict[str, Migration] = {}
    errors: list[str] = []
    for name, path in files.items():
        result = parse_migration(path)
        if isinstance(result, str):
            errors.append(result)
        else:
            parsed[name] = result
    return parsed, errors


def load_migrations(
    versions_dir: pathlib.Path, main_dir: pathlib.Path | None
) -> tuple[list[Migration], list[Migration], list[str]]:
    """Return ``(all_migrations, main_migrations, errors)``.

    ``all_migrations`` is the union of *versions_dir* and *main_dir*
    by file name, with *versions_dir* winning. ``main_migrations`` is
    the content of *main_dir* alone (empty when not given).
    """
    checked, errors = _parse_dir(_migration_files(versions_dir))
    on_main: dict[str, Migration] = {}
    if main_dir is not None:
        on_main, main_errors = _parse_dir(_migration_files(main_dir))
        errors.extend(main_errors)
    union = {**on_main, **checked}
    return list(union.values()), list(on_main.values()), sorted(set(errors))


def heads(migrations: list[Migration]) -> list[str]:
    """Return every revision no other migration names as its parent."""
    parents = {parent for m in migrations for parent in m.down_revisions}
    return sorted({m.revision for m in migrations if m.revision not in parents})


def _label(revision: str, migrations: list[Migration]) -> str:
    names = ", ".join(sorted(m.path.name for m in migrations if m.revision == revision))
    return f"{revision} ({names})"


def find_problems(migrations: list[Migration]) -> list[str]:
    """Return one line per problem in the chain; empty when it is healthy."""
    problems: list[str] = []
    by_revision: dict[str, list[str]] = {}
    for m in migrations:
        by_revision.setdefault(m.revision, []).append(m.path.name)
    for revision, names in sorted(by_revision.items()):
        if len(names) > 1:
            joined = ", ".join(names)
            problems.append(f"revision {revision} is used by {len(names)} files: {joined}")
    for m in migrations:
        for parent in m.down_revisions:
            if parent not in by_revision:
                problems.append(
                    f"{m.path.name}: down_revision {parent!r} does not exist in the chain"
                )
    found = heads(migrations)
    if len(found) > 1:
        listed = ", ".join(_label(revision, migrations) for revision in found)
        problems.append(f"Alembic finds {len(found)} heads, the app needs exactly one: {listed}")
    elif not found and migrations:
        problems.append("the chain has no head (every migration is named as a parent)")
    return problems


def _next_number(current: str, offset: int) -> str:
    return str(int(current) + offset).zfill(len(current))


def advice(main_migrations: list[Migration], migrations: list[Migration]) -> str:
    """One line that says what to change, based on where ``main`` is.

    New migrations are the files that are not on ``main``, found by file
    name. Their revision numbers are not used to find them, because a
    number that clashes with ``main`` is exactly the case to report.

    When the numbers already continue ``main``'s chain, every parent link
    is checked: the first new migration must point at ``main``'s head and
    each later one at the migration before it. The advice names every
    link that is wrong, so fixing one does not leave a second head behind.
    """
    generic = (
        "What to do: keep one chain. Every migration needs its own revision number, "
        "and exactly one migration may be the newest."
    )
    main_heads = heads(main_migrations)
    if len(main_heads) != 1 or not main_heads[0].isdigit():
        return generic
    main_head = main_heads[0]
    on_main = {m.path.name for m in main_migrations}
    new = sorted(
        (m for m in migrations if m.path.name not in on_main),
        key=lambda m: (m.revision, m.path.name),
    )
    if not new:
        return generic
    wanted = [_next_number(main_head, i + 1) for i in range(len(new))]
    if [m.revision for m in new] == wanted:
        parents = [main_head, *wanted[:-1]]
        broken = [
            (m, parent)
            for m, parent in zip(new, parents, strict=True)
            if m.down_revisions != (parent,)
        ]
        if not broken:
            return generic
        if len(new) == 1:
            return f"What to do: main is at {main_head}. Set down_revision to {main_head!r}."
        links = ", ".join(f"{m.path.name} to {parent!r}" for m, parent in broken)
        return f"What to do: main is at {main_head}. Set down_revision of {links}."
    if len(new) == 1:
        return (
            f"What to do: main is at {main_head}. Rename your migration to "
            f"{wanted[0]} and set down_revision to {main_head!r}."
        )
    files = ", ".join(m.path.name for m in new)
    numbers = ", ".join(wanted)
    return (
        f"What to do: main is at {main_head}. Your branch adds {len(new)} migrations "
        f"({files}). Number them {numbers} as one chain, with the first one's "
        f"down_revision set to {main_head!r}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when the Alembic migration chain has more than one head.",
    )
    parser.add_argument(
        "versions_dir",
        nargs="?",
        type=pathlib.Path,
        default=DEFAULT_VERSIONS_DIR,
        help="migrations directory to check (default: %(default)s)",
    )
    parser.add_argument(
        "--main",
        type=pathlib.Path,
        default=None,
        metavar="DIR",
        help="the same directory as it is on main; added to the check and used for advice",
    )
    args = parser.parse_args(argv)
    if not args.versions_dir.is_dir():
        print(f"check_migration_heads: not a directory: {args.versions_dir}", file=sys.stderr)
        return 2
    if args.main is not None and not args.main.is_dir():
        print(f"check_migration_heads: --main is not a directory: {args.main}", file=sys.stderr)
        return 2
    try:
        migrations, main_migrations, errors = load_migrations(args.versions_dir, args.main)
    except OSError as exc:
        print(f"check_migration_heads: cannot read migrations: {exc}", file=sys.stderr)
        return 2
    problems = errors + find_problems(migrations)
    if problems:
        print("Migration chain check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(advice(main_migrations, migrations), file=sys.stderr)
        return 1
    found = heads(migrations)
    print(f"Migration chain OK: single head {found[0]} ({len(migrations)} migrations).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
