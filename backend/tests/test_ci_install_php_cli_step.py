# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behaviour tests for the "Install PHP CLI" step of the unit shards (#6312).

Two merge-queue runs failed in this step. The runner image has a package
mirror (the git-core PPA) that sometimes does not answer. ``apt-get update``
waited for it until ``timeout`` killed it, before it stored any index, and
the install then said "Unable to locate package php-cli".

The step now installs php from package files kept in the workflow cache,
with no network. Only when the cache is empty, or its files do not install,
does it download php-cli, and then apt gets a short wait and no retry for
each file, so a dead mirror is skipped. These tests take the real ``run:``
text of the step from ``ci.yml`` and run it with fake ``sudo``, ``apt-get``,
``dpkg`` and ``timeout`` on a ``PATH`` that holds nothing else, so the answer
does not depend on whether this machine has php.
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

# The fake dpkg logs to the same file. DPKG_OK decides whether the cached
# package files install.
_FAKE_DPKG = """#!/bin/bash
echo "dpkg $*" >> "$FAKE_LOG"
if [ "${DPKG_OK:-1}" = "1" ]; then
  printf '#!/bin/bash\\necho "PHP 8.3.0 (cli)"\\n' > "$FAKE_BIN/php"
  chmod +x "$FAKE_BIN/php"
  exit 0
fi
echo "dpkg: dependency problems prevent configuration of php-cli" >&2
exit 1
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
    jobs = cast(dict[str, Any], doc["jobs"])
    found = [
        step["run"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if step.get("name") == _STEP_NAME
    ]
    assert len(found) == 1, "ci.yml must have exactly one Install PHP CLI step"
    return cast("str", found[0])


def _run_step(
    tmp_path: Path, *, with_php: bool = False, cached_debs: bool = False, **env: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    real_timeout = shutil.which("timeout")
    real_bash = shutil.which("bash")
    if real_timeout is None or real_bash is None:
        pytest.skip("bash and timeout are needed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (
        ("apt-get", _FAKE_APT),
        ("dpkg", _FAKE_DPKG),
        ("sudo", _FAKE_SUDO),
        ("timeout", _FAKE_TIMEOUT),
    ):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "timeout.real").symlink_to(real_timeout)
    (bin_dir / "bash").symlink_to(real_bash)
    for tool in ("head", "sleep", "chmod", "printf", "mkdir", "chown", "id"):
        found = shutil.which(tool)
        if found is None:
            pytest.skip(f"{tool} is needed")
        (bin_dir / tool).symlink_to(found)
    if with_php:
        (bin_dir / "php").write_text('#!/bin/bash\necho "PHP 8.2.0 (cli)"\n')
        (bin_dir / "php").chmod(0o755)
    home = tmp_path / "home"
    debs = home / ".cache" / "meho-ci-php-debs"
    debs.mkdir(parents=True)
    if cached_debs:
        for name in ("php8.3-cli_8.3.6_amd64.deb", "php-common_93_all.deb"):
            (debs / name).write_bytes(b"")
    log = tmp_path / "apt.log"
    log.touch()
    output = tmp_path / "github_output"
    output.touch()
    script = tmp_path / "step.sh"
    script.write_text(_step_script())
    done = subprocess.run(
        [real_bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": str(bin_dir),
            "HOME": str(home),
            "GITHUB_OUTPUT": str(output),
            "FAKE_LOG": str(log),
            "FAKE_BIN": str(bin_dir),
            **env,
        },
        check=False,
    )
    return done, log.read_text() + output.read_text()


def test_php_already_on_the_runner_skips_apt(tmp_path: Path) -> None:
    done, log = _run_step(tmp_path, with_php=True)
    assert done.returncode == 0
    assert "php ready" in done.stdout
    assert log == ""


def test_cached_packages_install_with_no_download(tmp_path: Path) -> None:
    """Package downloads are not possible at all, and php still comes from the cache."""
    done, log = _run_step(
        tmp_path,
        cached_debs=True,
        APT_INSTALL_OK="0",
        APT_UPDATE_RC="100",
        PHP_DEBS_CACHE_HIT="true",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "php ready from the package cache: PHP 8.3.0" in done.stdout
    assert "apt-get" not in log
    assert "dpkg -i " in log
    assert "php8.3-cli_8.3.6_amd64.deb" in log
    assert "save=true" not in log


def test_cached_packages_that_do_not_install_fall_back_to_a_download(tmp_path: Path) -> None:
    done, log = _run_step(tmp_path, cached_debs=True, DPKG_OK="0", PHP_DEBS_CACHE_HIT="true")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::warning::the cached php-cli package files did not install" in done.stdout
    assert "install -y --fix-broken --no-install-recommends php-cli" in log
    assert "save=true" not in log


def test_empty_cache_downloads_into_the_cache_folder_and_saves(tmp_path: Path) -> None:
    done, log = _run_step(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "dpkg" not in log
    debs = tmp_path / "home" / ".cache" / "meho-ci-php-debs"
    assert f"Dir::Cache::archives={debs}" in log
    assert "APT::Keep-Downloaded-Packages=true" in log
    assert (debs / "partial").is_dir()
    assert "save=true" in log


def test_dead_mirror_does_not_stop_the_install(tmp_path: Path) -> None:
    """The update exits 100 for the skipped mirror; the install still runs."""
    done, log = _run_step(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PHP 8.3.0" in done.stdout
    assert "::warning::apt-get update could not refresh" in done.stdout
    assert "install -y --fix-broken --no-install-recommends php-cli" in log


def test_apt_gets_a_short_wait_and_no_retry(tmp_path: Path) -> None:
    """Without the short wait the fake update hangs, so this also proves the options."""
    _, log = _run_step(tmp_path)
    for line in log.splitlines():
        if not line.startswith("apt-get"):
            continue
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
    assert "package files are in the cache" in text


def test_the_cache_steps_share_one_path_and_key() -> None:
    doc = yaml.safe_load(_WORKFLOW.read_text())
    steps = [
        step
        for job in cast(dict[str, Any], doc["jobs"]).values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(("actions/cache/restore@", "actions/cache/save@"))
        and "php" in str(step.get("with", {}).get("path", ""))
    ]
    assert len(steps) == 2
    for step in steps:
        assert step["with"]["path"] == "~/.cache/meho-ci-php-debs"
        assert step["with"]["key"] == "${{ steps.php-debs.outputs.key }}"


def test_the_unit_shards_still_require_php() -> None:
    assert 'MEHO_REQUIRE_PHP: "1"' in _WORKFLOW.read_text()
