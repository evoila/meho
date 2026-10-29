#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of two checks of the update (form-2 design, section 7): the
backup question, and the version step for RKE2 and MEHO."""

from __future__ import annotations

import unittest

from update_harness import INSTALL_RECORD, UpdateTestCase, run_update_lib

PROMPT = "Did you take a VM snapshot or a backup of this machine? Type yes to go on: "


class BackupTests(UpdateTestCase):
    def test_without_a_terminal_the_update_stops_before_any_change(self) -> None:
        before = self.snapshot()

        result = self.machine.run()

        line = self.assert_stopped_before_any_change(
            result,
            before,
            "The update needs a VM snapshot or a backup of this machine first, and this run has"
            " no terminal to ask. Nothing was changed.",
        )
        self.assertIn("run install.sh again with --confirm-backup.", line)
        self.assertNotIn(PROMPT, result.stdout)

    def test_any_answer_but_yes_stops_before_any_change(self) -> None:
        before = self.snapshot()
        for typed in ("no\n", "y\n", "yes please\n", "\n", "\x04"):
            with self.subTest(typed=typed):
                result = self.machine.run(terminal_input=typed)

                self.assert_stopped_before_any_change(
                    result, before, "The update stopped, because the answer was not yes."
                )
                self.assertIn(PROMPT, result.stdout)

    def test_yes_on_the_terminal_goes_on(self) -> None:
        for typed in ("yes\n", " Yes \n"):
            with self.subTest(typed=typed):
                result = self.machine.run(terminal_input=typed)

                self.assert_succeeds(result)
                self.assertIn(PROMPT, result.stdout)
                self.assertIn(
                    "[ok]   a VM snapshot or a backup of this machine exists", result.stdout
                )

    def test_confirm_backup_does_not_ask(self) -> None:
        result = self.machine.run("--confirm-backup", terminal_input="no\n")

        self.assert_succeeds(result)
        self.assertNotIn(PROMPT, result.stdout)
        self.assertIn(
            "[ok]   --confirm-backup: a VM snapshot or a backup of this machine exists",
            result.stdout,
        )

    def test_no_etcd_snapshot_is_taken(self) -> None:
        self.assert_succeeds(self.machine.run("--confirm-backup"))

        self.assertNotIn("etcd-snapshot", self.machine.calls())


class VersionStepTests(UpdateTestCase):
    """MEHO 0.36.0 with RKE2 v1.37.1+rke2r1 is installed."""

    def test_the_same_and_the_next_rke2_minor_version_pass(self) -> None:
        cases = {
            "v1.37.1+rke2r1": "RKE2 v1.37.1+rke2r1 of this download has the same minor version.",
            "v1.37.4+rke2r2": "RKE2 v1.37.4+rke2r2 of this download has the same minor version.",
            "v1.38.0+rke2r1": "RKE2 v1.38.0+rke2r1 of this download is the next minor version.",
        }
        record = self.machine.host_path(INSTALL_RECORD).read_text()
        for rke2, line in cases.items():
            with self.subTest(rke2=rke2):
                self.machine.set_download_version("0.37.0", rke2)

                result = self.machine.run("--confirm-backup")

                self.assert_succeeds(result)
                self.assertIn(f"[ok]   RKE2 v1.37.1+rke2r1 is installed. {line}", result.stdout)
                self.machine.remove_install_record()
                self.machine.host_path(INSTALL_RECORD).write_text(record)

    def test_a_skipped_rke2_minor_version_is_refused_and_names_the_one_first(self) -> None:
        self.machine.set_download_version("0.37.0", "v1.39.2+rke2r1")
        before = self.snapshot()

        line = self.assert_stopped_before_any_change(
            self.machine.run("--confirm-backup"),
            before,
            "RKE2 v1.37.1+rke2r1 is installed, and this download has RKE2 v1.39.2+rke2r1. RKE2"
            " moves only one minor version at a time, so RKE2 v1.38 must come first.",
        )

        self.assertIn("Fix: first install the MEHO release with RKE2 v1.38", line)

    def test_an_older_rke2_minor_version_is_refused(self) -> None:
        self.machine.set_download_version("0.37.0", "v1.36.9+rke2r1")
        before = self.snapshot()

        line = self.assert_stopped_before_any_change(
            self.machine.run("--confirm-backup"),
            before,
            "this download has the older RKE2 v1.36.9+rke2r1.",
        )

        self.assertIn("Fix: use the download of a MEHO release with RKE2 v1.37 or newer.", line)

    def test_an_older_meho_is_refused_and_names_the_version_to_use(self) -> None:
        for older in ("0.35.2", "0.36.0-rc.1"):
            with self.subTest(older=older):
                self.machine.set_download_version(older)
                before = self.snapshot()

                result = self.machine.run("--confirm-backup")

                line = self.assert_stopped_before_any_change(
                    result,
                    before,
                    f"MEHO 0.36.0 is installed, and this download has the older MEHO {older}. The"
                    " database changes of MEHO 0.36.0 cannot be undone.",
                )
                self.assertIn("Fix: use the download of MEHO 0.36.0 or newer.", line)
                self.assertIn(f"This download has the older MEHO {older}.", result.stdout)

    def test_the_same_meho_version_again_passes(self) -> None:
        self.machine.set_download_version("0.36.0")

        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertIn("This run installs the same version again.", result.stdout)

    def test_the_version_step_runs_before_the_backup_question(self) -> None:
        self.machine.set_download_version("0.35.2")

        result = self.machine.run(terminal_input="yes\n")

        self.assert_fails_once(result, "the older MEHO 0.35.2")
        self.assertNotIn("Did you take a VM snapshot", result.stdout)


class VersionOrderTests(UpdateTestCase):
    def test_the_order_follows_semantic_versioning(self) -> None:
        older_first = [
            "0.1.20261022-4217e54",
            "0.1.20261023-0a1b2c3",
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "1.0.1",
            "1.2.0",
            "1.10.0",
            "2.0.0",
        ]
        pairs = [(a, b) for a in older_first for b in older_first]
        snippet = "".join(f'echo "$(version_order {a} {b})"\n' for a, b in pairs)

        result = run_update_lib(self.machine, snippet)

        self.assertEqual(0, result.returncode, result.stderr)

        def order(a: str, b: str) -> str:
            rank_a, rank_b = older_first.index(a), older_first.index(b)
            return str((rank_a > rank_b) - (rank_a < rank_b))

        self.assertEqual([order(a, b) for a, b in pairs], result.stdout.split())


if __name__ == "__main__":
    unittest.main()
