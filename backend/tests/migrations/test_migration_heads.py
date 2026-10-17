# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/check_migration_heads.py`` (#4204).

The check must fail when two migration files take the same revision
number or when the chain has more than one head, and it must say what
to do. Synthetic chains are written into ``tmp_path``; the real
``backend/alembic/versions/`` tree is compared against Alembic's own
head computation so the two can never drift apart.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
from typing import Any, cast

import pytest
import yaml
from alembic.script import ScriptDirectory

_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[3]
_SCRIPT_PATH: pathlib.Path = _REPO_ROOT / "scripts" / "ci" / "check_migration_heads.py"
_REAL_VERSIONS_DIR: pathlib.Path = _REPO_ROOT / "backend" / "alembic" / "versions"
_WORKFLOW_PATH: pathlib.Path = _REPO_ROOT / ".github" / "workflows" / "migration-heads.yml"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("_check_migration_heads_under_test", _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their annotations through sys.modules, so the
    # module must be registered before it runs (the importlib recipe).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_module = _load_script_module()


def _write(directory: pathlib.Path, name: str, revision: str, down_revision: str | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    parent = "None" if down_revision is None else repr(down_revision)
    (directory / name).write_text(
        f'"""Synthetic migration."""\n\n'
        f"revision: str = {revision!r}\n"
        f"down_revision: str | None = {parent}\n\n\n"
        f"def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n"
    )


def _main_chain(tmp_path: pathlib.Path) -> pathlib.Path:
    """Three migrations as they would sit on ``main``: 0105 -> 0106 -> 0107."""
    main = tmp_path / "main"
    _write(main, "0105_a.py", "0105", None)
    _write(main, "0106_b.py", "0106", "0105")
    _write(main, "0107_c.py", "0107", "0106")
    return main


def test_real_versions_dir_has_one_head_and_matches_alembic() -> None:
    migrations, _, errors = _module.load_migrations(_REAL_VERSIONS_DIR, None)
    assert errors == []
    assert _module.find_problems(migrations) == []
    alembic_heads = ScriptDirectory(str(_REAL_VERSIONS_DIR.parent)).get_heads()
    assert _module.heads(migrations) == sorted(alembic_heads)


def test_one_head_passes(tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]) -> None:
    main = _main_chain(tmp_path)
    assert _module.main([str(main)]) == 0
    assert "single head 0107 (3 migrations)" in capfd.readouterr().out


def test_two_heads_fail(tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]) -> None:
    main = _main_chain(tmp_path)
    _write(main, "0108_d.py", "0108", "0106")
    assert _module.main([str(main)]) == 1
    err = capfd.readouterr().err
    assert "Migration chain check FAILED" in err
    assert "Alembic finds 2 heads" in err
    assert "0107 (0107_c.py)" in err
    assert "0108 (0108_d.py)" in err


