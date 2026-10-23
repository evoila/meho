#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of steps 4 to 6 of install.sh: install the core with Helm, write the
two one-time files, install the meho command and write the install record.
Also of --from-disk, which runs only the steps that need the host name, and
of the chart's answer to the two values files."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path

from harness import (
    INSTALL_DATE,
    LINUX_SERVER,
    SECRET_VALUES,
    TENANT_ID,
    UNSEAL_KEY,
    InstallTestCase,
)

CHART_DIR = LINUX_SERVER.parent / "charts" / "meho"
PLACE_VALUES = "/etc/meho/values-place.yaml"
INSTALL_RECORD = "/etc/meho/install.json"
FIRST_LOGIN_FILE = "/var/lib/meho/first-login-passwords"
UNSEAL_KEY_FILE = "/var/lib/meho/unseal-key"
CLI_PATH = "/usr/local/bin/meho"
RKE2_START = "systemctl enable --now --no-block rke2-server.service"
HINT = "run 'meho-host support-bundle'"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def place_values(
    hostname: str = "meho.example.com",
    tenant_id: str = TENANT_ID,
    admin: str = "admin",
    approver: str = "approver",
    pod_range: str = "10.42.0.0/16",
) -> str:
    return textwrap.dedent(f"""\
        # Written by MEHO install.sh: the answers of this place. A later run of
        # install.sh passes this file to Helm again. It holds no secret.
        ingress:
          host: "{hostname}"
        coreKeycloak:
          hostname: "{hostname}"
          firstTenantId: "{tenant_id}"
          firstLogin:
            adminUsername: "{admin}"
            approverUsername: "{approver}"
        # The pod range, so MEHO may reach the built-in PostgreSQL and OpenBao.
        networkPolicy:
          postgresCIDR: "{pod_range}"
          vaultCIDR: "{pod_range}"
        """)


