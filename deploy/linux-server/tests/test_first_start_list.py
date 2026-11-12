#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of the end of install.sh and of its update: the CA of the console
certificate goes to a file, and the meho command reads the first-start list
through the console address with that CA. A red line, or a list that cannot
be read, does not fail the install. Also the fix texts of the DNS check on an
update, which keeps the install's --public-address."""

from __future__ import annotations

import json
import shutil
import stat
import unittest

from harness import CONSOLE_CA, FIRST_START_GREEN, InstallTestCase
from update_harness import INSTALL_RECORD, UpdateTestCase

CA_FILE = "/var/lib/meho/console-ca.crt"
FIRST_START_CALL = "meho status --first-start --backplane https://meho.example.com"
CA_TEMPLATE_CALL = 'get secret meho-core-tls -o go-template={{index .data "ca.crt" | base64decode}}'
CONSOLE_PAGE = "https://meho.example.com/ui/first-start"
RED_LIST = (
    "GREEN   dns           meho.example.com points at this MEHO.\n"
    "RED     certificate   The certificate of meho.example.com is not trusted.\n"
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

    def assert_no_ca_file(self, secret: str, rc: str) -> None:
        self.machine.answer("kube_secret_tls", secret)
        self.machine.answer("kube_secret_tls_rc", rc)

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertFalse(self.machine.host_path(CA_FILE).exists())
        self.assertIn("[WARN] The Secret meho-core-tls holds no CA certificate", result.stderr)
        self.assertNotIn("Console CA:", result.stdout)
        self.assertIn("meho-env SSL_CERT_FILE=\n", self.machine.calls())

    def test_a_tls_secret_that_cannot_be_read_writes_no_ca_file(self) -> None:
        self.assert_no_ca_file("", "1")

    def test_a_tls_secret_without_a_ca_certificate_writes_no_ca_file(self) -> None:
        self.assert_no_ca_file("<no value>", "0")


class FirstStartListTests(InstallTestCase):
    def test_the_meho_command_reads_the_list_through_the_console_address_with_the_ca(
        self,
    ) -> None:
        self.assert_succeeds(self.machine.run())

        calls = self.machine.calls()
        self.assertIn(FIRST_START_CALL, calls)
        self.assertIn(f"meho-env SSL_CERT_FILE={self.machine.host_path(CA_FILE)}\n", calls)

    def test_the_list_is_read_last(self) -> None:
        self.assert_succeeds(self.machine.run())

        calls = self.machine.calls().splitlines()
        self.assertTrue(calls[-1].startswith("meho-env "), calls[-3:])
        self.assertEqual(FIRST_START_CALL, calls[-2])

    def test_a_green_list_is_shown_at_the_end_of_the_result(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn("[ok]   the first-start list is green", result.stdout)
        shown = "".join(f"    {line}\n" for line in FIRST_START_GREEN.splitlines())
        self.assertTrue(
            result.stdout.endswith("  First-start list:      green\n" + shown), result.stdout
        )

    def test_a_red_line_does_not_stop_the_install(self) -> None:
        self.machine.answer("meho_first_start_out", RED_LIST)
        self.machine.answer(
            "meho_first_start_err", "error: at least one first-start check is red\n"
        )
        self.machine.answer("meho_first_start_rc", "6")

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn("[WARN] The first-start list has a red line.", result.stderr)
        self.assertIn("A red line does not stop the install.", result.stderr)
        self.assertIn(
            "  First-start list:      a red line; each red line says what to fix\n", result.stdout
        )
        self.assertIn(
            "    RED     certificate   The certificate of meho.example.com", result.stdout
        )
        self.assertTrue(self.machine.host_path(INSTALL_RECORD).exists())

    def test_a_list_that_cannot_be_read_names_the_reason_and_the_console_page(self) -> None:
        reason = "call https://meho.example.com: x509: certificate signed by unknown authority"
        self.machine.answer("meho_first_start_out", "")
        self.machine.answer("meho_first_start_err", f"{reason}\nsecond line\n")
        self.machine.answer("meho_first_start_rc", "3")

        result = self.machine.run()

        self.assert_succeeds(result)
        warning = f"[WARN] The first-start list could not be read: {reason}."
        self.assertIn(warning, result.stderr)
        self.assertIn(f"After the first login, open {CONSOLE_PAGE}.", result.stderr)
        self.assertNotIn("second line", result.stderr)
        self.assertTrue(
            result.stdout.endswith(
                f"  First-start list:      not read; after the first login, open {CONSOLE_PAGE}\n"
            ),
            result.stdout,
        )

    def test_a_list_with_lines_but_another_exit_code_shows_no_lines(self) -> None:
        self.machine.answer("meho_first_start_out", FIRST_START_GREEN)
        self.machine.answer("meho_first_start_rc", "4")

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertNotIn("GREEN   dns", result.stdout)

    def test_a_meho_command_that_cannot_run_names_its_exit_code(self) -> None:
        self.machine.answer("meho_first_start_rc", "126")

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn(
            "[WARN] The first-start list could not be read: the meho command ended with exit"
            " code 126.",
            result.stderr,
        )


class FromDiskTests(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.machine.download / "rke2")
        shutil.rmtree(self.machine.download / "images")
        self.machine.install_rke2_files()

    def test_from_disk_writes_the_ca_and_reads_the_list_under_the_new_name(self) -> None:
        result = self.machine.run("--from-disk", "--hostname", "new-name.example.org")

        self.assert_succeeds(result)
        self.assertEqual(CONSOLE_CA, self.machine.host_path(CA_FILE).read_text())
        self.assertIn(
            "meho status --first-start --backplane https://new-name.example.org",
            self.machine.calls(),
        )
        self.assertIn("  First-start list:      green\n", result.stdout)


class UpdateFirstStartTests(UpdateTestCase):
    def test_the_update_writes_the_ca_again_and_shows_the_list(self) -> None:
        self.machine.answer("kube_secret_tls", NEW_CA)

        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertEqual(NEW_CA, self.machine.host_path(CA_FILE).read_text())
        self.assertIn(f"Console CA:            {CA_FILE} (not secret;", result.stdout)
        self.assertIn("  First-start list:      green\n", result.stdout)
        calls = self.machine.calls().splitlines()
        helm = next(i for i, line in enumerate(calls) if line.startswith("helm upgrade"))
        self.assertGreater(calls.index(FIRST_START_CALL), helm)

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
