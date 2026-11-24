#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of meho-host reset-second-factor (#4746) on a fake machine.

meho-host checks root and the arguments, then runs the reset module in the
backplane container of the core with RKE2's kubectl. The fake kubectl here
prints what the module would print and exits with its exit code. The module
itself, with saved Keycloak answers and its audit row, is tested in
backend/tests/test_core_reset_second_factor.py."""

from __future__ import annotations

import subprocess
import textwrap

from harness import _write_script
from host_harness import MEHO_HOST, HostTestCase

RKE2_KUBECTL = "/var/lib/rancher/rke2/bin/kubectl"
EXEC = (
    "kubectl --kubeconfig {root}/etc/rancher/rke2/rke2.yaml --request-timeout=10s "
    "--request-timeout=2m -n meho exec deploy/meho -c backplane -- "
    "python -m meho_backplane.core.reset_second_factor --by {by} --host meho.example.com -- {user}"
)
DONE = (
    "[ok]   removed the one-time code of admin\n"
    "[ok]   removed the recovery codes of admin\n"
    "[ok]   wrote audit row 5b0c9a1e-0000-4000-8000-000000000001\n"
)
UNKNOWN = "[FAIL] there is no user nobody in realm meho. Nothing was changed. Fix: check it.\n"

# The module's answer: its output from reset_out and its exit code from reset_rc.
KUBECTL_STUB = textwrap.dedent("""\
    cat "$MEHO_FAKE/reset_out"
    exit "$(cat "$MEHO_FAKE/reset_rc")"
    """)


class ResetSecondFactorTests(HostTestCase):
    def setUp(self) -> None:
        super().setUp()
        _write_script(self.host.host_path(RKE2_KUBECTL), KUBECTL_STUB)
        self.host.answer("reset_out", DONE)
        self.host.answer("reset_rc", "0")

    def exec_line(self, user: str, by: str = "root") -> str:
        return EXEC.format(root=self.host.root, by=by, user=user)

    def test_as_root_runs_the_reset_in_the_core(self) -> None:
        result = self.host.run("reset-second-factor", "admin")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(DONE, result.stdout)
        self.assertIn(self.exec_line("admin"), self.host.calls().splitlines())

    def test_names_the_person_behind_sudo(self) -> None:
        env = self.host.environment()
        env["SUDO_USER"] = "alice"

        result = subprocess.run(
            [str(MEHO_HOST), "reset-second-factor", "admin"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(self.exec_line("admin", by="alice"), self.host.calls().splitlines())

    def test_not_root_changes_nothing_and_exits_2(self) -> None:
        self.host.answer("uid", "1000")

        result = self.host.run("reset-second-factor", "admin")

        self.assertEqual(2, result.returncode, self.output(result))
        self.assertIn("[FAIL] meho-host does not run as root. Nothing was changed.", result.stderr)
        self.assertNotIn("kubectl", self.host.calls())

    def test_unknown_user_exits_1(self) -> None:
        self.host.answer("reset_out", UNKNOWN)
        self.host.answer("reset_rc", "3")

        result = self.host.run("reset-second-factor", "nobody")

        self.assertEqual(1, result.returncode, self.output(result))
        self.assertIn("there is no user nobody", result.stdout)
        self.assertNotIn("stopped at", self.output(result))

    def test_core_not_reached_fails_with_a_fix(self) -> None:
        self.host.answer("reset_out", "")
        self.host.answer("reset_rc", "1")

        result = self.host.run("reset-second-factor", "admin")

        self.assertEqual(1, result.returncode, self.output(result))
        self.assertIn(
            "[FAIL] the reset did not run to its end in the MEHO core (exit code 1).", result.stderr
        )
        self.assertIn("safe to run twice", result.stderr)

    def test_wrong_usage_exits_2(self) -> None:
        for args in ((), ("a", "b"), ("",), ("--all",)):
            with self.subTest(args=args):
                result = self.host.run("reset-second-factor", *args)
                self.assert_usage_error(result, "reset-second-factor takes one user name")
        self.assertNotIn("kubectl", self.host.calls())

    def test_help_names_the_command(self) -> None:
        result = self.host.run("--help")

        self.assertEqual(0, result.returncode)
        self.assertIn("reset-second-factor <user>", result.stdout)
