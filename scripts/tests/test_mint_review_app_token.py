#!/usr/bin/env python3
"""Focused contract tests for the review-App mint helper.

The fake MEHO, 1Password, and GitHub endpoints are intentionally local.  No
credential source or GitHub API is contacted by this test.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/setup/mint-review-app-token.sh"


class MintReviewAppTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp = Path(self.tempdir.name)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        self.key = self.temp / "disposable-test-key.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(self.key), "2048"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._stub(
            "meho",
            """\
            #!/bin/sh
            printf '%s\\n' "$@" >> "$ARGS_LOG"
            case "$*" in
              *client-id*) printf '%s' test-client-id ;;
              *private-key*) cat "$TEST_KEY_FILE" ;;
              *) exit 71 ;;
            esac
            """,
        )
        self._stub(
            "op",
            """\
            #!/bin/sh
            printf '%s\\n' "$@" >> "$ARGS_LOG"
            case "$*" in
              *client-id*) printf '%s' test-client-id ;;
              *private-key*) cat "$TEST_KEY_FILE" ;;
              *) exit 72 ;;
            esac
            """,
        )
        self._stub(
            "curl",
            """\
            #!/bin/sh
            printf '%s\\n' "$@" >> "$ARGS_LOG"
            case "$*" in
              */repos/*/installation*)
                printf '{"id":123}\\n%s' "${TEST_DISCOVER_STATUS:-200}"
                ;;
              *)
                if [ "${TEST_MINT_STATUS:-201}" = 201 ]; then
                  printf '%s' '{"token":"test-installation-token",'
                  printf '%s\\n201' '"expires_at":"2099-01-01T00:00:00Z"}'
                else
                  printf '{}\\n%s' "$TEST_MINT_STATUS"
                fi
                ;;
            esac
            """,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _stub(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(content))
        path.chmod(0o755)

    def run_helper(
        self, *args: str, stdin: bytes | None = None, **extra: str
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "TMPDIR": str(self.temp),
            "TEST_KEY_FILE": str(self.key),
            "ARGS_LOG": str(self.temp / "arguments.log"),
        } | extra
        return subprocess.run(
            [str(HELPER), *args], input=stdin, capture_output=True, env=env, check=False
        )

    def assert_no_secret_persisted(self, result: subprocess.CompletedProcess[bytes]) -> None:
        key_bytes = self.key.read_bytes()
        self.assertNotIn(key_bytes, result.stdout)
        self.assertNotIn(key_bytes, result.stderr)
        self.assertNotIn(key_bytes, (self.temp / "arguments.log").read_bytes())
        self.assertEqual([], list(self.temp.glob("tmp.*")), "helper must not stage a PEM in TMPDIR")

    def test_governed_vault_mints_from_pipe_without_persisting_key(self) -> None:
        result = self.run_helper(
            "--credential-source", "governed-vault", "--vault-target", "test-vault",
            "--vault-path", "automation/review-app/test",
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(b"test-installation-token\n", result.stdout)
        self.assert_no_secret_persisted(result)
        self.assertIn(b"minted installation token", result.stderr)

    def test_explicit_1password_fallback_mints(self) -> None:
        result = self.run_helper(
            "--credential-source", "1password", "--op-vault", "test-vault",
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(b"test-installation-token\n", result.stdout)
        self.assertIn(b"meho-review-app", (self.temp / "arguments.log").read_bytes())
        self.assert_no_secret_persisted(result)

    def test_existing_stdin_interface_mints_without_persisting_key(self) -> None:
        result = self.run_helper(
            "--client-id", "test-client-id", "--key-file", "-", stdin=self.key.read_bytes()
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(b"test-installation-token\n", result.stdout)
        self.assert_no_secret_persisted(result)

    def test_missing_governed_private_key_fails_closed(self) -> None:
        self._stub(
            "meho",
            "#!/bin/sh\ncase \"$*\" in *client-id*) printf test-client-id ;; *) exit 42 ;; esac\n",
        )
        result = self.run_helper(
            "--credential-source", "governed-vault", "--vault-target", "test-vault",
            "--vault-path", "automation/review-app/test",
        )
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertIn(b"private key", result.stderr)

    def test_governed_source_error_after_key_output_fails_closed(self) -> None:
        self._stub(
            "meho",
            """\
            #!/bin/sh
            case "$*" in
              *client-id*) printf test-client-id ;;
              *) cat "$TEST_KEY_FILE"; exit 42 ;;
            esac
            """,
        )
        result = self.run_helper(
            "--credential-source", "governed-vault", "--vault-target", "test-vault",
            "--vault-path", "automation/review-app/test",
        )
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertIn(b"private key", result.stderr)

    def test_github_error_statuses_leave_stdout_empty(self) -> None:
        for name, extra in (
            ("unauthorized", {"TEST_DISCOVER_STATUS": "401"}),
            ("not_found", {"TEST_DISCOVER_STATUS": "404"}),
            ("mint_failed", {"TEST_MINT_STATUS": "500"}),
        ):
            with self.subTest(name=name):
                result = self.run_helper(
                    "--credential-source", "governed-vault", "--vault-target", "test-vault",
                    "--vault-path", "automation/review-app/test", **extra,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(b"", result.stdout)


if __name__ == "__main__":
    unittest.main()
