#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of the options of install.sh: --help lists every option, and wrong
usage exits 2 before anything is checked or written."""

from __future__ import annotations

import re
import unittest

from harness import INSTALL_SH, InstallTestCase

CASE_LABEL = re.compile(r"^\s+(?:-h \| )?(--[a-z][a-z-]*)\)", re.MULTILINE)
HELP_OPTION = re.compile(r"^\s+(?:-h, )?(--[a-z][a-z-]*)", re.MULTILINE)


def options_the_script_accepts() -> set[str]:
    return set(CASE_LABEL.findall(INSTALL_SH.read_text()))


class HelpTests(InstallTestCase):
    def test_help_lists_every_option(self) -> None:
        accepted = options_the_script_accepts()

        result = self.machine.run("--help")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--hostname", accepted, "the option parser was not found in install.sh")
        self.assertEqual(accepted, set(HELP_OPTION.findall(result.stdout)))
        self.assertIn("-h, --help", result.stdout)

    def test_help_works_without_root_and_changes_nothing(self) -> None:
        self.machine.answer("uid", "1000")

        result = self.machine.run("-h")

        self.assertEqual(0, result.returncode)
        self.assertIn("Options:", result.stdout)
        self.assertEqual([], self.written_files())


class WrongUsageTests(InstallTestCase):
    def tearDown(self) -> None:
        self.assertEqual("", self.machine.calls(), "wrong usage must stop before any check")

    def test_a_start_with_sh_instead_of_bash_exits_2(self) -> None:
        self.assert_usage_error(self.machine.run("--help", shell="sh"), "run it with bash")

    def test_unknown_option_exits_2(self) -> None:
        self.assert_usage_error(
            self.machine.run("--no-such-option", "x"), "unknown option: --no-such-option"
        )

    def test_argument_that_is_not_an_option_exits_2(self) -> None:
        self.assert_usage_error(
            self.machine.run("meho.example.com"), "unknown argument: meho.example.com"
        )

    def test_option_without_value_exits_2(self) -> None:
        for args in (
            ["--hostname"],
            ["--hostname", "--cluster-cidr", "10.42.0.0/16"],
            ["--service-cidr="],
        ):
            with self.subTest(args=args):
                self.assert_usage_error(self.machine.run(*args), "needs a value.")

    def test_bad_values_exit_2(self) -> None:
        cases = {
            "--cluster-cidr must be an IPv4 range": ["--cluster-cidr", "10.42.0.0"],
            "--service-cidr must be an IPv4 range": ["--service-cidr", "300.1.0.0/16"],
            "--public-address must be an IPv4 address": ["--public-address", "meho.example.com"],
            "--join-address must be a host name or an IPv4 address": [
                "--join-address",
                "https://10.0.0.9",
                "--join-token-file",
                __file__,
            ],
        }
        for message, args in cases.items():
            with self.subTest(args=args):
                self.assert_usage_error(self.machine.run(*args), message)


if __name__ == "__main__":
    unittest.main()
