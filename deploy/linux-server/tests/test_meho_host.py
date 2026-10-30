#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of meho-host: the check list, its JSON form and exit codes, the
support bundle and what is removed from it, the SSH rule of the firewall
(admin-network), uninstall with the host changes of RKE2's CIS profile
(#5133), and the install of meho-host by install.sh."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tarfile
import unittest

from harness import LINUX_SERVER, RKE2_CIS_SYSCTL, FakeMachine, InstallTestCase
from host_harness import (
    ACCOUNT_ANSWERS,
    GROUPDEL_STUB,
    HEALTHY_PODS,
    MEHO_PODS,
    TRAEFIK_POD,
    USERDEL_STUB,
    HostTestCase,
    certificate_table,
    df_answer,
    pod_line,
)

SUPPORT_HINT = "run 'meho-host support-bundle' and send the file to support."
RULES = "/etc/meho/firewall.nft"
# The file install.sh writes with --admin-network 10.0.50.0/24.
RULES_FIXTURE = LINUX_SERVER / "tests" / "fixtures" / "firewall-admin-network.nft"
FIREWALL_UNIT = "/etc/systemd/system/meho-firewall.service"
RKE2_DROPIN = "/etc/systemd/system/rke2-server.service.d/meho-firewall.conf"
RKE2_UNIT = "/etc/systemd/system/rke2-server.service"
RKE2_UNINSTALL = "/usr/local/bin/rke2-uninstall.sh"
MEHO_COMMANDS = ("/usr/local/bin/meho", "/usr/local/sbin/meho-host")
# The fake rke2-uninstall.sh removes what the real one removes: RKE2's
# unit, its data, its binaries and itself.
RKE2_UNINSTALL_STUB = (
    '#!/bin/sh\nprintf \'%s\\n\' "rke2-uninstall.sh" >> "$MEHO_FAKE/calls.log"\n'
    f'rm -rf "$MEHO_TEST_ROOT{RKE2_UNIT}" "$MEHO_TEST_ROOT/var/lib/rancher" '
    '"$MEHO_TEST_ROOT/usr/local/bin/rke2" "$0"\n'
)
REMOVE_FIREWALL = (
    "systemctl disable meho-firewall.service && rm -r "
    "/etc/systemd/system/meho-firewall.service /etc/meho && systemctl daemon-reload && "
    "nft delete table inet meho"
)
# The host changes of RKE2's CIS profile (lib/cis.sh) and the record of the
# parts of the etcd account that the install created.
SYSCTL_FILE = "/etc/sysctl.d/60-rke2-cis.conf"
ETCD_RECORD = "/etc/meho/etcd-account"
# The lines of /etc/passwd and /etc/group without the etcd account. The
# names avoid "passwd", which CodeQL reads as a password.
BASE_USERS = "root:x:0:0:root:/root:/bin/bash\n"
BASE_GROUPS = "root:x:0:\n"
ETCD_USER = "etcd:x:998:998:etcd user:/nonexistent:/sbin/nologin\n"
ETCD_GROUP = "etcd:x:998:\n"
KERNEL_LINE = (
    f"Removing {SYSCTL_FILE} does not change the running kernel. A restart of this "
    "machine brings back the operating system's own kernel values."
)
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
        for text in (
            "check [--json]",
            "support-bundle",
            "admin-network",
            "uninstall",
            "Exit codes:",
        ):
            self.assertIn(text, result.stdout)
        self.assertNotIn("id -u", self.host.calls())
        self.assertFalse(self.host.host_path("/var/lib/meho/support").exists())

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
        self.assertIn("|Succeeded|False|", HEALTHY_PODS)

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
            pod_line("meho", "meho-backplane-7d9f8-x2k4p", "Pending")
            + pod_line("meho", "meho-keycloak-0", "Running", "False")
            + pod_line("kube-system", "rke2-canal-k9x2p", "Running", "True"),
        )

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] pods: meho/meho-backplane-7d9f8-x2k4p is Pending, "
            f"meho/meho-keycloak-0 is not Ready. Fix: {SUPPORT_HINT}",
            result.stdout,
        )

    def test_pods_of_other_namespaces_are_not_checked(self) -> None:
        self.host.answer("kube_pods", HEALTHY_PODS + pod_line("default", "debug-shell", "Pending"))

        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(OK_LINES[1], result.stdout)

    def test_no_pod_in_meho_means_the_core_is_not_installed(self) -> None:
        self.host.answer(
            "kube_pods", pod_line("kube-system", "rke2-canal-k9x2p", "Running", "True")
        )

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] pods: no pod in namespace meho, so the MEHO core is not installed. "
            f"Fix: {SUPPORT_HINT}",
            result.stdout,
        )

    def test_an_evicted_pod_is_a_warning_with_a_fix(self) -> None:
        evicted = pod_line("meho", "meho-backplane-7d9f8-old", "Failed", "False", "Evicted")
        self.host.answer("kube_pods", HEALTHY_PODS + evicted)

        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(
            "[WARN] pods: evicted by the kubelet when the disk was full: "
            "meho/meho-backplane-7d9f8-old; the other pods run. Fix: check the disk line. "
            "An evicted entry does no harm, and support removes it.",
            result.stdout,
        )

    def test_a_pod_that_failed_for_another_reason_is_red(self) -> None:
        failed = pod_line("meho", "meho-core-bootstrap-x", "Failed", "False", "DeadlineExceeded")
        self.host.answer("kube_pods", HEALTHY_PODS + failed)

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn("[FAIL] pods: meho/meho-core-bootstrap-x is Failed.", result.stdout)

    def test_a_disk_below_20_percent_free_warns_and_the_exit_code_stays_0(self) -> None:
        self.host.answer("df_meho", df_answer(200, 36))

        result = self.host.run("check")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(
            "[WARN] disk: /var/lib/rancher has 150 GB free (75%); /var/lib/meho has 36 GB free "
            "(18%). Fix: free space on the disk or make it larger, before it falls below 15%.",
            result.stdout,
        )
        checks = json.loads(self.host.run("check", "--json").stdout)["checks"]
        self.assertEqual("warn", checks[2]["status"])
        self.assertTrue(checks[2]["hint"].startswith("free space"), checks[2])

    def test_a_disk_below_15_percent_free_is_red(self) -> None:
        self.host.answer("df_rancher", df_answer(200, 24))

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] disk: /var/lib/rancher has 24 GB free (12%); /var/lib/meho has 150 GB free "
            "(75%). Fix: free space on the disk or make it larger. Below 15% the built-in "
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

    def test_a_certificate_that_is_not_yet_valid_is_red(self) -> None:
        self.host.answer(
            "rke2_cert_table",
            certificate_table(
                ("client-admin.crt", "system:admin", 365, "NOT YET VALID"),
                ("server-ca.crt", "rke2-server-ca", 3650, "OK"),
            ),
        )

        result = self.host.run("check")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "[FAIL] certificates: not yet valid: client-admin.crt. Fix: the clock of this machine "
            f"or the certificate is wrong. Check the time: timedatectl. Then {SUPPORT_HINT}",
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
        lines = result.stdout.splitlines()
        self.assertEqual(BUNDLE_PATH, lines[0])
        self.assertIn("then delete it", lines[1])
        self.assertEqual(2, len(lines))
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

    def test_no_secret_is_in_the_bundle_or_on_a_command_line(self) -> None:
        admin_pass = "Fake-Admin-Password-7f3a"
        approver_pass = "Fake-Approver-Password-9c1d"
        db_pass = "Fake-Database-Password-3e5f"
        token = "fake-token-9c1d2e"
        unseal_key = "fake-unseal-key-5e2b8a4c"
        secrets = (admin_pass, approver_pass, db_pass, token, unseal_key)
        # The customer deletes the two one-time files after the first login,
        # so the database password is only in the core's Secrets.
        self.host.host_file(
            "/var/lib/meho/first-login-passwords",
            f"admin_password={admin_pass}\nadmin_username=admin\n"
            f"approver_password={approver_pass}\napprover_username=approver\n",
        )
        self.host.host_file("/var/lib/meho/unseal-key", f"{unseal_key}\n")
        self.host.answer(
            "kube_secret_values",
            f"admin_password={admin_pass}\nadmin_username=admin\n"
            f"approver_username=approver\ndb_password={db_pass}\nshort=abc\n",
        )
        self.host.answer(
            "kube_logs",
            f"2026-10-23T11:00:00Z KEYCLOAK_ADMIN_TOKEN={token}\n"
            f"2026-10-23T11:00:01Z the admin logs in with {admin_pass}\n"
            f"2026-10-23T11:00:02Z unseal key {unseal_key} loaded\n"
            f"2026-10-23T11:00:03Z the db password is {db_pass}\n"
            f"2026-10-23T11:00:04Z api_key={token} KEYCLOAK_ADMIN_URL: http://meho-keycloak:8080/auth\n"
            "2026-10-23T11:00:05Z short values such as abc stay\n"
            f'{{"event": "login", "user": "approver", "password": "{approver_pass}"}}\n'
            f'{{"token":"{token}"}}\n'
            f'{{"apiKey": "{token}", "keycloak_url": "http://meho-keycloak:8080"}}\n'
            f"token:{token}\n",
        )
        self.host.answer(
            "journal_out", f"2026-10-23T11:00:00+0000 meho rke2[1234]: Db-Password: {admin_pass}\n"
        )
        self.host.answer(
            "kube_node_describe", f"Name: meho.example.com\nAnnotations: api_secret_key={token}\n"
        )
        self.host.answer(
            "kube_events",
            "LAST SEEN   TYPE     REASON   OBJECT                MESSAGE\n"
            "3h          Normal   Pulled   pod/meho-keycloak-0   "
            "image quay.io/keycloak/keycloak:26.8.1\n",
        )

        files = self.bundle()

        for name, text in files.items():
            for value in secrets:
                with self.subTest(file=name, value=value):
                    self.assertNotIn(value, text)
        log = files["pods/meho.meho-keycloak-0.log"]
        self.assertIn("KEYCLOAK_ADMIN_TOKEN=[removed]", log)
        self.assertIn("the admin logs in with [removed]", log)
        self.assertIn("unseal key [removed] loaded", log)
        self.assertIn("the db password is [removed]", log)
        self.assertIn("api_key=[removed] KEYCLOAK_ADMIN_URL: http://meho-keycloak:8080/auth", log)
        self.assertIn("short values such as abc stay", log)
        self.assertIn('{"event": "login", "user": "approver", "password": "[removed]"}', log)
        self.assertIn('{"token":"[removed]"}', log)
        self.assertIn('{"apiKey": "[removed]", "keycloak_url": "http://meho-keycloak:8080"}', log)
        self.assertIn("token:[removed]", log)
        self.assertIn("Db-Password: [removed]", files["rke2-server.journal.txt"])
        self.assertIn("api_secret_key=[removed]", files["node.txt"])
        self.assertIn("quay.io/keycloak/keycloak:26.8.1", files["events.txt"])
        self.assertIn("meho-core-first-login", files["objects/meho.secrets.txt"])
        # No value was on the command line of any command the script ran,
        # and the files with the values and the sed rules are gone.
        calls = self.host.calls()
        self.assertIn("sed -E -i -f ", calls)
        # The fake kubectl does not run the template, so the test checks
        # that it leaves Helm's release records out.
        self.assertIn(
            'get secrets -o go-template={{range .items}}{{if ne .type "helm.sh/release.v1"}}',
            calls,
        )
        for value in secrets:
            with self.subTest(value=value):
                self.assertNotIn(value, calls)
        folder = self.host.host_path("/var/lib/meho/support")
        self.assertEqual(
            [self.host.host_path(BUNDLE_PATH).name], [path.name for path in folder.iterdir()]
        )

    def test_the_bundle_is_written_when_rke2_is_down(self) -> None:
        self.host.answer("kubectl_rc", "1")

        files = self.bundle()

        checks = json.loads(files["check.json"])["checks"]
        self.assertEqual(["fail", "fail"], [check["status"] for check in checks[:2]])
        self.assertIn("(this command failed with exit code 1)", files["node.txt"])
        self.assertEqual([], [name for name in files if name.startswith("pods/")])


