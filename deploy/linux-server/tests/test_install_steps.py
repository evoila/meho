#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of steps 2 and 3 of install.sh: prepare the machine, then install,
start and wait for RKE2."""

from __future__ import annotations

import stat
import textwrap
import unittest

from harness import LINUX_SERVER, InstallTestCase

RKE2_START = "systemctl enable --now --no-block rke2-server.service"


class PrepareTests(InstallTestCase):
    def test_meho_folders_are_for_root_only(self) -> None:
        self.assert_succeeds(self.machine.run())

        for folder in ("/var/lib/meho", "/etc/meho"):
            with self.subTest(folder=folder):
                path = self.machine.host_path(folder)
                self.assertEqual(0o700, stat.S_IMODE(path.stat().st_mode))

    def test_meho_image_archive_goes_where_rke2_imports_images(self) -> None:
        self.assert_succeeds(self.machine.run())

        copied = self.machine.host_path(
            "/var/lib/rancher/rke2/agent/images/meho-images.linux-amd64.tar.zst"
        )
        self.assertEqual("fake MEHO images\n", copied.read_text())

    def test_storage_manifest_goes_where_rke2_applies_manifests(self) -> None:
        self.assert_succeeds(self.machine.run())

        copied = self.machine.host_path(
            "/var/lib/rancher/rke2/server/manifests/local-path-storage.yaml"
        )
        source = LINUX_SERVER / "storage" / "local-path-storage.yaml"
        self.assertEqual(source.read_text(), copied.read_text())


class RKE2StartTests(InstallTestCase):
    def test_rke2_installs_offline_from_the_download_and_then_starts(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        calls = self.machine.calls().splitlines()
        installer = f"rke2-installer INSTALL_RKE2_ARTIFACT_PATH={self.machine.download}/rke2"
        self.assertIn(installer, calls)
        self.assertIn(RKE2_START, calls)
        self.assertLess(calls.index(installer), calls.index(RKE2_START))
        for line in (
            "[ok]   the node is Ready",
            "[ok]   local-path is the default storage class",
            "[ok]   the storage provisioner runs",
        ):
            with self.subTest(line=line):
                self.assertIn(line, result.stdout)
        self.assertIn(
            "RKE2 and its default storage class are ready on meho.example.com.", result.stdout
        )

    def test_the_script_waits_until_the_node_is_ready(self) -> None:
        counter = self.machine.fake / "node_queries"
        self.machine.answer("kube_nodes", "")
        kubectl = self.machine.host_path("/var/lib/rancher/rke2/bin/kubectl")
        kubectl.write_text(
            textwrap.dedent(f"""\
            #!/bin/sh
            case "$*" in
              *"get nodes"*)
                echo x >> {counter}
                if [ "$(grep -c x {counter})" -ge 4 ]; then echo True; else echo False; fi ;;
              *"get storageclass"*) echo true ;;
              *"get deployment"*) echo 1 ;;
            esac
            """)
        )

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertEqual(3, self.machine.calls().count("sleep 5"))

    def test_a_failed_rke2_installer_stops_before_the_start(self) -> None:
        self.machine.answer("rke2_installer_rc", "1")

        result = self.machine.run()

        self.assert_fails_once(result, "RKE2's installer stopped with an error.")
        self.assertNotIn(RKE2_START, self.machine.calls())

    def test_a_failed_start_stops_with_the_log_command(self) -> None:
        self.machine.answer("systemctl_enable_rc", "1")

        self.assert_fails_once(
            self.machine.run(), "could not start rke2-server", "journalctl -u rke2-server"
        )

    def test_a_timeout_names_what_is_missing_and_the_log_command(self) -> None:
        cases = {
            "the node is Ready": ("kube_nodes", "False"),
            "local-path is the default storage class": ("kube_storageclass", ""),
            "the storage provisioner runs": ("kube_deployment", "0"),
        }
        for missing, (answer, value) in cases.items():
            with self.subTest(missing=missing):
                self.machine.answer(answer, value)

                line = self.assert_fails_once(
                    self.machine.run(), "not ready after 15 minutes", f"{missing} did not happen."
                )

                self.assertTrue(
                    line.endswith("Fix: see RKE2's log: journalctl -u rke2-server -e"), line
                )
                self.machine.answer(
                    answer,
                    {"kube_nodes": "True", "kube_storageclass": "true", "kube_deployment": "1"}[
                        answer
                    ],
                )

    def test_an_unexpected_error_prints_one_fail_line(self) -> None:
        self.machine.host_path("/proc/meminfo").unlink()

        self.assert_fails_once(self.machine.run(), "install.sh stopped at checks.sh line")

    def test_the_time_limit_is_15_minutes_of_5_second_waits(self) -> None:
        self.machine.answer("kube_nodes", "False")

        self.assert_fails_once(self.machine.run(), "not ready after 15 minutes")

        self.assertEqual(180, self.machine.calls().count("sleep 5"))


if __name__ == "__main__":
    unittest.main()
