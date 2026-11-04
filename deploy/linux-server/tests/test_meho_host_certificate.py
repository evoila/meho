#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of meho-host certificate (#4738): the customer's certificate and key
go into the console's TLS Secret only when every check passes, the key is
never on a command line or in the output, and the certificate line of the
check is printed at the end."""

from __future__ import annotations

import base64
import subprocess
import unittest
from pathlib import Path

from cluster_harness import CORE_LABELS, SUPPORT_HINT, TLS_SECRET, ClusterTestCase
from host_harness import certificate_with_key

CHAIN_HINT = (
    "put the server certificate first into one file, then every intermediate CA certificate, "
    "and your CA certificate last. Then run: "
    "sudo meho-host certificate --cert <chain.pem> --key <key.pem>"
)
NAME_FIX = f"Fix: give a certificate for meho.example.com that is valid now, and {CHAIN_HINT}"
GREEN_LINE = (
    "[ok]   console-certificate: valid for meho.example.com until 2027-10-23 (365 days left)"
)
INSTALLED = (
    "[ok]   installed the certificate for meho.example.com in the Secret meho-core-tls: the "
    "console, the API and the login show it within a minute\n"
    "[note] browsers and the meho CLI trust it when the computer trusts your CA.\n"
)


def customer_certificate(host: str = "meho.example.com", **dates: int) -> tuple[str, str, str]:
    """(server certificate and intermediate, root CA, key) of a certificate
    that a customer CA with an intermediate CA signed."""
    return certificate_with_key(host, ca="Customer Root CA", intermediate_end=3650, **dates)


def pkcs8(key: str) -> str:
    """The key as openssl pkey prints it, the form meho-host writes."""
    return subprocess.run(
        ["openssl", "pkey"], input=key, capture_output=True, text=True, check=True, timeout=30
    ).stdout


class CertificateTestCase(ClusterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.before = self.host.secret(TLS_SECRET)
        self.chain, self.root, self.key = customer_certificate()

    def files(self, chain: str, key: str) -> tuple[str, str]:
        """Write the two files the admin gives, and return their paths."""
        folder = self.host.root.parent
        chain_file, key_file = folder / "chain.pem", folder / "key.pem"
        chain_file.write_text(chain)
        key_file.write_text(key)
        return str(chain_file), str(key_file)

    def install(self, chain: str, key: str) -> subprocess.CompletedProcess[str]:
        chain_file, key_file = self.files(chain, key)
        return self.host.run("certificate", "--cert", chain_file, "--key", key_file)

    def assert_refused(self, result: subprocess.CompletedProcess[str], line: str) -> None:
        """One [FAIL] line, nothing on standard output, and the Secret unchanged."""
        self.assert_one_fail_line(result, line)
        self.assertEqual("", result.stdout)
        self.assertEqual(self.before, self.host.secret(TLS_SECRET))
        self.assertEqual([], self.kubectl_calls(" patch "))


class CertificateTests(CertificateTestCase):
    def test_a_valid_chain_for_the_host_name_is_installed_and_the_line_turns_green(self) -> None:
        result = self.install(self.chain + self.root, self.key)

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(INSTALLED + GREEN_LINE + "\n", result.stdout)
        self.assertEqual("", result.stderr)
        secret = self.host.secret(TLS_SECRET)
        assert secret is not None
        self.assertEqual(
            {"tls.crt": self.chain + self.root, "tls.key": pkcs8(self.key), "ca.crt": self.root},
            secret["data"],
        )
        self.assertEqual(CORE_LABELS, secret["labels"])
        self.assertEqual("kubernetes.io/tls", secret["type"])
        patch = self.kubectl_calls(" patch ")
        self.assertEqual(1, len(patch), patch)
        self.assertIn(
            "-n meho patch secret meho-core-tls --type=merge --patch-file=/dev/stdin", patch[0]
        )

    def test_a_chain_of_the_server_certificate_and_its_ca_is_installed(self) -> None:
        chain, ca, key = certificate_with_key(ca="Customer CA")

        result = self.install(chain + ca, key)

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertTrue(result.stdout.endswith(GREEN_LINE + "\n"), result.stdout)
        secret = self.host.secret(TLS_SECRET)
        assert secret is not None
        self.assertEqual(ca, secret["data"]["ca.crt"])

    def test_other_text_and_windows_line_ends_are_left_out_of_tls_crt(self) -> None:
        dirty = "subject=CN = meho.example.com\n" + self.chain + self.key + self.root
        dirty = dirty.replace("\n", "\r\n")

        result = self.install(dirty, self.key)

        self.assertEqual(0, result.returncode, self.output(result))
        secret = self.host.secret(TLS_SECRET)
        assert secret is not None
        self.assertEqual(self.chain + self.root, secret["data"]["tls.crt"])

    def test_a_certificate_with_20_days_left_is_installed_with_a_warning(self) -> None:
        chain, root, key = customer_certificate(end=20)

        result = self.install(chain + root, key)

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(
            "[WARN] console-certificate: only 20 days left: it expires on 2026-11-12. Fix: install "
            "a certificate for the host name of this install that is valid for more than 30 days: "
            "sudo meho-host certificate --cert <chain.pem> --key <key.pem>\n",
            result.stdout,
        )

    def test_the_key_is_never_on_a_command_line_or_in_the_output(self) -> None:
        result = self.install(self.chain + self.root, self.key)

        self.assertEqual(0, result.returncode, self.output(result))
        secret_lines = [line for line in self.key.splitlines() if "PRIVATE KEY" not in line]
        secret_lines.append(base64.b64encode(pkcs8(self.key).encode()).decode()[:40])
        for line in secret_lines:
            self.assertNotIn(line, self.host.calls())
            self.assertNotIn(line, self.output(result))
        self.assertIn("openssl pkey -in ", self.host.calls())
        self.assertIn("base64 -w 0", self.host.calls())


class RefusedCertificateTests(CertificateTestCase):
    def test_a_wrong_key_is_refused_with_one_line(self) -> None:
        other_key = customer_certificate("other.example.com")[2]
        chain_file, key_file = self.files(self.chain + self.root, other_key)

        result = self.host.run("certificate", "--cert", chain_file, "--key", key_file)

        self.assert_refused(
            result,
            f"[FAIL] the key in {key_file} does not belong to the server certificate, the first "
            f"one in {chain_file}. Nothing was changed. Fix: give the private key that was made "
            "together with the request for this certificate.",
        )

    def test_a_wrong_name_is_refused_with_one_line(self) -> None:
        chain, root, key = customer_certificate("other.example.com")
        chain_file, key_file = self.files(chain + root, key)

        result = self.host.run("certificate", "--cert", chain_file, "--key", key_file)

        self.assert_refused(
            result,
            f"[FAIL] the certificate in {chain_file} cannot be used: it does not name "
            "meho.example.com, the host name of this install. Nothing was changed. " + NAME_FIX,
        )

    def test_an_expired_certificate_is_refused_with_one_line(self) -> None:
        chain, root, key = customer_certificate(start=-400, end=-5)
        chain_file, key_file = self.files(chain + root, key)

        result = self.host.run("certificate", "--cert", chain_file, "--key", key_file)

        self.assert_refused(
            result,
            f"[FAIL] the certificate in {chain_file} cannot be used: it expired on 2026-10-18. "
            "Nothing was changed. " + NAME_FIX,
        )

    def test_a_broken_chain_or_key_file_is_refused_with_one_line(self) -> None:
        server = self.chain.split("-----END CERTIFICATE-----\n")[0] + "-----END CERTIFICATE-----\n"
        encrypted = subprocess.run(
            ["openssl", "pkey", "-aes256", "-passout", "pass:made-up-phrase"],
            input=self.key,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        no_pass_phrase = (
            "holds no private key that openssl can read without a pass phrase. Nothing was "
            "changed. Fix: give the private key without a pass phrase"
        )
        cases = {
            "not yet valid": (
                "".join(customer_certificate(start=3, end=400)[:2]),
                customer_certificate(start=3, end=400)[2],
                "cannot be used: it is valid only from 2026-10-26, so the clock of this machine "
                "or the certificate is wrong. Nothing was changed.",
            ),
            "the intermediate is missing": (
                server + self.root,
                self.key,
                "cannot be used: its chain to your CA, the last certificate of ",
            ),
            "only the server certificate": (
                server,
                self.key,
                "holds only one certificate, so the chain to your CA is missing.",
            ),
            "the server certificate twice": (
                server + server,
                self.key,
                "is the server certificate again, so the chain to your CA is missing.",
            ),
            "no certificate": (self.key, self.key, "holds no certificate. Nothing was changed."),
            "an encrypted key": (self.chain + self.root, encrypted, no_pass_phrase),
            "a certificate as the key": (self.chain + self.root, self.root, no_pass_phrase),
        }
        for case, (chain, key, fragment) in cases.items():
            with self.subTest(case=case):
                result = self.install(chain, key)

                self.assertEqual(1, result.returncode, self.output(result))
                self.assertEqual(1, len(result.stderr.splitlines()), result.stderr)
                self.assertTrue(result.stderr.startswith("[FAIL] "), result.stderr)
                self.assertIn(fragment, result.stderr)
                self.assertEqual(self.before, self.host.secret(TLS_SECRET))
                self.assertEqual([], self.kubectl_calls(" patch "))

    def test_a_file_that_cannot_be_read_is_refused(self) -> None:
        chain_file, _ = self.files(self.chain + self.root, self.key)
        missing = str(Path(chain_file).with_name("missing.pem"))

        result = self.host.run("certificate", "--cert", chain_file, "--key", missing)

        self.assert_refused(
            result,
            f"[FAIL] {missing} cannot be read. Nothing was changed. Fix: give the path of the "
            "file with the private key.",
        )

    def test_without_the_host_name_of_the_install_nothing_changes(self) -> None:
        self.host.host_path("/etc/meho/install.json").unlink()

        result = self.install(self.chain + self.root, self.key)

        self.assert_refused(
            result,
            "[FAIL] the host name is not in /etc/meho/install.json, so the certificate cannot be "
            f"checked. Nothing was changed. Fix: {SUPPORT_HINT}",
        )

    def test_a_failed_write_keeps_the_old_certificate_and_hides_kubectls_message(self) -> None:
        self.host.fail("patch")

        result = self.install(self.chain + self.root, self.key)

        self.assert_one_fail_line(
            result,
            "[FAIL] the Secret meho-core-tls could not be changed, so the console keeps its "
            f"certificate. Fix: check the node and pods lines: sudo meho-host check. Then "
            f"{SUPPORT_HINT}",
        )
        self.assertEqual(self.before, self.host.secret(TLS_SECRET))

    def test_not_root_changes_nothing(self) -> None:
        self.host.answer("uid", "1000")

        result = self.install(self.chain + self.root, self.key)

        self.assertEqual(1, result.returncode)
        self.assertIn("[FAIL] meho-host does not run as root.", result.stderr)
        self.assertEqual(self.before, self.host.secret(TLS_SECRET))

    def test_wrong_usage(self) -> None:
        chain_file, _ = self.files(self.chain + self.root, self.key)
        cases = {
            ("certificate",): "certificate needs both files: --cert <chain.pem> --key <key.pem>.",
            ("certificate", "--cert", chain_file): "certificate needs both files",
            ("certificate", "--cert", chain_file, "--key"): "certificate: --key needs a file.",
            ("certificate", "--force"): "unknown option of certificate: --force",
        }
        for args, fragment in cases.items():
            with self.subTest(args=args):
                self.assert_usage_error(self.host.run(*args), fragment)
        self.assertEqual(self.before, self.host.secret(TLS_SECRET))


if __name__ == "__main__":
    unittest.main()