class AdminNetworkTests(HostTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.host.host_file(RULES, RULES_FIXTURE.read_text())

    def rules(self) -> str:
        return self.host.host_path(RULES).read_text()

    def admin_set(self) -> str:
        rules = self.rules()
        start = rules.index("    set admin_network {")
        return rules[start : rules.index("    }\n", start) + 6]

    def test_a_range_changes_only_the_ssh_rule(self) -> None:
        before = self.rules().splitlines()

        result = self.host.run("admin-network", "10.0.60.0/24")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual("[ok]   SSH is allowed from 10.0.60.0/24\n", result.stdout)
        after = self.rules().splitlines()
        self.assertEqual(len(before), len(after))
        changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
        self.assertEqual(
            [
                (
                    "    # Given with --admin-network at the install.",
                    "    # Set with meho-host admin-network.",
                ),
                ("        elements = { 10.0.50.0/24 }", "        elements = { 10.0.60.0/24 }"),
            ],
            changed,
        )
        # The new file is checked before it takes the old one's place, then
        # loaded from its place.
        calls = self.host.calls()
        self.assertRegex(calls, r"nft -c -f \S*/etc/meho/firewall\.nft\.\S+\n")
        self.assertIn(f"nft -f {self.host.host_path(RULES)}\n", calls)
        self.assertLess(calls.index("nft -c -f"), calls.index("nft -f "))
        self.assertEqual(["firewall.nft"], os.listdir(self.host.host_path("/etc/meho")))

    def test_one_address_is_a_range_too(self) -> None:
        result = self.host.run("admin-network", "10.0.50.9")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn("        elements = { 10.0.50.9 }\n", self.admin_set())

    def test_none_closes_ssh_from_every_address(self) -> None:
        result = self.host.run("admin-network", "none")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn("SSH is closed for every address; only the console works", result.stdout)
        self.assertNotIn("elements", self.admin_set())
        self.assertIn("ip saddr @admin_network tcp dport 22 accept", self.rules())
        self.assertNotIn("Type yes", self.output(result))

    def test_over_ssh_from_outside_the_range_it_asks_and_stops_without_yes(self) -> None:
        before = self.rules()

        result = self.host.run("admin-network", "192.168.1.0/24", ssh_client="10.0.0.5 51234 22")

        self.assertEqual(1, result.returncode, self.output(result))
        self.assertIn(
            "[WARN] This SSH session comes from 10.0.0.5, which is outside 192.168.1.0/24.",
            result.stderr,
        )
        self.assertIn("Change the SSH rule anyway? Type yes to go on: ", result.stdout)
        self.assertIn("Stopped. Nothing was changed.", result.stdout)
        self.assertEqual(before, self.rules())
        self.assertNotIn("\nnft ", self.host.calls())

    def test_over_ssh_a_yes_changes_the_rule(self) -> None:
        result = self.host.run(
            "admin-network", "192.168.1.0/24", stdin="yes\n", ssh_client="10.0.0.5 51234 22"
        )

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn("        elements = { 192.168.1.0/24 }\n", self.admin_set())

    def test_over_ssh_inside_the_range_it_does_not_ask(self) -> None:
        result = self.host.run("admin-network", "10.0.0.0/16", ssh_client="10.0.0.5 51234 22")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertNotIn("Type yes", self.output(result))
        self.assertEqual("", result.stderr)

    def test_none_over_ssh_asks_too(self) -> None:
        result = self.host.run("admin-network", "none", ssh_client="10.0.0.5 51234 22")

        self.assertEqual(1, result.returncode, self.output(result))
        self.assertIn("closes SSH for every address", result.stderr)
        self.assertIn("        elements = { 10.0.50.0/24 }\n", self.admin_set())

    def test_under_sudo_the_session_is_found_in_a_parent_process(self) -> None:
        # sudo resets the environment, so meho-host itself has no SSH_CLIENT.
        # The processes above it are read from /proc, which the fake root holds.
        parent = os.getpid()
        self.host.host_file(f"/proc/{parent}/environ", "HOME=/root\0TERM=xterm\0")
        self.host.host_file(f"/proc/{parent}/status", "Name:\tsudo\nPPid:\t4242\n")
        self.host.host_file(
            "/proc/4242/environ", "USER=admin\0SSH_CONNECTION=10.0.50.7 40000 10.0.0.5 22\0"
        )

        result = self.host.run("admin-network", "192.168.1.0/24")

        self.assertEqual(1, result.returncode, self.output(result))
        self.assertIn("This SSH session comes from 10.0.50.7", result.stderr)

    def test_a_file_nft_refuses_keeps_the_old_file(self) -> None:
        self.host.answer("nft_check_rc", "1")
        self.host.answer("nft_check_out", "Error: syntax error, unexpected end of file\n")
        before = self.rules()

        result = self.host.run("admin-network", "10.0.60.0/24")

        self.assertEqual(1, result.returncode)
        self.assertIn("Error: syntax error, unexpected end of file", result.stderr)
        self.assertIn(
            "[FAIL] nft does not accept the new firewall file, so /etc/meho/firewall.nft was "
            "not changed.",
            result.stderr,
        )
        self.assertEqual(before, self.rules())
        self.assertNotIn("nft -f ", self.host.calls())
        self.assertEqual(["firewall.nft"], os.listdir(self.host.host_path("/etc/meho")))

    def test_a_set_not_in_the_form_of_install_sh_is_red_and_nothing_changes(self) -> None:
        fixture = RULES_FIXTURE.read_text()
        edits = {
            "a tab before flags interval": (
                "10.0.60.0/24",
                fixture.replace("        flags interval\n", "\tflags interval\n"),
            ),
            "the note line above the set removed": (
                "none",
                fixture.replace("    # Given with --admin-network at the install.\n", ""),
            ),
        }
        for name, (value, rules) in edits.items():
            with self.subTest(edit=name):
                self.host.host_file(RULES, rules)

                result = self.host.run("admin-network", value)

                self.assertEqual(1, result.returncode)
                self.assertIn(
                    "[FAIL] the set admin_network in /etc/meho/firewall.nft is not in the form "
                    "install.sh writes, so meho-host cannot change it. Nothing was changed.",
                    result.stderr,
                )
                self.assertEqual(rules, self.rules())
                self.assertNotIn("\nnft ", "\n" + self.host.calls())
                self.assertEqual(["firewall.nft"], os.listdir(self.host.host_path("/etc/meho")))

    def test_a_file_without_the_set_is_red(self) -> None:
        self.host.host_file(RULES, "table inet meho {\n}\n")

        result = self.host.run("admin-network", "10.0.60.0/24")

        self.assertEqual(1, result.returncode)
        self.assertIn("has no set admin_network", result.stderr)

    def test_a_bad_value_is_a_usage_error(self) -> None:
        for value in ("10.0.0", "10.0.50.0/33", "fe80::1", "all"):
            with self.subTest(value=value):
                self.assert_usage_error(
                    self.host.run("admin-network", value), f"'{value}' is not an IPv4 range"
                )
        self.assert_usage_error(self.host.run("admin-network"), "takes one value")
        # The same rule as install.sh --admin-network.
        self.assert_usage_error(
            self.host.run("admin-network", "10.0.50.7/24"),
            "10.0.50.7/24 does not start at the first address of its range.",
        )

    def test_every_address_gets_a_warning(self) -> None:
        result = self.host.run("admin-network", "0.0.0.0/0", ssh_client="10.0.0.5 51234 22")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn(
            "[WARN] admin-network 0.0.0.0/0 allows SSH from every address.", result.stderr
        )
        self.assertIn("        elements = { 0.0.0.0/0 }\n", self.admin_set())


def etcd_record(*parts: str) -> str:
    """The record of the etcd account, as install.sh writes it."""
    return (
        "# Written by MEHO install.sh: the parts of the etcd account for RKE2's\n"
        "# etcd that the install created. meho-host uninstall removes only these.\n"
        + "".join(f"{part}\n" for part in parts)
    )


class UninstallTests(HostTestCase):
    """A machine after a fresh install: the install created the etcd user and
    group, and its record says so."""

    def setUp(self) -> None:
        super().setUp()
        self.host.host_file("/var/lib/meho/volumes/postgres/data", "rows\n")
        self.host.host_file("/var/lib/meho/first-login-passwords", "admin_password=x\n")
        self.host.host_file("/etc/meho/install.json", "{}\n")
        self.host.host_file("/etc/meho/values-place.yaml", "tenant: x\n")
        self.host.host_file(RULES, RULES_FIXTURE.read_text())
        self.host.host_file(FIREWALL_UNIT, "[Unit]\n")
        self.host.host_file(RKE2_DROPIN, "[Unit]\n")
        self.host.host_file(RKE2_UNIT, "[Unit]\n")
        for command in MEHO_COMMANDS:
            self.host.host_file(command, "#!/bin/sh\n")
        self.host.host_file("/var/lib/rancher/rke2/server/db/state.db", "etcd\n")
        uninstall = self.host.host_path(RKE2_UNINSTALL)
        uninstall.write_text(RKE2_UNINSTALL_STUB)
        uninstall.chmod(0o755)
        self.host.host_file(SYSCTL_FILE, RKE2_CIS_SYSCTL)
        self.host.host_file("/etc/passwd", BASE_USERS + ETCD_USER)
        self.host.host_file("/etc/group", BASE_GROUPS + ETCD_GROUP)
        self.host.host_file(ETCD_RECORD, etcd_record("user", "group"))

    def uninstall(self) -> subprocess.CompletedProcess[str]:
        result = self.host.run("uninstall", stdin="yes\nyes\n")
        self.assertEqual(0, result.returncode, self.output(result))
        return result

    def host_text(self, path: str) -> str:
        return self.host.host_path(path).read_text()

    def assert_nothing_removed(self) -> None:
        for path in (
            "/var/lib/meho/volumes/postgres/data",
            "/etc/meho/install.json",
            ETCD_RECORD,
            SYSCTL_FILE,
            RKE2_UNIT,
            *MEHO_COMMANDS,
        ):
            self.assertTrue(self.host.host_path(path).exists(), path)
        self.assertEqual(BASE_USERS + ETCD_USER, self.host_text("/etc/passwd"))
        self.assertEqual(BASE_GROUPS + ETCD_GROUP, self.host_text("/etc/group"))
        calls = self.host.calls()
        for command in ("rke2-uninstall.sh", "userdel", "groupdel"):
            self.assertNotIn(command, calls)

    def test_it_asks_twice_and_stops_without_two_yes(self) -> None:
        for answers in ("", "no\n", "yes\n", "yes\nno\n"):
            with self.subTest(answers=answers):
                result = self.host.run("uninstall", stdin=answers)

                self.assertEqual(1, result.returncode, self.output(result))
                self.assertIn("Stopped. Nothing was changed.", result.stdout)
                self.assert_nothing_removed()

    def test_the_question_names_the_host_changes_of_the_cis_profile(self) -> None:
        result = self.host.run("uninstall", stdin="")

        self.assertIn(
            f"It also removes RKE2's kernel settings file {SYSCTL_FILE} and the etcd user"
            " and group that the install created.",
            result.stdout,
        )

    def test_after_two_yes_meho_and_rke2_are_gone_and_the_firewall_stays(self) -> None:
        result = self.uninstall()

        self.assertEqual(2, result.stdout.count("Type yes to go on: "))
        self.assertIn("rke2-uninstall.sh\n", self.host.calls())
        for path in (
            RKE2_UNIT,
            "/var/lib/rancher",
            "/var/lib/meho",
            RKE2_DROPIN,
            RKE2_DROPIN.rsplit("/", 1)[0],
            *MEHO_COMMANDS,
        ):
            self.assertFalse(self.host.host_path(path).exists(), path)
        self.assertEqual(["firewall.nft"], os.listdir(self.host.host_path("/etc/meho")))
        self.assertEqual(RULES_FIXTURE.read_text(), self.host.host_path(RULES).read_text())
        self.assertTrue(self.host.host_path(FIREWALL_UNIT).exists())
        self.assertNotIn("\nnft ", self.host.calls())
        self.assertIn("systemctl daemon-reload\n", self.host.calls())
        self.assertEqual(
            "The MEHO firewall stays on, so this machine is never without one. When your own "
            f"firewall is ready, remove it with: {REMOVE_FIREWALL}",
            result.stdout.splitlines()[-2],
        )

    def test_the_kernel_settings_file_goes_and_the_last_line_names_a_restart(self) -> None:
        result = self.uninstall()

        self.assertFalse(self.host.host_path(SYSCTL_FILE).exists())
        self.assertTrue(self.host.host_path("/etc/sysctl.d").is_dir())
        self.assertIn(f"[ok]   removed RKE2's kernel settings file {SYSCTL_FILE}\n", result.stdout)
        self.assertEqual(KERNEL_LINE, result.stdout.splitlines()[-1])

    def test_without_the_kernel_settings_file_the_last_line_stays(self) -> None:
        self.host.host_path(SYSCTL_FILE).unlink()

        result = self.uninstall()

        self.assertNotIn("removed RKE2's kernel settings file", result.stdout)
        self.assertEqual(KERNEL_LINE, result.stdout.splitlines()[-1])

    def test_the_etcd_user_and_group_that_the_install_created_go(self) -> None:
        result = self.uninstall()

        self.assertEqual(BASE_USERS, self.host_text("/etc/passwd"))
        self.assertEqual(BASE_GROUPS, self.host_text("/etc/group"))
        calls = self.host.calls().splitlines()
        self.assertIn("userdel etcd", calls)
        # userdel removed the group with the user (USERGROUPS_ENAB yes).
        self.assertNotIn("groupdel", self.host.calls())
        self.assertIn(
            "[ok]   removed the etcd user and group that the install created for RKE2's etcd\n",
            result.stdout,
        )
        self.assertNotIn("[note]", result.stdout)
        self.assertFalse(self.host.host_path(ETCD_RECORD).exists())

    def test_the_account_goes_after_rke2_and_before_the_record(self) -> None:
        self.uninstall()

        calls = self.host.calls().splitlines()
        self.assertLess(calls.index("rke2-uninstall.sh"), calls.index("userdel etcd"))
        self.assertLess(calls.index("userdel etcd"), calls.index("systemctl daemon-reload"))

    def test_groupdel_removes_the_group_when_userdel_leaves_it(self) -> None:
        self.host.answer("usergroups_enab", "no")

        result = self.uninstall()

        calls = self.host.calls().splitlines()
        self.assertLess(calls.index("userdel etcd"), calls.index("groupdel etcd"))
        self.assertEqual(BASE_USERS, self.host_text("/etc/passwd"))
        self.assertEqual(BASE_GROUPS, self.host_text("/etc/group"))
        self.assertIn("[ok]   removed the etcd user and group that the install", result.stdout)

    def test_an_etcd_user_from_before_the_install_stays(self) -> None:
        self.host.host_file(ETCD_RECORD, etcd_record("group"))

        result = self.uninstall()

        self.assertEqual(BASE_USERS + ETCD_USER, self.host_text("/etc/passwd"))
        self.assertEqual(BASE_GROUPS, self.host_text("/etc/group"))
        self.assertNotIn("userdel", self.host.calls())
        self.assertIn("groupdel etcd\n", self.host.calls())
        self.assertIn(
            "[ok]   removed the etcd group that the install created for RKE2's etcd\n",
            result.stdout,
        )
        self.assertIn(
            "[note] kept the etcd user: the install did not record that it created it, so "
            "another program may use it. If nothing else uses it, remove it with: userdel etcd\n",
            result.stdout,
        )

    def test_an_etcd_group_from_before_the_install_stays_and_so_does_the_user(self) -> None:
        self.host.host_file(ETCD_RECORD, etcd_record("user"))

        result = self.uninstall()

        self.assertEqual(BASE_USERS + ETCD_USER, self.host_text("/etc/passwd"))
        self.assertEqual(BASE_GROUPS + ETCD_GROUP, self.host_text("/etc/group"))
        for command in ("userdel", "groupdel"):
            self.assertNotIn(command, self.host.calls())
        self.assertIn(
            "[note] kept the etcd user and group. The install created the user, but the group"
            " existed before the install, and userdel would remove the group together with the"
            " user. If nothing else uses the group, remove both with: userdel etcd, then"
            " groupdel etcd if the group is still there.\n",
            result.stdout,
        )

    def test_without_a_record_the_etcd_account_stays_and_the_note_says_how_to_remove_it(
        self,
    ) -> None:
        # An install before #5133 wrote no record, and an install that found
        # the user and group writes none either.
        self.host.host_path(ETCD_RECORD).unlink()

        result = self.uninstall()

        self.assertEqual(BASE_USERS + ETCD_USER, self.host_text("/etc/passwd"))
        self.assertEqual(BASE_GROUPS + ETCD_GROUP, self.host_text("/etc/group"))
        for command in ("userdel", "groupdel"):
            self.assertNotIn(command, self.host.calls())
        self.assertIn(
            "[note] kept the etcd user and group: the install did not record that it created"
            " them, so another program may use them. If nothing else uses them, remove them"
            " with: userdel etcd, then groupdel etcd if the group is still there.\n",
            result.stdout,
        )
        self.assertEqual(KERNEL_LINE, result.stdout.splitlines()[-1])

    def test_without_an_etcd_account_nothing_is_said_about_it(self) -> None:
        self.host.host_file("/etc/passwd", BASE_USERS)
        self.host.host_file("/etc/group", BASE_GROUPS)

        result = self.uninstall()

        for text in ("removed the etcd", "kept the etcd"):
            self.assertNotIn(text, result.stdout)
        for command in ("userdel", "groupdel"):
            self.assertNotIn(command, self.host.calls())

    def test_a_failed_userdel_is_a_warning_with_the_command_and_uninstall_goes_on(self) -> None:
        self.host.answer("userdel_rc", "8")
        self.host.answer("groupdel_rc", "8")

        result = self.host.run("uninstall", stdin="yes\nyes\n")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertIn("userdel: user etcd is currently used by process 4242", result.stderr)
        self.assertIn(
            "[WARN] userdel could not remove the etcd user, which the install created. Read its"
            " message above, then run: userdel etcd\n",
            result.stderr,
        )
        self.assertIn(
            "[WARN] groupdel could not remove the etcd group, which the install created. Read"
            " its message above, then run: groupdel etcd\n",
            result.stderr,
        )
        self.assertNotIn("removed the etcd", result.stdout)
        self.assertNotIn("[note] kept the etcd", result.stdout)
        self.assertFalse(self.host.host_path("/var/lib/meho").exists())
        self.assertEqual(KERNEL_LINE, result.stdout.splitlines()[-1])

    def test_without_the_rke2_script_the_meho_files_still_go(self) -> None:
        self.host.host_path(RKE2_UNINSTALL).unlink()

        result = self.uninstall()

        self.assertIn(f"[note] {RKE2_UNINSTALL} is not on this machine", result.stdout)
        self.assertFalse(self.host.host_path("/var/lib/meho").exists())
        self.assertEqual(["firewall.nft"], os.listdir(self.host.host_path("/etc/meho")))

    def test_a_failed_rke2_script_stops_before_the_meho_files(self) -> None:
        self.host.host_path(RKE2_UNINSTALL).write_text("#!/bin/sh\necho 'rke2: no' >&2\nexit 1\n")

        result = self.host.run("uninstall", stdin="yes\nyes\n")

        self.assertEqual(1, result.returncode)
        self.assertIn("rke2: no", result.stderr)
        self.assertIn("[FAIL] RKE2's uninstall script", result.stderr)
        self.assertTrue(self.host.host_path("/var/lib/meho/volumes/postgres/data").exists())
        self.assertTrue(self.host.host_path(SYSCTL_FILE).exists())
        self.assertTrue(self.host.host_path(ETCD_RECORD).exists())
        self.assertNotIn("userdel", self.host.calls())

    def test_uninstall_takes_no_option(self) -> None:
        self.assert_usage_error(self.host.run("uninstall", "--yes"), "uninstall takes no option")


def run_installed_meho_host(machine: FakeMachine, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the meho-host that install.sh put on the fake machine, with two
    yes answers. The install's stubs answer id and systemctl, the stubs of
    host_harness.py answer userdel and groupdel, and find and rmdir are the
    real tools."""
    for tool in ("find", "rmdir"):
        link = machine.tools / tool
        if not link.exists():
            found = shutil.which(tool)
            if found is None:
                raise RuntimeError(f"the tests need '{tool}' on PATH")
            link.symlink_to(found)
    machine.add_stub("userdel", USERDEL_STUB)
    machine.add_stub("groupdel", GROUPDEL_STUB)
    for name, content in ACCOUNT_ANSWERS.items():
        machine.answer(name, content)
    return subprocess.run(
        [str(machine.host_path("/usr/local/sbin/meho-host")), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{machine.stubs}:{machine.tools}",
            "MEHO_TEST_ROOT": str(machine.root),
            "MEHO_FAKE": str(machine.fake),
            "LC_ALL": "C",
        },
        input="yes\nyes\n",
        check=False,
        timeout=60,
    )


class InstallUpdateUninstallTests(InstallTestCase):
    """The record goes from the first install through an update to
    uninstall. The update finds the user and group that the first install
    created, so it must keep the first install's record (#5133)."""

    def update(self) -> None:
        self.machine.mark_installed()
        self.machine.set_download_version("0.37.0")
        self.assert_succeeds(self.machine.run("--confirm-backup"))

    def etc_text(self, name: str) -> str:
        return self.machine.host_path(f"/etc/{name}").read_text()

    def test_the_update_keeps_the_record_and_uninstall_removes_the_account(self) -> None:
        self.assert_succeeds(self.machine.run())
        record = self.machine.host_path(ETCD_RECORD).read_text()
        self.assertEqual(etcd_record("user", "group"), record)
        self.update()
        self.assertEqual(record, self.machine.host_path(ETCD_RECORD).read_text())

        result = run_installed_meho_host(self.machine, "uninstall")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotRegex(self.etc_text("passwd"), r"(?m)^etcd:")
        self.assertNotRegex(self.etc_text("group"), r"(?m)^etcd:")
        self.assertFalse(self.machine.host_path(SYSCTL_FILE).exists())
        self.assertEqual(KERNEL_LINE, result.stdout.splitlines()[-1])

    def test_an_account_from_before_the_install_stays_through_update_and_uninstall(
        self,
    ) -> None:
        users = BASE_USERS + "etcd:x:991:991::/nonexistent:/sbin/nologin\n"
        groups = BASE_GROUPS + "etcd:x:991:\n"
        self.machine.host_file("/etc/passwd", users)
        self.machine.host_file("/etc/group", groups)
        self.assert_succeeds(self.machine.run())
        self.update()
        self.assertFalse(self.machine.host_path(ETCD_RECORD).exists())

        result = run_installed_meho_host(self.machine, "uninstall")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(users, self.etc_text("passwd"))
        self.assertEqual(groups, self.etc_text("group"))
        self.assertIn("[note] kept the etcd user and group:", result.stdout)
        self.assertNotIn("userdel", self.machine.calls())


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