def test_same_revision_in_two_files_fails(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    main = _main_chain(tmp_path)
    _write(main, "0107_other.py", "0107", "0106")
    assert _module.main([str(main)]) == 1
    err = capfd.readouterr().err
    assert "revision 0107 is used by 2 files: 0107_c.py, 0107_other.py" in err


def test_unknown_down_revision_fails(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    main = _main_chain(tmp_path)
    _write(main, "0108_d.py", "0108", "0199")
    assert _module.main([str(main)]) == 1
    assert "down_revision '0199' does not exist" in capfd.readouterr().err


def test_branch_behind_main_fails_with_rename_advice(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The branch took 0107 while main already has 0107. Alone, the
    branch looks fine; with ``--main`` the clash shows and the message
    says which number to use."""
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0105_a.py", "0105", None)
    _write(branch, "0106_b.py", "0106", "0105")
    _write(branch, "0107_mine.py", "0107", "0106")
    assert _module.main([str(branch)]) == 0
    assert _module.main([str(branch), "--main", str(main)]) == 1
    err = capfd.readouterr().err
    assert "revision 0107 is used by 2 files: 0107_c.py, 0107_mine.py" in err
    assert (
        "What to do: main is at 0107. Rename your migration to 0108 "
        "and set down_revision to '0107'." in err
    )


def test_branch_with_right_number_but_old_parent_gets_parent_advice(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0108_mine.py", "0108", "0106")
    assert _module.main([str(branch), "--main", str(main)]) == 1
    err = capfd.readouterr().err
    assert "Alembic finds 2 heads" in err
    assert "What to do: main is at 0107. Set down_revision to '0107'." in err


def test_branch_file_wins_over_main_file_with_same_name(tmp_path: pathlib.Path) -> None:
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0107_c.py", "0107", "0106")
    _write(branch, "0108_mine.py", "0108", "0107")
    assert _module.main([str(branch), "--main", str(main)]) == 0


def test_unparseable_file_fails(tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]) -> None:
    main = _main_chain(tmp_path)
    (main / "0108_broken.py").write_text("revision = 108\n")
    assert _module.main([str(main)]) == 1
    assert "0108_broken.py: 'revision' must be set to one string" in capfd.readouterr().err


def test_missing_directory_is_an_internal_error(tmp_path: pathlib.Path) -> None:
    assert _module.main([str(tmp_path / "nope")]) == 2
    assert _module.main([str(_main_chain(tmp_path)), "--main", str(tmp_path / "nope")]) == 2


def test_cli_against_real_versions_dir_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Migration chain OK" in result.stdout


def test_workflow_runs_on_every_pr_and_in_the_merge_queue() -> None:
    """The check is a required status check, so it must trigger with no
    path filter on pull requests and on merge_group. The re-check after
    a main push is a job of the push run, not a separate trigger."""
    with _WORKFLOW_PATH.open() as fh:
        workflow = cast("dict[Any, Any]", yaml.safe_load(fh))
    triggers = cast("dict[str, Any]", workflow.get("on", workflow.get(True)))
    assert set(triggers) == {"push", "pull_request", "merge_group"}
    assert "paths" not in (triggers["pull_request"] or {})
    assert workflow["jobs"]["check"]["name"] == "Migration chain (single head)"
    recheck = workflow["jobs"]["recheck-open-prs"]
    assert recheck["needs"] == "check"
    assert recheck["if"] == "github.event_name == 'push'"


def test_two_new_migrations_where_the_first_clashes_with_main(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Main is at 0107. The branch adds 0107 and 0108 as a chain. Only
    0108 is a new number, but both files are new, so the advice must
    renumber the whole chain, not just fix the parent."""
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0107_mine.py", "0107", "0106")
    _write(branch, "0108_mine_too.py", "0108", "0107")
    assert _module.main([str(branch), "--main", str(main)]) == 1
    err = capfd.readouterr().err
    assert "revision 0107 is used by 2 files: 0107_c.py, 0107_mine.py" in err
    assert (
        "What to do: main is at 0107. Your branch adds 2 migrations "
        "(0107_mine.py, 0108_mine_too.py). Number them 0108, 0109 as one chain, "
        "with the first one's down_revision set to '0107'." in err
    )


def test_two_new_migrations_with_right_numbers_but_stale_parent(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0108_mine.py", "0108", "0106")
    _write(branch, "0109_mine_too.py", "0109", "0108")
    assert _module.main([str(branch), "--main", str(main)]) == 1
    err = capfd.readouterr().err
    assert "Alembic finds 2 heads" in err
    assert "What to do: main is at 0107. Set down_revision of 0108_mine.py to '0107'." in err


def test_two_new_migrations_with_right_numbers_but_the_second_parent_wrong(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Main is at 0107. The branch adds 0108 and 0109, both pointing at
    0107. The first link is right, so the advice must name the second
    one; saying "set 0108's parent to 0107" would change nothing."""
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0108_mine.py", "0108", "0107")
    _write(branch, "0109_mine_too.py", "0109", "0107")
    assert _module.main([str(branch), "--main", str(main)]) == 1
    err = capfd.readouterr().err
    assert "Alembic finds 2 heads" in err
    assert "What to do: main is at 0107. Set down_revision of 0109_mine_too.py to '0108'." in err


def test_two_new_migrations_with_right_numbers_and_both_parents_wrong(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0108_mine.py", "0108", "0106")
    _write(branch, "0109_mine_too.py", "0109", "0106")
    assert _module.main([str(branch), "--main", str(main)]) == 1
    assert (
        "What to do: main is at 0107. Set down_revision of 0108_mine.py to '0107', "
        "0109_mine_too.py to '0108'." in capfd.readouterr().err
    )


def test_branch_file_that_replaces_a_main_file_is_not_counted_as_new(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A branch that edits 0107_c.py (same name as on main) and adds one
    new file with a stale parent gets advice about the new file only."""
    main = _main_chain(tmp_path)
    branch = tmp_path / "branch"
    _write(branch, "0107_c.py", "0107", "0106")
    _write(branch, "0108_mine.py", "0108", "0106")
    assert _module.main([str(branch), "--main", str(main)]) == 1
    assert "What to do: main is at 0107. Set down_revision to '0107'." in capfd.readouterr().err
