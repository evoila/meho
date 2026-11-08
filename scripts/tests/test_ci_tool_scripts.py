# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test the scripts that CI jobs use to get their tools, with fake commands."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI_SCRIPTS = ROOT / "scripts/ci"
RETRY = CI_SCRIPTS / "retry.sh"
FETCH = CI_SCRIPTS / "fetch_with_retry.sh"
INSTALL_TOOL = CI_SCRIPTS / "install_tool.sh"
INSTALL_MODEL = CI_SCRIPTS / "install_spacy_model.sh"

URL = "https://downloads.example.test/file"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tool_program(printed_version: str) -> bytes:
    return f"#!/bin/sh\necho 'mytool has version {printed_version}'\n".encode()


class FakeCommandsTestCase(unittest.TestCase):
    """Run a script with fake curl, sleep and uv first on PATH."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / "fake-bin"
        self.home = self.tmp / "home"
        self.log = self.tmp / "calls.log"
        self.plan = self.tmp / "curl-plan"
        self.payload = self.tmp / "payload"
        self.github_path = self.tmp / "github-path"
        self.bin.mkdir()
        self.home.mkdir()
        self.log.touch()
        self.plan.write_text("ok\n")
        self.payload.write_bytes(b"content")
        self.fake(
            "sleep",
            """\
            #!/bin/sh
            echo "sleep $1" >> "$CALLS"
            """,
        )
        # Each try takes the next word of the plan; the last word repeats.
        self.fake(
            "curl",
            """\
            #!/bin/bash
            while [[ $# -gt 1 ]]; do
              [[ "$1" == -o ]] && out="$2"
              shift
            done
            echo "curl $1" >> "$CALLS"
            step="$(head -n 1 "$PLAN")"
            if [[ "$(wc -l < "$PLAN")" -gt 1 ]]; then
              sed -i 1d "$PLAN"
            fi
            case "$step" in
              ok) cp "$PAYLOAD" "$out" ;;
              bad) echo "error page" > "$out" ;;
              *) exit 22 ;;
            esac
            """,
        )
        self.fake(
            "uv",
            """\
            #!/bin/bash
            if [[ "$1 $2" == "run python" ]]; then
              [[ -n "${INSTALLED_MODEL:-}" ]] || exit 1
              echo "$INSTALLED_MODEL"
              exit 0
            fi
            echo "uv $*" >> "$CALLS"
            exit "${UV_PIP_STATUS:-0}"
            """,
        )

    def fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def fake_tool_on_runner(self, printed_version: str) -> None:
        path = self.bin / "mytool"
        path.write_bytes(tool_program(printed_version))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_script(self, script: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), *args],
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.home),
                "GITHUB_PATH": str(self.github_path),
                "CALLS": str(self.log),
                "PLAN": str(self.plan),
                "PAYLOAD": str(self.payload),
                **env,
            },
            cwd=self.tmp,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def downloads(self) -> int:
        return sum(call.startswith("curl ") for call in self.calls())

    def sleeps(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("sleep ")]

    def set_plan(self, *steps: str) -> None:
        self.plan.write_text("\n".join(steps) + "\n")


class RetryTests(FakeCommandsTestCase):
    def counting_command(self, *statuses: int) -> str:
        """A fake command that exits with each status in turn."""
        (self.tmp / "statuses").write_text("\n".join(str(status) for status in statuses) + "\n")
        self.fake(
            "flaky",
            f"""\
            #!/bin/bash
            echo "flaky $*" >> "$CALLS"
            status="$(head -n 1 {self.tmp}/statuses)"
            sed -i 1d {self.tmp}/statuses
            exit "$status"
            """,
        )
        return "flaky"

    def tries(self) -> int:
        return sum(call.startswith("flaky ") for call in self.calls())

    def test_success_on_the_first_try_does_not_wait(self) -> None:
        result = self.run_script(RETRY, self.counting_command(0), "arg")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["flaky arg"])

    def test_success_on_the_last_try_waits_longer_each_time(self) -> None:
        result = self.run_script(RETRY, self.counting_command(1, 1, 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tries(), 3)
        self.assertEqual(self.sleeps(), ["sleep 5", "sleep 15"])

    def test_stops_after_three_tries_with_the_last_exit_status(self) -> None:
        result = self.run_script(RETRY, self.counting_command(3, 4, 7, 0))
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.tries(), 3)
        self.assertEqual(self.sleeps(), ["sleep 5", "sleep 15"])
        self.assertIn("failed after 3 tries", result.stdout)

    def test_no_command_is_a_usage_error(self) -> None:
        self.assertEqual(self.run_script(RETRY).returncode, 2)


class FetchWithRetryTests(FakeCommandsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = self.tmp / "out"
        self.good_sha = sha256(b"content")

    def test_good_download_is_kept(self) -> None:
        result = self.run_script(FETCH, URL, str(self.out), self.good_sha)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.out.read_bytes(), b"content")
        self.assertEqual(self.calls(), [f"curl {URL}"])

    def test_checksum_is_optional(self) -> None:
        self.set_plan("bad")
        result = self.run_script(FETCH, URL, str(self.out))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.downloads(), 1)

    def test_failed_downloads_are_tried_again(self) -> None:
        self.set_plan("fail", "fail", "ok")
        result = self.run_script(FETCH, URL, str(self.out), self.good_sha)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.downloads(), 3)
        self.assertEqual(self.sleeps(), ["sleep 5", "sleep 15"])

    def test_wrong_checksum_counts_as_a_failed_try(self) -> None:
        self.set_plan("bad", "ok")
        result = self.run_script(FETCH, URL, str(self.out), self.good_sha)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.downloads(), 2)
        self.assertEqual(self.sleeps(), ["sleep 5"])
        self.assertEqual(self.out.read_bytes(), b"content")

    def test_stops_after_three_tries_and_leaves_no_file(self) -> None:
        self.set_plan("bad")
        result = self.run_script(FETCH, URL, str(self.out), self.good_sha)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.downloads(), 3)
        self.assertEqual(self.sleeps(), ["sleep 5", "sleep 15"])
        self.assertFalse(self.out.exists())

    def test_missing_arguments_are_a_usage_error(self) -> None:
        self.assertEqual(self.run_script(FETCH, URL).returncode, 2)
        self.assertEqual(self.downloads(), 0)


class InstallToolTests(FakeCommandsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool_dir = self.home / ".cache/meho-ci-tools/mytool-v2.12.2"
        self.cached = self.tool_dir / "mytool"
        self.installed = self.home / ".local/bin/mytool"

    def set_download(self, program: bytes) -> str:
        self.payload.write_bytes(program)
        return sha256(program)

    def install(self, sha: str = "0" * 64, *member: str, **env: str):
        return self.run_script(INSTALL_TOOL, "mytool", "v2.12.2", URL, sha, *member, **env)

    def test_tool_on_the_runner_at_the_version_is_used(self) -> None:
        for printed in ("2.12.2", "v2.12.2", "2.12.2 built with go1.26.2"):
            with self.subTest(printed=printed):
                self.fake_tool_on_runner(printed)
                result = self.install()
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("already on the runner", result.stdout)
                self.assertEqual(self.downloads(), 0)
                self.assertFalse(self.installed.exists())

    def test_similar_version_on_the_runner_is_rejected(self) -> None:
        for printed in ("2.12.20", "12.12.2", "2.12.2-rc1", "2.12.2.1", "2.12.1"):
            with self.subTest(printed=printed):
                self.log.write_text("")
                self.fake_tool_on_runner(printed)
                sha = self.set_download(tool_program("2.12.2"))
                result = self.install(sha)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("already on the runner", result.stdout)
                self.assertEqual(self.downloads(), 1)
                self.cached.unlink()

    def test_version_argument_is_passed_to_the_tool(self) -> None:
        self.fake(
            "mytool",
            """\
            #!/bin/sh
            [ "$1" = version ] && echo 'mytool v2.12.2 go1.26.2 linux/amd64'
            """,
        )
        result = self.install(VERSION_ARG="version")
        self.assertIn("already on the runner", result.stdout)
        self.assertEqual(self.downloads(), 0)

    def test_cached_tool_is_used_without_a_download(self) -> None:
        self.tool_dir.mkdir(parents=True)
        self.cached.write_bytes(tool_program("2.12.2"))
        self.cached.chmod(0o755)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("comes from the cache", result.stdout)
        self.assertEqual(self.downloads(), 0)
        self.assertEqual(self.installed.read_bytes(), tool_program("2.12.2"))
        self.assertEqual(self.github_path.read_text().strip(), str(self.installed.parent))

    def test_plain_program_is_downloaded_and_kept_for_the_cache(self) -> None:
        sha = self.set_download(tool_program("2.12.2"))
        result = self.install(sha)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [f"curl {URL}"])
        self.assertEqual(self.cached.read_bytes(), tool_program("2.12.2"))
        self.assertTrue(os.access(self.installed, os.X_OK))
        self.assertIn("2.12.2", result.stdout)

    def test_archive_member_is_extracted(self) -> None:
        member = self.tmp / "pkg/mytool"
        member.parent.mkdir()
        member.write_bytes(tool_program("2.12.2"))
        with tarfile.open(self.payload, "w:gz") as archive:
            archive.add(member, arcname="mytool-2.12.2-linux/mytool")
        sha = sha256(self.payload.read_bytes())
        result = self.install(sha, "mytool-2.12.2-linux/mytool")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cached.read_bytes(), tool_program("2.12.2"))
        self.assertEqual(self.installed.read_bytes(), tool_program("2.12.2"))

    def test_missing_archive_member_fails(self) -> None:
        member = self.tmp / "other"
        member.write_bytes(b"other")
        with tarfile.open(self.payload, "w:gz") as archive:
            archive.add(member, arcname="other")
        result = self.install(sha256(self.payload.read_bytes()), "mytool")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.cached.exists())
        self.assertFalse(self.installed.exists())

    def test_wrong_checksum_fails_after_three_tries(self) -> None:
        result = self.install("0" * 64)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.downloads(), 3)
        self.assertEqual(self.sleeps(), ["sleep 5", "sleep 15"])
        self.assertFalse(self.cached.exists())
        self.assertFalse(self.installed.exists())

    def test_failed_download_fails_the_install(self) -> None:
        self.set_plan("fail")
        sha = self.set_download(tool_program("2.12.2"))
        result = self.install(sha)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.downloads(), 3)
        self.assertFalse(self.installed.exists())

    def test_wrong_number_of_arguments_is_a_usage_error(self) -> None:
        result = self.run_script(INSTALL_TOOL, "mytool", "v2.12.2", URL)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.downloads(), 0)


class InstallSpacyModelTests(FakeCommandsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.wheel_name = "en_model-3.8.0-py3-none-any.whl"
        self.wheel = self.home / ".cache/meho-ci-tools/en_model-3.8.0" / self.wheel_name
        self.url = (
            "https://github.com/explosion/spacy-models/releases/download/"
            f"en_model-3.8.0/{self.wheel_name}"
        )
        self.good_sha = sha256(b"content")
        self.pip_install = f"uv pip install --no-deps {self.wheel}"

    def install(self, sha: str | None = None, **env: str):
        return self.run_script(INSTALL_MODEL, "en_model", "3.8.0", sha or self.good_sha, **env)

    def test_model_installed_at_the_version_is_used(self) -> None:
        result = self.install(INSTALLED_MODEL="3.8.0")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("already installed", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_similar_installed_version_is_replaced(self) -> None:
        result = self.install(INSTALLED_MODEL="3.8.01")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [f"curl {self.url}", self.pip_install])

    def test_cached_model_is_installed_without_a_download(self) -> None:
        self.wheel.parent.mkdir(parents=True)
        self.wheel.write_bytes(b"content")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("comes from the cache", result.stdout)
        self.assertEqual(self.calls(), [self.pip_install])

    def test_damaged_cached_model_is_downloaded_again(self) -> None:
        self.wheel.parent.mkdir(parents=True)
        self.wheel.write_bytes(b"half a file")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [f"curl {self.url}", self.pip_install])
        self.assertEqual(self.wheel.read_bytes(), b"content")

    def test_model_is_downloaded_and_kept_for_the_cache(self) -> None:
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [f"curl {self.url}", self.pip_install])
        self.assertEqual(self.wheel.read_bytes(), b"content")

    def test_wrong_checksum_fails_and_installs_nothing(self) -> None:
        result = self.install("0" * 64)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.downloads(), 3)
        self.assertFalse(self.wheel.exists())
        self.assertNotIn(self.pip_install, self.calls())

    def test_failed_install_fails_the_script(self) -> None:
        result = self.install(UV_PIP_STATUS="5")
        self.assertEqual(result.returncode, 5)

    def test_wrong_number_of_arguments_is_a_usage_error(self) -> None:
        result = self.run_script(INSTALL_MODEL, "en_model", "3.8.0")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
