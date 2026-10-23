#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of meho-host: the check list, its JSON form and exit codes, the
support bundle and what is removed from it, and the install of meho-host by
install.sh."""

from __future__ import annotations

import json
import stat
import tarfile
import unittest

from harness import LINUX_SERVER, InstallTestCase
from host_harness import (
    HEALTHY_PODS,
    MEHO_PODS,
    TRAEFIK_POD,
    HostTestCase,
    certificate_table,
    df_answer,
)

SUPPORT_HINT = "run 'meho-host support-bundle' and send the file to support."
OK_LINES = [
    "[ok]   node: Ready",
    "[ok]   pods: all 7 pods in meho, kube-system, local-path-storage run",
    "[ok]   disk: /var/lib/rancher has 150 GB free (75%); /var/lib/meho has 150 GB free (75%)",
    "[ok]   certificates: all 2 RKE2 certificates are valid for at least 30 days",
]
BUNDLE_PATH = "/var/lib/meho/support/meho-support-meho.example.com-20261023T120000Z.tar.gz"
BUNDLE_FILES = {
    "check.json",
    *(f"pods/meho.{pod}.log" for pod in MEHO_PODS),
    f"pods/kube-system.{TRAEFIK_POD}.log",
    "objects/meho.txt",
    "objects/meho.secrets.txt",
    "objects/kube-system.txt",
    "objects/kube-system.secrets.txt",
    "objects/local-path-storage.txt",
    "objects/local-path-storage.secrets.txt",
    "events.txt",
    "node.txt",
    "rke2-server.journal.txt",
    "os/os-release.txt",
    "os/uname.txt",
    "os/free.txt",
    "os/df.txt",
    "os/ip-addr.txt",
    "os/ip-route.txt",
    "os/timedatectl.txt",
    "install.json",
}


class UsageTests(HostTestCase):
    def test_help_lists_both_commands_and_needs_no_root(self) -> None:
        self.host.answer("uid", "1000")

        result = self.host.run("--help")

        self.assertEqual(0, result.returncode, result.stderr)
        for text in ("check [--json]", "support-bundle", "Exit codes:"):
            self.assertIn(text, result.stdout)
        self.assertEqual("", self.host.calls())

    def test_no_command_is_a_usage_error(self) -> None:
        self.assert_usage_error(self.host.run(), "no command given")

    def test_an_unknown_command_is_a_usage_error(self) -> None:
        self.assert_usage_error(self.host.run("reboot"), "unknown command: reboot")

    def test_an_unknown_option_of_check_is_a_usage_error(self) -> None:
        self.assert_usage_error(self.host.run("check", "--all"), "unknown option of check: --all")

    def test_support_bundle_takes_no_option(self) -> None:
        self.assert_usage_error(
            self.host.run("support-bundle", "--json"), "support-bundle takes no option: --json"
        )

    def test_not_root_fails_with_the_sudo_command(self) -> None:
        self.host.answer("uid", "1000")

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn(
            "[FAIL] meho-host does not run as root. Fix: run it with sudo: sudo meho-host check",
            result.stderr,
        )


class CheckTests(HostTestCase):
    def test_a_healthy_machine_is_all_green(self) -> None:
        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(OK_LINES, result.stdout.splitlines())
        self.assertEqual("", result.stderr)

    def test_a_finished_job_pod_counts_as_fine(self) -> None:
        self.assertIn("Succeeded False", HEALTHY_PODS)

        self.assertEqual(0, self.host.run("check").returncode)

    def test_json_has_the_shape_meho_doctor_reads(self) -> None:
        result = self.host.run("check", "--json")

        self.assertEqual(0, result.returncode, self.output(result))
        checks = json.loads(result.stdout)["checks"]
        self.assertEqual(
            ["node", "pods", "disk", "certificates"], [check["name"] for check in checks]
        )
        for check in checks:
            with self.subTest(check=check["name"]):
                self.assertEqual({"name", "status", "detail", "hint"}, set(check))
                self.assertEqual("ok", check["status"])
                self.assertEqual("", check["hint"])
        self.assertEqual("Ready", checks[0]["detail"])

    def test_a_node_that_is_not_ready_is_red(self) -> None:
        self.host.answer("kube_nodes", "False")

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertEqual(
            "[FAIL] node: not Ready. Fix: see RKE2's log: journalctl -u rke2-server -e",
            result.stdout.splitlines()[0],
        )

    def test_rke2_that_does_not_answer_is_red_for_the_node_and_the_pods(self) -> None:
        self.host.answer("kubectl_rc", "1")

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        lines = result.stdout.splitlines()
        self.assertEqual(4, len(lines), result.stdout)
        self.assertEqual(
            "[FAIL] node: RKE2 does not answer. Fix: check that RKE2 runs: "
            "systemctl status rke2-server; see RKE2's log: journalctl -u rke2-server -e",
            lines[0],
        )
        self.assertEqual(f"[FAIL] pods: RKE2 does not answer. Fix: {SUPPORT_HINT}", lines[1])
        self.assertEqual(OK_LINES[2:], lines[2:])

    def test_a_pod_that_waits_or_is_not_ready_is_named(self) -> None:
        self.host.answer(
            "kube_pods",
            "meho meho-backplane-7d9f8-x2k4p Pending \n"
            "meho meho-keycloak-0 Running False\n"
            "kube-system rke2-canal-k9x2p Running True\n",
        )

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] pods: meho/meho-backplane-7d9f8-x2k4p is Pending, "
            f"meho/meho-keycloak-0 is not Ready. Fix: {SUPPORT_HINT}",
            result.stdout,
        )

    def test_pods_of_other_namespaces_are_not_checked(self) -> None:
        self.host.answer("kube_pods", HEALTHY_PODS + "default debug-shell Pending \n")

        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(OK_LINES[1], result.stdout)

    def test_no_pod_in_meho_means_the_core_is_not_installed(self) -> None:
        self.host.answer("kube_pods", "kube-system rke2-canal-k9x2p Running True\n")

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] pods: no pod in namespace meho, so the MEHO core is not installed. "
            f"Fix: run install.sh again. If it fails, {SUPPORT_HINT}",
            result.stdout,
        )

    def test_a_disk_below_20_percent_free_warns_and_the_exit_code_stays_0(self) -> None:
        self.host.answer("df_meho", df_answer(200, 30))

        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(
            "[WARN] disk: /var/lib/rancher has 150 GB free (75%); /var/lib/meho has 30 GB free "
            "(15%). Fix: free space on the disk or make it larger, before it falls below 10%.",
            result.stdout,
        )
        checks = json.loads(self.host.run("check", "--json").stdout)["checks"]
        self.assertEqual("warn", checks[2]["status"])
        self.assertTrue(checks[2]["hint"].startswith("free space"), checks[2])

    def test_a_disk_below_10_percent_free_is_red(self) -> None:
        self.host.answer("df_rancher", df_answer(200, 10))

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] disk: /var/lib/rancher has 10 GB free (5%); /var/lib/meho has 150 GB free "
            "(75%). Fix: free space on the disk or make it larger. Below 10% the built-in "
            "Kubernetes stops pods to protect the disk.",
            result.stdout,
        )

    def test_a_disk_that_cannot_be_read_is_red(self) -> None:
        self.host.answer("df_meho", "")

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] disk: /var/lib/meho cannot be read. "
            "Fix: check that the disk is mounted: df -h /var/lib/meho",
            result.stdout,
        )

    def test_a_certificate_with_20_days_left_warns(self) -> None:
        self.host.answer(
            "rke2_cert_table",
            certificate_table(
                ("client-admin.crt", "system:admin", 365, "OK"),
                ("serving-kube-apiserver.crt", "kube-apiserver", 20, "OK"),
            ),
        )

        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(
            "[WARN] certificates: less than 30 days left: serving-kube-apiserver.crt (20 days). "
            "Fix: restart RKE2, which renews a certificate with less than 120 days left: "
            "systemctl restart rke2-server.",
            result.stdout,
        )

    def test_a_certificate_with_30_days_left_is_fine(self) -> None:
        self.host.answer(
            "rke2_cert_table",
            certificate_table(
                ("client-admin.crt", "system:admin", 30, "OK"),
                ("server-ca.crt", "rke2-server-ca", 3650, "OK"),
            ),
        )

        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(OK_LINES[3], result.stdout)

    def test_an_expired_certificate_is_red(self) -> None:
        self.host.answer(
            "rke2_cert_table",
            certificate_table(
                ("client-kube-proxy.crt", "system:kube-proxy", -5, "EXPIRED"),
                ("server-ca.crt", "rke2-server-ca", 3650, "OK"),
            ),
        )

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] certificates: expired: client-kube-proxy.crt. Fix: restart RKE2, which "
            "renews them: systemctl restart rke2-server. Then run the check again.",
            result.stdout,
        )

    def test_a_subject_with_spaces_does_not_break_the_table(self) -> None:
        self.host.answer(
            "rke2_cert_table",
            certificate_table(
                ("server-ca.crt", "rke2 server ca", 3650, "OK"),
                ("client-admin.crt", "system:admin", 10, "OK"),
            ),
        )

        result = self.host.run("check")

        self.assertIn("less than 30 days left: client-admin.crt (10 days)", result.stdout)

    def test_a_failed_rke2_command_is_red(self) -> None:
        self.host.answer("rke2_rc", "1")

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] certificates: 'rke2 certificate check' did not run. Fix: check that RKE2 "
            f"is installed: ls -l /usr/local/bin/rke2. Then {SUPPORT_HINT}",
            result.stdout,
        )

    def test_an_empty_table_is_red(self) -> None:
        self.host.answer("rke2_cert_table", certificate_table())

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] certificates: 'rke2 certificate check' listed no certificate.", result.stdout
        )


class SupportBundleTests(HostTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.host.host_file("/etc/meho/install.json", '{"meho_version": "0.36.0"}\n')

    def bundle(self) -> dict[str, str]:
        """Run support-bundle, check the printed path, and return the archive's files."""
        result = self.host.run("support-bundle")
        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(BUNDLE_PATH + "\n", result.stdout)
        files = {}
        with tarfile.open(self.host.host_path(BUNDLE_PATH)) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    content = archive.extractfile(member)
                    assert content is not None
                    files[member.name.removeprefix("./")] = content.read().decode()
        return files

    def test_the_file_is_for_root_only_and_the_work_folder_is_gone(self) -> None:
        self.bundle()

        folder = self.host.host_path("/var/lib/meho/support")
        self.assertEqual(0o700, stat.S_IMODE(folder.stat().st_mode))
        archive = self.host.host_path(BUNDLE_PATH)
        self.assertEqual(0o600, stat.S_IMODE(archive.stat().st_mode))
        self.assertEqual([archive.name], [path.name for path in folder.iterdir()])

    def test_the_bundle_holds_every_part(self) -> None:
        files = self.bundle()

        self.assertEqual(BUNDLE_FILES, set(files))
        checks = json.loads(files["check.json"])["checks"]
        self.assertEqual(
            ["node", "pods", "disk", "certificates"], [check["name"] for check in checks]
        )
        self.assertTrue(files["os/os-release.txt"].startswith('PRETTY_NAME="Ubuntu 24.04'))
        self.assertIn("fake journal line", files["rke2-server.journal.txt"])
        self.assertIn("fake log line", files[f"pods/kube-system.{TRAEFIK_POD}.log"])
        self.assertIn('"meho_version": "0.36.0"', files["install.json"])
        self.assertIn(
            "journalctl -u rke2-server --since -24h --no-pager -o short-iso", self.host.calls()
        )

    def test_the_logs_of_the_previous_run_are_kept_only_when_there_was_one(self) -> None:
        self.assertNotIn("pods/meho.meho-keycloak-0.previous.log", self.bundle())

        self.host.answer("kube_logs_previous_rc", "0")
        self.host.answer("kube_logs_previous", "crashed earlier\n")

        files = self.bundle()

        self.assertEqual("crashed earlier\n", files["pods/meho.meho-keycloak-0.previous.log"])

    def test_no_password_token_or_key_is_in_the_bundle(self) -> None:
        password = "Fake-Admin-Password-7f3a"
        token = "fake-token-9c1d2e"
        unseal_key = "fake-unseal-key-5e2b8a4c"
        self.host.host_file(
            "/var/lib/meho/first-login-passwords",
            f"admin_password={password}\nadmin_username=admin\n",
        )
        self.host.host_file("/var/lib/meho/unseal-key", f"{unseal_key}\n")
        self.host.answer(
            "kube_secret_data",
            f"apiVersion: v1\nkind: Secret\nstringData:\n  credentials: {password}\n",
        )
        self.host.answer(
            "kube_logs",
            f"2026-10-23T11:00:00Z KEYCLOAK_ADMIN_TOKEN={token}\n"
            f"2026-10-23T11:00:01Z the admin logs in with {password}\n"
            f"2026-10-23T11:00:02Z unseal key {unseal_key} loaded\n",
        )
        self.host.answer(
            "journal_out", f"2026-10-23T11:00:00+0000 meho rke2[1234]: Db-Password: {password}\n"
        )
        self.host.answer(
            "kube_node_describe", f"Name: meho.example.com\nAnnotations: api_secret_key={token}\n"
        )

        files = self.bundle()

        for name, text in files.items():
            for value in (password, token, unseal_key):
                with self.subTest(file=name, value=value):
                    self.assertNotIn(value, text)
        log = files["pods/meho.meho-keycloak-0.log"]
        self.assertIn("KEYCLOAK_ADMIN_TOKEN=[removed]", log)
        self.assertIn("logs in with [removed]", log)
        self.assertIn("unseal key [removed] loaded", log)
        self.assertIn("Db-Password: [removed]", files["rke2-server.journal.txt"])
        self.assertIn("api_secret_key=[removed]", files["node.txt"])
        self.assertIn("meho-core-first-login", files["objects/meho.secrets.txt"])
        for call in self.host.calls().splitlines():
            if "secret" in call:
                self.assertNotIn(" -o ", call, call)

    def test_the_bundle_is_written_when_rke2_is_down(self) -> None:
        self.host.answer("kubectl_rc", "1")

        files = self.bundle()

        checks = json.loads(files["check.json"])["checks"]
        self.assertEqual(["fail", "fail"], [check["status"] for check in checks[:2]])
        self.assertIn("(this command failed with exit code 1)", files["node.txt"])
        self.assertEqual([], [name for name in files if name.startswith("pods/")])


class InstallTests(InstallTestCase):
    def test_install_sh_puts_meho_host_on_the_machine(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        installed = self.machine.host_path("/usr/local/sbin/meho-host")
        self.assertEqual((LINUX_SERVER / "meho-host").read_text(), installed.read_text())
        self.assertEqual(0o755, stat.S_IMODE(installed.stat().st_mode))
        self.assertIn("[ok]   installed meho-host to /usr/local/sbin/meho-host", result.stdout)


if __name__ == "__main__":
    unittest.main()
