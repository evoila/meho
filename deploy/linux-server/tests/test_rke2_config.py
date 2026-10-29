#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of /etc/rancher/rke2/config.yaml, which step 2 of install.sh writes.

They cover the default settings, custom pod and service ranges, and the join
options. The join token is read from its file and is never printed, logged
or passed as an argument. RKE2's CIS profile is in every case; test_cis.py
covers its pod security file and its host steps.
"""

from __future__ import annotations

import stat
import textwrap
import unittest
from pathlib import Path

from harness import InstallTestCase

CONFIG = "/etc/rancher/rke2/config.yaml"
JOIN_TOKEN = "K10test-join-token-3f9c2a::server:7d1e0b"

DEFAULT_CONFIG = textwrap.dedent("""\
    # Written by MEHO install.sh. A later run of install.sh writes it again.
    write-kubeconfig-mode: "0600"
    tls-san:
      - "meho.example.com"
    ingress-controller: traefik
    cluster-cidr: "10.42.0.0/16"
    service-cidr: "10.43.0.0/16"
    profile: cis
    pod-security-admission-config-file: "/etc/rancher/rke2/meho-pss.yaml"
    """)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class RKE2ConfigTests(InstallTestCase):
    def config(self) -> str:
        return self.machine.host_path(CONFIG).read_text()

    def write_token_file(self) -> Path:
        token_file = self.machine.home / "join-token"
        token_file.write_text(JOIN_TOKEN + "\n")
        return token_file

    def test_default_settings(self) -> None:
        self.assert_succeeds(self.machine.run())

        self.assertEqual(DEFAULT_CONFIG, self.config())
        self.assertEqual(0o600, mode(self.machine.host_path(CONFIG)))

    def test_custom_ranges(self) -> None:
        result = self.machine.run("--cluster-cidr", "172.30.0.0/16", "--service-cidr=172.31.0.0/20")

        self.assert_succeeds(result)
        expected = DEFAULT_CONFIG.replace("10.42.0.0/16", "172.30.0.0/16").replace(
            "10.43.0.0/16", "172.31.0.0/20"
        )
        self.assertEqual(expected, self.config())

    def test_host_name_option_becomes_the_certificate_name(self) -> None:
        self.machine.answer("resolvectl_out", "meho-01.lab.example.org: 10.0.0.5\n")

        result = self.machine.run("--hostname", "meho-01.lab.example.org")

        self.assert_succeeds(result)
        self.assertIn('tls-san:\n  - "meho-01.lab.example.org"\n', self.config())

    def test_join_options_point_rke2_to_the_server_and_the_token_file(self) -> None:
        token_file = self.write_token_file()

        result = self.machine.run(
            "--join-address", "10.0.0.9", "--join-token-file", str(token_file)
        )

        self.assert_succeeds(result)
        self.assertEqual(
            DEFAULT_CONFIG
            + 'server: "https://10.0.0.9:9345"\ntoken-file: "/etc/meho/join-token"\n',
            self.config(),
        )
        copied = self.machine.host_path("/etc/meho/join-token")
        self.assertEqual(JOIN_TOKEN + "\n", copied.read_text())
        self.assertEqual(0o600, mode(copied))

    def test_the_join_token_appears_in_no_output(self) -> None:
        token_file = self.write_token_file()

        result = self.machine.run(
            "--join-address", "rke2-1.example.com", f"--join-token-file={token_file}"
        )

        self.assert_succeeds(result)
        for name, text in (
            ("standard output", result.stdout),
            ("standard error", result.stderr),
            ("arguments of every command the script ran", self.machine.calls()),
            ("RKE2's settings", self.config()),
        ):
            with self.subTest(place=name):
                self.assertNotIn(JOIN_TOKEN, text)

    def test_join_address_without_token_file_is_wrong_usage(self) -> None:
        result = self.machine.run("--join-address", "10.0.0.9")

        self.assert_usage_error(result, "--join-address and --join-token-file go together")
        self.assertEqual([], self.written_files())

    def test_token_file_without_join_address_is_wrong_usage(self) -> None:
        result = self.machine.run("--join-token-file", str(self.write_token_file()))

        self.assert_usage_error(result, "--join-address and --join-token-file go together")

    def test_missing_or_empty_token_file_is_wrong_usage(self) -> None:
        empty = self.machine.home / "empty-token"
        empty.write_text("")
        for token_file in (self.machine.home / "no-such-file", empty):
            with self.subTest(token_file=token_file.name):
                result = self.machine.run(
                    "--join-address", "10.0.0.9", "--join-token-file", str(token_file)
                )

                self.assert_usage_error(result, "is not a readable file with a token in it")


if __name__ == "__main__":
    unittest.main()
