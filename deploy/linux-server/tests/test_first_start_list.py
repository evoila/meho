#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of the end of install.sh and of its update: the CA of the console
certificate goes to a file, and the result names where to read the
first-start list after the first login. MEHO shows the list only after a
login, so the install does not run the meho command. Also the fix texts of
the DNS check on an update, which keeps the install's --public-address."""

from __future__ import annotations

import json
import shutil
import stat
import unittest

from harness import CONSOLE_CA, InstallTestCase
from update_harness import INSTALL_RECORD, UpdateTestCase

CA_FILE = "/var/lib/meho/console-ca.crt"
CA_TEMPLATE_CALL = 'get secret meho-core-tls -o go-template={{index .data "ca.crt" | base64decode}}'
FIRST_START_LINE = (
    "  First-start list:      after the first login, open"
    " https://{host}/ui/first-start, or run \"meho login https://{host}\" and then"
    " \"meho status --first-start\"\n"
)
NEW_CA = "-----BEGIN CERTIFICATE-----\nMIIBfakeNewConsoleCA\n-----END CERTIFICATE-----\n"


class ConsoleCaTests(InstallTestCase):
    def test_the_console_ca_goes_to_a_file_that_is_not_secret(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        ca_file = self.machine.host_path(CA_FILE)
        self.assertEqual(CONSOLE_CA, ca_file.read_text())
        self.assertEqual(0o644, stat.S_IMODE(ca_file.stat().st_mode))
        self.assertIn(f"[ok]   wrote the CA of the console certificate to {CA_FILE}", result.stdout)
        self.assertIn(f"Console CA:            {CA_FILE} (not secret;", result.stdout)

    def test_only_the_ca_of_the_tls_secret_is_read_never_its_key(self) -> None:
        self.assert_succeeds(self.machine.run())

        calls = self.machine.calls()
        self.assertIn(CA_TEMPLATE_CALL, calls)
        self.assertNotIn("tls.key", calls)

    def assert_no_ca_file(self, ca_output: str, rc: str) -> None:
        """ca_output is what kubectl prints for the CA of the TLS Secret."""
        self.machine.answer("kube_secret_tls", ca_output)
        self.machine.answer("kube_secret_tls_rc", rc)

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertFalse(self.machine.host_path(CA_FILE).exists())
        self.assertIn("[WARN] The Secret meho-core-tls holds no CA certificate", result.stderr)
        self.assertNotIn("Console CA:", result.stdout)

    def test_a_tls_secret_that_cannot_be_read_writes_no_ca_file(self) -> None:
        self.assert_no_ca_file("", "1")

    def test_a_tls_secret_without_a_ca_certificate_writes_no_ca_file(self) -> None:
        self.assert_no_ca_file("<no value>", "0")


def meho_calls(calls: str) -> list[str]:
    """The calls of the meho command in calls.log."""
    return [line for line in calls.splitlines() if line.split(" ", 1)[0] == "meho"]


class FirstStartListTests(InstallTestCase):
    def test_the_result_ends_with_where_to_read_the_list_after_the_first_login(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertTrue(
            result.stdout.endswith(FIRST_START_LINE.format(host="meho.example.com")),
            result.stdout,
        )

    def test_the_install_does_not_run_the_meho_command(self) -> None:
        self.assert_succeeds(self.machine.run())

        self.assertEqual([], meho_calls(self.machine.calls()))


class FromDiskTests(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.machine.download / "rke2")
        shutil.rmtree(self.machine.download / "images")
        self.machine.install_rke2_files()

    def test_from_disk_writes_the_ca_and_names_the_list_under_the_new_name(self) -> None:
        result = self.machine.run("--from-disk", "--hostname", "new-name.example.org")

        self.assert_succeeds(result)
        self.assertEqual(CONSOLE_CA, self.machine.host_path(CA_FILE).read_text())
        self.assertIn(FIRST_START_LINE.format(host="new-name.example.org"), result.stdout)
        self.assertEqual([], meho_calls(self.machine.calls()))


class UpdateFirstStartTests(UpdateTestCase):
    def test_the_update_writes_the_ca_again_and_names_where_to_read_the_list(self) -> None:
        self.machine.answer("kube_secret_tls", NEW_CA)

        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertEqual(NEW_CA, self.machine.host_path(CA_FILE).read_text())
        self.assertIn(f"Console CA:            {CA_FILE} (not secret;", result.stdout)
        self.assertTrue(
            result.stdout.endswith(FIRST_START_LINE.format(host="meho.example.com")),
            result.stdout,
        )
        self.assertEqual([], meho_calls(self.machine.calls()))

    def test_an_update_whose_ca_cannot_be_read_removes_the_old_file(self) -> None:
        self.assertTrue(self.machine.host_path(CA_FILE).exists())
        self.machine.answer("kube_secret_tls_rc", "1")

        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertFalse(self.machine.host_path(CA_FILE).exists())
        self.assertNotIn("Console CA:", result.stdout)


class UpdateDnsTests(UpdateTestCase):
    def test_dns_to_another_machine_names_only_the_dns_record(self) -> None:
        self.machine.answer("resolvectl_out", "meho.example.com: 192.0.2.50\n")

        line = self.assert_fails_once(
            self.machine.run("--confirm-backup"),
            "resolves to 192.0.2.50, which is not an address of this machine.",
            "correct the DNS record so that it points to an address of this machine.",
            "An update keeps the answers of the install and takes no --public-address.",
        )
        self.assertNotIn("pass the translated address", line)

    def test_dns_away_from_the_installs_public_address_names_that_address(self) -> None:
        record_file = self.machine.host_path(INSTALL_RECORD)
        record = json.loads(record_file.read_text())
        record["options"]["public_address"] = "192.0.2.60"
        record_file.write_text(json.dumps(record, indent=2) + "\n")

        line = self.assert_fails_once(
            self.machine.run("--confirm-backup"),
            "not to the address the install was given with --public-address (192.0.2.60).",
            "correct the DNS record so that it points to 192.0.2.60.",
        )
        self.assertNotIn("pass the address it points to", line)


if __name__ == "__main__":
    unittest.main()