class CoreInstallTests(InstallTestCase):
    def helm_call(self) -> str:
        download, root = self.machine.download, self.machine.root
        return (
            f"helm upgrade --install meho {download}/chart/meho-0.36.0.tgz"
            f" --kubeconfig {root}/etc/rancher/rke2/rke2.yaml"
            " --namespace meho --create-namespace --wait --wait-for-jobs --timeout 15m"
            " --set image.tag=v0.36.0"
            f" -f {download}/chart/values-single-node.yaml -f {root}{PLACE_VALUES}"
        )

    def test_helm_installs_the_chart_after_rke2_is_ready(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        calls = self.machine.calls().splitlines()
        self.assertIn(self.helm_call(), calls)
        self.assertLess(calls.index(RKE2_START), calls.index(self.helm_call()))
        self.assertIn(
            "[ok]   the MEHO core runs: Helm release meho in namespace meho", result.stdout
        )

    def test_helm_output_is_not_shown(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertNotIn("kubectl", self.output(result))
        self.assertNotIn("NOTES", self.output(result))

    def test_a_failed_helm_install_shows_the_error_and_the_hint(self) -> None:
        self.machine.answer("helm_rc", "1")
        self.machine.answer("helm_stderr", "Error: INSTALLATION FAILED: timed out waiting\n")

        result = self.machine.run()

        self.assert_fails_once(result, "Helm could not install the MEHO core.", HINT)
        self.assertIn("Error: INSTALLATION FAILED: timed out waiting", result.stderr)
        for path in (FIRST_LOGIN_FILE, UNSEAL_KEY_FILE, CLI_PATH, INSTALL_RECORD):
            with self.subTest(path=path):
                self.assertFalse(self.machine.host_path(path).exists())

    def test_place_values_hold_the_defaults_and_a_new_tenant_id(self) -> None:
        self.assert_succeeds(self.machine.run())

        self.assertEqual(place_values(), self.machine.host_path(PLACE_VALUES).read_text())

    def test_place_values_hold_the_options(self) -> None:
        self.machine.answer("resolvectl_out", "meho-01.example.org: 10.0.0.5\n")

        result = self.machine.run(
            "--hostname=meho-01.example.org",
            "--first-tenant-id",
            "9B4D2A7E-3F1C-4E2A-8C1F-5D6E7F8A9B0C",
            "--admin-user=first.admin",
            "--approver-user",
            "second@example.org",
            "--cluster-cidr",
            "172.30.0.0/16",
        )

        self.assert_succeeds(result)
        expected = place_values(
            hostname="meho-01.example.org",
            tenant_id="9b4d2a7e-3f1c-4e2a-8c1f-5d6e7f8a9b0c",
            admin="first.admin",
            approver="second@example.org",
            pod_range="172.30.0.0/16",
        )
        self.assertEqual(expected, self.machine.host_path(PLACE_VALUES).read_text())

    def test_bad_place_options_are_wrong_usage(self) -> None:
        cases = {
            "--first-tenant-id must be a UUID": ["--first-tenant-id", "tenant-one"],
            "--admin-user must be a user name": ["--admin-user", "first admin"],
            "--approver-user must be a user name": ["--approver-user", 'x"y'],
            "must be two different names": ["--admin-user", "same", "--approver-user", "same"],
        }
        for message, args in cases.items():
            with self.subTest(args=args):
                self.assert_usage_error(self.machine.run(*args), message)
                self.assertEqual([], self.written_files())

    def test_the_one_time_files_are_for_root_only(self) -> None:
        self.assert_succeeds(self.machine.run())

        first_login = self.machine.host_path(FIRST_LOGIN_FILE)
        unseal_key = self.machine.host_path(UNSEAL_KEY_FILE)
        self.assertEqual(0o600, mode(first_login))
        self.assertEqual(0o600, mode(unseal_key))
        self.assertIn("admin_password=Fake-Admin-Password-7f3a\n", first_login.read_text())
        self.assertIn("approver_password=Fake-Approver-Password-9c1d\n", first_login.read_text())
        self.assertEqual(f"{UNSEAL_KEY}\n", unseal_key.read_text())

    def test_no_secret_in_the_output_or_the_records(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        places = {
            "standard output": result.stdout,
            "standard error": result.stderr,
            "arguments of every command the script ran": self.machine.calls(),
            "the place values": self.machine.host_path(PLACE_VALUES).read_text(),
            "the install record": self.machine.host_path(INSTALL_RECORD).read_text(),
        }
        for place, text in places.items():
            for value in SECRET_VALUES:
                with self.subTest(place=place, value=value):
                    self.assertNotIn(value, text)

    def test_a_missing_unseal_secret_warns_and_writes_no_key_file(self) -> None:
        self.machine.answer("kube_secret_unseal", "")
        self.machine.answer("kube_secret_unseal_rc", "1")

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn("[WARN] The Secret meho-core-vault-unseal does not exist", result.stderr)
        self.assertFalse(self.machine.host_path(UNSEAL_KEY_FILE).exists())
        self.assertNotIn("Unseal key:", result.stdout)
        self.assertEqual(0o600, mode(self.machine.host_path(FIRST_LOGIN_FILE)))

    def test_a_missing_first_login_secret_fails(self) -> None:
        self.machine.answer("kube_secret_first_login", "")
        self.machine.answer("kube_secret_first_login_rc", "1")

        result = self.machine.run()

        self.assert_fails_once(result, "meho-core-first-login", HINT)
        self.assertFalse(self.machine.host_path(FIRST_LOGIN_FILE).exists())

    def test_the_meho_command_is_installed(self) -> None:
        self.assert_succeeds(self.machine.run())

        cli = self.machine.host_path(CLI_PATH)
        self.assertEqual("fake meho CLI\n", cli.read_text())
        self.assertEqual(0o755, mode(cli))

    def test_the_install_record(self) -> None:
        self.assert_succeeds(self.machine.run("--public-address", "10.0.0.5"))

        record = json.loads(self.machine.host_path(INSTALL_RECORD).read_text())
        self.assertEqual(
            {
                "meho_version": "0.36.0",
                "chart_version": "0.36.0",
                "rke2_version": "v1.37.1+rke2r1",
                "installed_at": INSTALL_DATE,
                "options": {
                    "hostname": "meho.example.com",
                    "public_address": "10.0.0.5",
                    "first_tenant_id": TENANT_ID,
                    "admin_user": "admin",
                    "approver_user": "approver",
                    "cluster_cidr": "10.42.0.0/16",
                    "service_cidr": "10.43.0.0/16",
                    "from_disk": False,
                },
            },
            record,
        )

    def test_the_result_names_the_console_and_the_two_files(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        for line in (
            "install.sh: MEHO 0.36.0 is installed on meho.example.com.",
            "Console:               https://meho.example.com/ui/",
            f"First-login passwords: {FIRST_LOGIN_FILE} (root only;",
            f"Unseal key:            {UNSEAL_KEY_FILE} (root only;",
            "First-start list:      not in this version yet.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, result.stdout)

    def test_a_download_without_the_chart_fails_in_step_1(self) -> None:
        (self.machine.download / "chart" / "meho-0.36.0.tgz").unlink()

        self.assert_fails_once(self.machine.run(), "Missing: chart/meho-0.36.0.tgz.")
        self.assertEqual([], self.written_files())


class FromDiskTests(InstallTestCase):
    """The download folder on the disk of a cleaned machine: the RKE2 files
    and the image archive are gone, the chart, bin/ and VERSION are there."""

    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.machine.download / "rke2")
        shutil.rmtree(self.machine.download / "images")

    def test_from_disk_runs_only_the_steps_that_need_the_host_name(self) -> None:
        result = self.machine.run("--from-disk", "--hostname", "new-name.example.org")

        self.assert_succeeds(result)
        calls = self.machine.calls()
        commands = {line.split(" ", 1)[0] for line in calls.splitlines()}
        for command in ("getconf", "df", "ss", "timedatectl", "resolvectl", "rke2-installer"):
            with self.subTest(command=command):
                self.assertNotIn(command, commands)
        self.assertIn(RKE2_START, calls)
        self.assertIn("helm upgrade --install meho", calls)
        config = self.machine.host_path("/etc/rancher/rke2/config.yaml").read_text()
        self.assertIn('tls-san:\n  - "new-name.example.org"\n', config)
        self.assertFalse(self.machine.host_path("/var/lib/rancher/rke2/agent/images").exists())
        self.assertEqual(
            place_values(hostname="new-name.example.org"),
            self.machine.host_path(PLACE_VALUES).read_text(),
        )
        self.assertEqual(0o600, mode(self.machine.host_path(FIRST_LOGIN_FILE)))
        record = json.loads(self.machine.host_path(INSTALL_RECORD).read_text())
        self.assertEqual("new-name.example.org", record["options"]["hostname"])
        self.assertTrue(record["options"]["from_disk"])

    def test_from_disk_needs_the_core_files(self) -> None:
        (self.machine.download / "bin" / "helm").unlink()

        self.assert_fails_once(self.machine.run("--from-disk"), "Missing: bin/helm.")
        self.assertEqual([], self.written_files())

    def test_from_disk_with_public_address_is_wrong_usage(self) -> None:
        result = self.machine.run("--from-disk", "--public-address", "10.0.0.5")

        self.assert_usage_error(result, "--from-disk runs no DNS check")
        self.assertEqual("", self.machine.calls())


@unittest.skipUnless(
    shutil.which("helm") or os.environ.get("MEHO_REQUIRE_HELM") == "1",
    "helm is not on PATH; set MEHO_REQUIRE_HELM=1 to fail instead",
)
class ChartRenderTests(InstallTestCase):
    def test_the_chart_accepts_the_two_values_files(self) -> None:
        self.assert_succeeds(self.machine.run())

        result = subprocess.run(
            [
                "helm",
                "template",
                "meho",
                str(CHART_DIR),
                "--namespace",
                "meho",
                "--set",
                "image.tag=test",
                "-f",
                str(LINUX_SERVER / "values-single-node.yaml"),
                "-f",
                str(self.machine.host_path(PLACE_VALUES)),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("ingressClassName: traefik", result.stdout)
        self.assertIn("secretName: meho-core-tls", result.stdout)
        self.assertIn("meho-core-postgres-meho", result.stdout)
        self.assertIn('VAULT_ADDR: "http://meho-vault:8200"', result.stdout)


if __name__ == "__main__":
    unittest.main()
