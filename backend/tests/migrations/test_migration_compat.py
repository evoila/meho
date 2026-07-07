# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``scripts/ci/check_migration_compat.py``.

The CI guard is the gate that protects Goal #11's DoD bullet 3
(``helm rollback`` works without manual DB intervention) at the
schema layer. Weak coverage here lets a destructive migration slip
through and breaks rollback for every future v0.1 deployment, so the
matrix below is intentionally exhaustive: a positive test against
the real first migration shipped in T28, and one negative test per
banned pattern.

Synthetic destructive migrations are written into ``tmp_path``-rooted
fake versions directories and the CI script is invoked against that
directory via its CLI argument. The fixtures never land under
``backend/alembic/versions/`` and never trip the production guard's
own scan path.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import textwrap

import pytest

#: Repo root resolved relative to this test file. The script imports as
#: a module rather than via ``subprocess`` so failure messages render
#: as proper pytest tracebacks; the subprocess invocation is exercised
#: separately by :func:`test_cli_invocation_returns_nonzero_on_violation`.
_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[3]
_SCRIPT_PATH: pathlib.Path = _REPO_ROOT / "scripts" / "ci" / "check_migration_compat.py"


def _load_script_module() -> object:
    """Import the CI guard script as a module without polluting
    ``sys.path``. ``importlib.util.spec_from_file_location`` is the
    canonical Python 3.12 idiom — see the
    :mod:`importlib.util` docs.
    """
    spec = importlib.util.spec_from_file_location(
        "_check_migration_compat_under_test",
        _SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_module = _load_script_module()


def _write_migration(path: pathlib.Path, body: str) -> None:
    """Write *body* into *path* with a minimal Alembic file skeleton.

    The skeleton matches the shape Alembic's own template renders:
    a leading ``revision = "..."`` line plus the ``upgrade()`` /
    ``downgrade()`` definitions. Tests pass the *interior* of
    ``upgrade()`` (already 4-space-indented) via ``body``.
    """
    path.write_text(
        textwrap.dedent(
            '''\
            """Synthetic test migration."""

            from alembic import op
            import sqlalchemy as sa

            revision = "test"
            down_revision = None


            def upgrade():
            {body}


            def downgrade():
                pass
            '''
        ).format(body=body)
    )


def _versions_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a fresh fake versions directory under *tmp_path*."""
    versions = tmp_path / "versions"
    versions.mkdir()
    return versions


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_real_first_migration_passes() -> None:
    """The shipped ``0001_create_audit_log.py`` is purely additive in
    upgrade(), so the guard returns 0 violations on it.

    This is the canonical positive case: it locks in the contract
    that the first migration on the schema must pass, which means
    every subsequent CI run starts from a clean slate.
    """
    versions = _REPO_ROOT / "backend" / "alembic" / "versions"
    violations = _module.check_versions_dir(versions)
    assert violations == [], (
        "expected zero violations on the real versions directory, got:\n" + "\n".join(violations)
    )


def test_purely_additive_migration_passes(tmp_path: pathlib.Path) -> None:
    """A synthetic migration using only ``create_table`` /
    ``add_column`` / ``create_index`` returns zero violations."""
    versions = _versions_dir(tmp_path)
    _write_migration(
        versions / "0002_clean.py",
        body=(
            '    op.create_table("foo", sa.Column("id", sa.Integer(), primary_key=True))\n'
            '    op.add_column("foo", sa.Column("name", sa.Text(), nullable=True))\n'
            '    op.create_index("foo_name_idx", "foo", ["name"])'
        ),
    )
    assert _module.check_versions_dir(versions) == []


def test_alter_column_nullable_true_passes(tmp_path: pathlib.Path) -> None:
    """Adding a column or relaxing NOT NULL is purely additive and
    must not be flagged."""
    versions = _versions_dir(tmp_path)
    _write_migration(
        versions / "0003_relax.py",
        body='    op.alter_column("foo", "name", nullable=True)',
    )
    assert _module.check_versions_dir(versions) == []


def test_destructive_pattern_in_downgrade_only_passes(tmp_path: pathlib.Path) -> None:
    """``op.drop_table()`` inside ``downgrade()`` is allowed — the
    guard only inspects ``upgrade()`` because production never
    invokes ``alembic downgrade``.

    Exercises the same shape as the real
    ``0001_create_audit_log.py`` downgrade(), so a regression in
    the upgrade-only restriction would surface here as well as on
    :func:`test_real_first_migration_passes`.
    """
    versions = _versions_dir(tmp_path)
    (versions / "0004_downgrade_only.py").write_text(
        textwrap.dedent(
            '''\
            """Migration whose only destructive op is in downgrade()."""

            from alembic import op
            import sqlalchemy as sa

            revision = "test"
            down_revision = None


            def upgrade():
                op.create_table("foo", sa.Column("id", sa.Integer(), primary_key=True))


            def downgrade():
                op.drop_table("foo")
                op.drop_column("foo", "name")
                op.execute("DROP TABLE foo")
            '''
        )
    )
    assert _module.check_versions_dir(versions) == []


def test_missing_versions_dir_passes() -> None:
    """A nonexistent versions directory is treated as zero violations
    rather than an error — the guard predates the first migration in
    a fresh repo bootstrap."""
    assert _module.check_versions_dir(pathlib.Path("/nonexistent/path/should/not/exist")) == []


# ---------------------------------------------------------------------------
# Negative cases — one per banned pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected_substring"),
    [
        # Rule 1: op.drop_column
        (
            '    op.drop_column("audit_log", "request_id")',
            "op.drop_column() is not allowed",
        ),
        # Rule 2: op.drop_table
        (
            '    op.drop_table("audit_log")',
            "op.drop_table() is not allowed",
        ),
        # Rule 3: op.rename_table
        (
            '    op.rename_table("audit_log", "audit_events")',
            "op.rename_table() is not allowed",
        ),
        # Rule 4: op.alter_column with new_column_name (rename)
        (
            '    op.alter_column("audit_log", "request_id", new_column_name="req_id")',
            "new_column_name=...) is not allowed",
        ),
        # Rule 5: op.alter_column with nullable=False (NOT NULL)
        (
            '    op.alter_column("audit_log", "operator_sub", nullable=False)',
            "nullable=False) requires a nullable-phase-in shim",
        ),
        # Rule 6a: op.execute with raw DROP COLUMN
        (
            '    op.execute("ALTER TABLE audit_log DROP COLUMN request_id")',
            "DROP/RENAME/SET NOT NULL",
        ),
        # Rule 6b: op.execute with raw DROP TABLE
        (
            '    op.execute("DROP TABLE audit_log")',
            "DROP/RENAME/SET NOT NULL",
        ),
        # Rule 6c: op.execute with RENAME TABLE
        (
            '    op.execute("ALTER TABLE audit_log RENAME TO audit_events")',
            "raw SQL in upgrade() contains a banned destructive pattern",
        ),
        # Rule 6d: op.execute with SET NOT NULL
        (
            '    op.execute("ALTER TABLE audit_log ALTER COLUMN operator_sub SET NOT NULL")',
            "SET NOT NULL",
        ),
    ],
)
def test_banned_pattern_in_upgrade_is_rejected(
    tmp_path: pathlib.Path,
    body: str,
    expected_substring: str,
) -> None:
    """One synthetic migration per banned pattern. Each must produce
    at least one violation whose message contains the documented
    substring — that contract is what gives operators a clear,
    pattern-named diagnostic on a failing PR."""
    versions = _versions_dir(tmp_path)
    _write_migration(versions / "0099_destructive.py", body=body)
    violations = _module.check_versions_dir(versions)
    assert violations, f"expected at least one violation for body={body!r}"
    assert any(expected_substring in v for v in violations), (
        f"expected violation containing {expected_substring!r}; got:\n" + "\n".join(violations)
    )


def test_raw_sql_in_fstring_is_caught_by_text_pass(tmp_path: pathlib.Path) -> None:
    """The AST pass cannot resolve f-string payloads, but the regex
    backstop must still catch destructive SQL embedded in them.

    This is the load-bearing test for the AST/text dual-detector
    design: removing the regex pass would silently let migrations
    smuggle destructive ops past the guard via string formatting.
    """
    versions = _versions_dir(tmp_path)
    _write_migration(
        versions / "0098_fstring.py",
        body=('    table = "audit_log"\n    op.execute(f"DROP TABLE {table}")'),
    )
    violations = _module.check_versions_dir(versions)
    assert violations, "expected the regex backstop to catch the f-string raw SQL"
    assert any("raw SQL in upgrade()" in v for v in violations)


def test_destructive_pattern_in_helper_function_is_still_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """Helpers called from ``upgrade()`` are walked by ``ast.walk`` —
    a destructive op nested inside a helper *defined inside*
    upgrade() must still be flagged.

    The simpler shape "helper at module level, called from upgrade()"
    is intentionally *not* in scope: it would require call-graph
    analysis. Migrations should keep their op calls in the top-level
    upgrade() body anyway, which is the alembic-template
    convention.
    """
    versions = _versions_dir(tmp_path)
    (versions / "0097_nested.py").write_text(
        textwrap.dedent(
            '''\
            """Nested helper inside upgrade()."""

            from alembic import op

            revision = "test"
            down_revision = None


            def upgrade():
                def _do_drop():
                    op.drop_column("audit_log", "request_id")
                _do_drop()


            def downgrade():
                pass
            '''
        )
    )
    violations = _module.check_versions_dir(versions)
    assert violations
    assert any("op.drop_column() is not allowed" in v for v in violations)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_invocation_returns_zero_on_clean_dir(tmp_path: pathlib.Path) -> None:
    """The script's ``main()`` returns 0 on a clean directory passed
    via the CLI positional argument."""
    versions = _versions_dir(tmp_path)
    _write_migration(
        versions / "0002_clean.py",
        body='    op.create_table("foo", sa.Column("id", sa.Integer(), primary_key=True))',
    )
    rc = _module.main([str(versions)])
    assert rc == 0


def test_cli_invocation_returns_nonzero_on_violation(
    tmp_path: pathlib.Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``main()`` returns 1 *and* prints findings to stderr when a
    destructive pattern is present.

    Captured stderr is asserted to contain both the ``FAILED`` header
    and the path of the violating file — the structured shape
    operators reading the CI log rely on. The fixture is ``capfd``
    (file-descriptor-level) rather than ``capsys`` because the
    autouse secret-leak sweep in :mod:`tests.conftest` already
    holds a ``capfd`` capture and pytest forbids the two from
    overlapping.
    """
    versions = _versions_dir(tmp_path)
    _write_migration(
        versions / "0099_bad.py",
        body='    op.drop_table("audit_log")',
    )
    rc = _module.main([str(versions)])
    assert rc == 1
    captured = capfd.readouterr()
    assert "Migration compatibility check FAILED" in captured.err
    assert "0099_bad.py" in captured.err


def test_subprocess_invocation_against_real_versions_dir() -> None:
    """End-to-end: the script runs as ``python scripts/ci/...`` from
    the repo root and exits 0 against the shipped versions tree.

    Mirrors what the GitHub Actions workflow runs on every PR
    touching ``backend/alembic/versions/``. The subprocess shape
    surfaces shebang / interpreter-discovery regressions that an
    in-process import would mask.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"expected exit 0; got {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
