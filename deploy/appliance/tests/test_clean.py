#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of clean.sh: what it deletes, what it keeps, the order of the
steps, and the check that fails the build when anything is left."""

from __future__ import annotations

import shutil
import unittest

from clean_harness import BUILD_USER, LABEL, RKE2, CleanTestCase

GONE = (
    "/var/lib/meho/first-login-passwords",
    "/var/lib/meho/unseal-key",
    "/var/lib/meho/volumes",
    f"{RKE2}/server/db",
    f"{RKE2}/server/tls",
    f"{RKE2}/server/cred",
    f"{RKE2}/server/token",
    f"{RKE2}/server/agent-token",
    f"{RKE2}/agent/containerd",
    f"{RKE2}/agent/client-ca.crt",
    f"{RKE2}/agent/kubelet.kubeconfig",
    "/etc/rancher/node/password",
    "/etc/rancher/rke2/rke2.yaml",
    "/etc/ssh/ssh_host_ed25519_key",
    "/etc/ssh/ssh_host_ed25519_key.pub",
    "/etc/netplan/60-meho-build.yaml",
    "/etc/sudoers.d/90-cloud-init-users",
    "/var/lib/cloud",
    "/var/log/cloud-init.log",
    "/root/.bash_history",
    f"/home/{BUILD_USER}",
)

KEPT = (
    "/var/lib/meho",
    f"{RKE2}/server/manifests/local-path-storage.yaml",
    f"{RKE2}/server/manifests/rke2-traefik-config.yaml",
    f"{RKE2}/agent/images/meho-images.linux-amd64.tar.zst",
    f"{RKE2}/agent/images/rke2-images.linux-amd64.tar.zst",
    "/etc/rancher/rke2/config.yaml",
    "/etc/rancher/rke2/meho-pss.yaml",
    "/etc/ssh/sshd_config",
    "/etc/cloud/cloud-init.disabled",
    "/opt/meho/appliance/download/install.sh",
    "/opt/meho/appliance/download/VERSION",
)


class CleanTests(CleanTestCase):
    def test_it_deletes_what_must_differ_and_keeps_the_rest(self) -> None:
        result = self.vm.run()

        self.assert_succeeds(result)
        for path in GONE:
            with self.subTest(gone=path):
                self.assertFalse(self.vm.host_path(path).exists(), path)
        for path in KEPT:
            with self.subTest(kept=path):
                self.assertTrue(self.vm.host_path(path).exists(), path)
        self.assertEqual(["sh.helm.release.v1.meho.v1"], self.vm.secrets())
        self.assertEqual("uninitialized\n", self.vm.host_path("/etc/machine-id").read_text())
        for log in ("/var/log/syslog", "/var/log/pods/meho_core/0.log"):
            with self.subTest(log=log):
                self.assertEqual(0, self.vm.host_path(log).stat().st_size)
        self.assertIn("[ok]   nothing that must differ between appliances is left", result.stdout)

    def test_the_steps_run_in_the_order_of_the_design(self) -> None:
        self.assert_succeeds(self.vm.run())

        calls = self.vm.calls()
        expected = [
            f"-n meho delete secret -l {LABEL}",
            "-n meho delete secret meho-core-tls",
            "systemctl disable --now rke2-server",
            f"loginctl terminate-user {BUILD_USER}",
            f"userdel -r {BUILD_USER}",
            "cloud-init clean --logs --machine-id",
            "meho-host admin-network none",
            "journalctl --rotate",
            "journalctl --vacuum-time=1s",
            "fstrim -av",
            "systemctl poweroff",
        ]
        positions = [
            next(i for i, call in enumerate(calls) if call.endswith(line)) for line in expected
        ]
        self.assertEqual(sorted(positions), positions, "\n".join(calls))
        self.assertEqual("systemctl poweroff", calls[-1])

    def test_a_secret_that_survives_the_delete_stops_before_anything_is_removed(self) -> None:
        self.vm.answer("secrets_stick", "1")

        result = self.vm.run()

        self.assert_fails_once(result, "meho-core-postgres-superuser", "Nothing was removed")
        self.assertTrue(self.vm.host_path("/var/lib/meho/unseal-key").exists())
        self.assertTrue(self.vm.host_path(f"{RKE2}/server/tls").exists())
        self.assertNotIn("systemctl poweroff", self.vm.calls())

    def test_a_failed_admin_network_none_stops_without_power_off(self) -> None:
        self.vm.answer("meho_host_rc", "1")

        result = self.vm.run()

        self.assertEqual(1, result.returncode, self.output(result))
        self.assertIn("[FAIL] clean.sh stopped at line", result.stderr)
        self.assertNotIn("systemctl poweroff", self.vm.calls())

    def test_the_build_user_name_is_an_option(self) -> None:
        self.vm.answer(
            "passwd", "root:x:0:0::/root:/bin/bash\nbuilder:x:1000:1000::/home/builder:/bin/bash\n"
        )
        shutil.rmtree(self.vm.host_path(f"/home/{BUILD_USER}"))
        self.vm.host_file("/home/builder/.ssh/authorized_keys", "fake key\n")

        result = self.vm.run("--build-user", "builder")

        self.assert_succeeds(result)
        self.assertIn("userdel -r builder", self.vm.calls())
        self.assertFalse(self.vm.host_path("/home/builder").exists())

    def test_wrong_usage_is_exit_2(self) -> None:
        result = self.vm.run("--wrong")

        self.assertEqual(2, result.returncode)
        self.assertIn("unknown option: --wrong", result.stderr)


class CheckTests(CleanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assert_succeeds(self.vm.run())
        self.vm.answer("calls.log", "")

    def test_a_clean_machine_passes_and_is_not_powered_off(self) -> None:
        result = self.vm.run("--check")

        self.assert_succeeds(result)
        self.assertEqual([], self.left_lines(result))
        self.assertNotIn("systemctl poweroff", self.vm.calls())

    def test_each_leftover_is_found_and_named(self) -> None:
        # Planted file, what to remove after the check, the [LEFT] line.
        leftovers = {
            "a file under /var/lib/meho": (
                "/var/lib/meho/unseal-key",
                "/var/lib/meho/unseal-key",
                "files under /var/lib/meho",
            ),
            "the etcd database": (
                f"{RKE2}/server/db/etcd/db",
                f"{RKE2}/server/db",
                f"{RKE2}/server/db is still there",
            ),
            "the server certificates": (
                f"{RKE2}/server/tls/ca.key",
                f"{RKE2}/server/tls",
                f"{RKE2}/server/tls is still there",
            ),
            "the secrets key": (
                f"{RKE2}/server/cred/enc.json",
                f"{RKE2}/server/cred",
                f"{RKE2}/server/cred is still there",
            ),
            "a token file": (
                f"{RKE2}/server/node-token",
                f"{RKE2}/server/node-token",
                f"{RKE2}/server/node-token is still there",
            ),
            "agent state": (
                f"{RKE2}/agent/kubelet.kubeconfig",
                f"{RKE2}/agent/kubelet.kubeconfig",
                f"RKE2 state under {RKE2}/agent/ other than images/",
            ),
            "the node password": (
                "/etc/rancher/node/password",
                "/etc/rancher/node/password",
                "/etc/rancher/node/password is still there",
            ),
            "the kubeconfig": (
                "/etc/rancher/rke2/rke2.yaml",
                "/etc/rancher/rke2/rke2.yaml",
                "/etc/rancher/rke2/rke2.yaml is still there",
            ),
            "an SSH host key": (
                "/etc/ssh/ssh_host_rsa_key",
                "/etc/ssh/ssh_host_rsa_key",
                "SSH host keys under /etc/ssh",
            ),
            "the build's network file": (
                "/etc/netplan/60-meho-build.yaml",
                "/etc/netplan/60-meho-build.yaml",
                "/etc/netplan/60-meho-build.yaml is still there",
            ),
            "the build user's home": (
                f"/home/{BUILD_USER}/x",
                f"/home/{BUILD_USER}",
                f"/home/{BUILD_USER} is still there",
            ),
        }
        for name, (planted, remove, line) in leftovers.items():
            with self.subTest(leftover=name):
                self.vm.host_file(planted, "left\n")
                try:
                    result = self.vm.run("--check")

                    self.assert_fails_once(result, "1 thing(s) are still on the machine")
                    self.assertEqual([f"[LEFT] {line}"], self.left_lines(result))
                finally:
                    target = self.vm.host_path(remove)
                    shutil.rmtree(target) if target.is_dir() else target.unlink()

    def test_a_machine_id_is_found(self) -> None:
        self.vm.host_file("/etc/machine-id", "0123456789abcdef0123456789abcdef\n")

        result = self.vm.run("--check")

        self.assert_fails_once(result)
        self.assertEqual(["[LEFT] a machine-id in /etc/machine-id"], self.left_lines(result))

    def test_the_build_user_is_found(self) -> None:
        self.vm.answer("passwd", f"{BUILD_USER}:x:1000:1000::/home/{BUILD_USER}:/bin/bash\n")

        result = self.vm.run("--check")

        self.assert_fails_once(result)
        self.assertEqual([f"[LEFT] the build user {BUILD_USER}"], self.left_lines(result))

    def test_rke2_that_starts_at_boot_is_found(self) -> None:
        self.vm.answer("rke2_enabled", "enabled\n")

        result = self.vm.run("--check")

        self.assert_fails_once(result)
        self.assertEqual(["[LEFT] rke2-server starts at boot"], self.left_lines(result))

    def test_a_core_secret_is_found_while_the_database_exists(self) -> None:
        self.vm.host_file(f"{RKE2}/server/db/etcd/db", "left\n")
        self.vm.answer("secrets", "meho-core-tls -\nother -\n")

        result = self.vm.run("--check")

        self.assert_fails_once(result)
        self.assertEqual(
            [
                "[LEFT] Secrets of the core: secret/meho-core-tls",
                f"[LEFT] {RKE2}/server/db is still there",
            ],
            self.left_lines(result),
        )

    def test_what_must_stay_is_checked_too(self) -> None:
        must_stay = {
            f"{RKE2}/agent/images": f"{RKE2}/agent/images is missing; it must stay",
            f"{RKE2}/server/manifests": f"{RKE2}/server/manifests is missing; it must stay",
            "/etc/rancher/rke2/meho-pss.yaml": "/etc/rancher/rke2/meho-pss.yaml is missing; it must stay",
            "/etc/rancher/rke2/config.yaml": "/etc/rancher/rke2/config.yaml is missing; it must stay",
            "/opt/meho/appliance/download/install.sh": "/opt/meho/appliance/download/install.sh is missing; it must stay",
            "/etc/cloud/cloud-init.disabled": "/etc/cloud/cloud-init.disabled is missing; it must stay",
        }
        for path, line in must_stay.items():
            with self.subTest(path=path):
                target = self.vm.host_path(path)
                moved = self.vm.root / "moved"
                target.rename(moved)
                try:
                    result = self.vm.run("--check")

                    self.assert_fails_once(result)
                    self.assertEqual([f"[LEFT] {line}"], self.left_lines(result))
                finally:
                    moved.rename(target)

    def test_all_leftovers_are_listed_in_one_run(self) -> None:
        self.vm.host_file("/etc/ssh/ssh_host_rsa_key", "left\n")
        self.vm.host_file("/etc/rancher/node/password", "left\n")

        result = self.vm.run("--check")

        self.assert_fails_once(result, "2 thing(s) are still on the machine")
        self.assertEqual(2, len(self.left_lines(result)))


if __name__ == "__main__":
    unittest.main()
