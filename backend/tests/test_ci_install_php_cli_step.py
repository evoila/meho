# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for the "Install PHP CLI" step of the unit shards (#6312).

Two merge-queue runs failed in this step. The runner image has a package
mirror (the git-core PPA) that sometimes does not answer. ``apt-get update``
waited for it until ``timeout`` killed it, before it stored any index, and
the install then said "Unable to locate package php-cli".

The step now gives apt a short wait and no retry for each file, so the dead
mirror is skipped and the Ubuntu indexes are stored. These tests take the
real ``run:`` text of the step from ``ci.yml`` and run it with fake
``sudo``, ``apt-get`` and ``timeout`` on a ``PATH`` that holds nothing else, so the
answer does not depend on whether this machine has php.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_STEP_NAME = "Install PHP CLI"

# The fake apt-get logs every call. It models the dead mirror: an update
# that is not given the short wait hangs (the test kills it with timeout),
# and an update that is given it ends with exit 100 at once, like the real
# one does after it skips a mirror. APT_INSTALL_OK decides the install.
_FAKE_APT = """#!/bin/bash
echo "apt-get $*" >> "$FAKE_LOG"
case " $* " in
  *" update "*)
    case " $* " in
      *Acquire::http::Timeout=10*) exit "${APT_UPDATE_RC:-100}" ;;
      *) sleep 30 ;;
    esac
    ;;
  *" install "*)
    if [ "${APT_INSTALL_OK:-1}" = "1" ]; then
      printf '#!/bin/bash\\necho "PHP 8.3.0 (cli)"\\n' > "$FAKE_BIN/php"
      chmod +x "$FAKE_BIN/php"
      exit 0
    fi
    echo "E: Unable to locate package php-cli" >&2
    exit 100
    ;;
esac
"""

# The real sudo is not needed: drop -n and the VAR=value words, run the rest.
_FAKE_SUDO = """#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in
    -n) shift ;;
    *=*) shift ;;
    *) break ;;
  esac
done
exec "$@"
"""

# A short `timeout`, so a test that hangs on purpose ends fast.
_FAKE_TIMEOUT = """#!/bin/bash
shift
exec timeout.real 3 "$@"
"""


def _step_script() -> str:
    doc = yaml.safe_load(_WORKFLOW.read_text())
    jobs = cast("dict[str, Any]", doc["jobs"])
    found = [
        step["run"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if step.get("name") == _STEP_NAME
    ]
    assert len(found) == 1, "ci.yml must have exactly one Install PHP CLI step"
    return cast("str", found[0])


def _run_step(
    tmp_path: Path, *, with_php: bool = False, **env: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    real_timeout = shutil.which("timeout")
    real_bash = shutil.which("bash")
    if real_timeout is None or real_bash is None:
        pytest.skip("bash and timeout are needed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (
        ("apt-get", _FAKE_APT),
        ("sudo", _FAKE_SUDO),
        ("timeout", _FAKE_TIMEOUT),
    ):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "timeout.real").symlink_to(real_timeout)
    (bin_dir / "bash").symlink_to(real_bash)
    for tool in ("head", "sleep", "chmod", "printf"):
        found = shutil.which(tool)
        if found is None:
            pytest.skip(f"{tool} is needed")
        (bin_dir / tool).symlink_to(found)
    if with_php:
        (bin_dir / "php").write_text('#!/bin/bash\necho "PHP 8.2.0 (cli)"\n')
        (bin_dir / "php").chmod(0o755)
    log = tmp_path / "apt.log"
    log.touch()
    script = tmp_path / "step.sh"
    script.write_text(_step_script())
    done = subprocess.run(
        [real_bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": str(bin_dir), "FAKE_LOG": str(log), "FAKE_BIN": str(bin_dir), **env},
        check=False,
    )
    return done, log.read_text()


def test_php_already_on_the_runner_skips_apt(tmp_path: Path) -> None:
    done, log = _run_step(tmp_path, with_php=True)
    assert done.returncode == 0
    assert "php ready" in done.stdout
    assert log == ""


def test_dead_mirror_does_not_stop_the_install(tmp_path: Path) -> None:
    """The update exits 100 for the skipped mirror; the install still runs."""
    done, log = _run_step(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PHP 8.3.0" in done.stdout
    assert "::warning::apt-get update could not refresh" in done.stdout
    assert "install -y --no-install-recommends php-cli" in log


def test_apt_gets_a_short_wait_and_no_retry(tmp_path: Path) -> None:
    """Without the short wait the fake update hangs, so this also proves the options."""
    _, log = _run_step(tmp_path)
    for line in log.splitlines():
        assert "Acquire::http::Timeout=10" in line
        assert "Acquire::https::Timeout=10" in line
        assert "Acquire::Retries=0" in line


def test_failed_install_says_setup_failure(tmp_path: Path) -> None:
    done, log = _run_step(tmp_path, APT_INSTALL_OK="0")
    assert done.returncode == 1
    assert "SETUP FAILURE, not a test failure" in done.stdout
    assert log.count("install -y") == 2
    assert not (tmp_path / "bin" / "php").exists()


def test_step_comment_names_the_new_way() -> None:
    text = _WORKFLOW.read_text()
    assert "Acquire::*::Timeout" in text
    assert "SETUP FAILURE" in text


def test_the_unit_shards_still_require_php() -> None:
    assert 'MEHO_REQUIRE_PHP: "1"' in _WORKFLOW.read_text()
