#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of the update (form-2 design, section 7): install.sh finds the
install, keeps its answers, updates RKE2 and the core, and leaves the
firewall, the one-time files, the data and the Secrets alone.

The backup question and the version step are in test_update_checks.py, the
OS packages of the appliance in test_os_packages.py.
"""

from __future__ import annotations

import json
import unittest

from harness import TENANT_ID, FakeMachine, InstallTestCase
from update_harness import (
    FIREWALL_FILES,
    HELM_UPGRADE,
    INSTALL_RECORD,
    INSTALLER,
    NEW_VERSION,
    ONE_TIME_FILES,
    PLACE_VALUES,
    RKE2_CONFIG,
    RKE2_RESTART,
    UPDATE_DATE,
    VOLUME_FILE,
    UpdateTestCase,
    sha256,
)

HINT = "run 'meho-host support-bundle' and send the file to support."


class FindInstallTests(InstallTestCase):
    def installed_machine(self) -> FakeMachine:
        machine = FakeMachine()
        self.addCleanup(machine.close)
        result = machine.run()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        machine.answer("calls.log", "")
        return machine

    def test_a_machine_with_no_sign_of_an_install_gets_a_first_install(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn(
            "[ok]   MEHO is not installed on this machine. This run installs MEHO 0.36.0.",
            result.stdout,
        )
        self.assertIn("systemctl is-active --quiet rke2-server.service", self.machine.calls())
        self.assertNotIn("helm list", self.machine.calls())
        self.assertNotIn("Update step", result.stdout)

    def test_only_some_signs_stop_and_change_nothing(self) -> None:
        def record_only(machine: FakeMachine) -> None:
            pass

        def no_record(machine: FakeMachine) -> None:
            machine.mark_installed()
            machine.remove_install_record()

        def rke2_only(machine: FakeMachine) -> None:
            no_record(machine)
            machine.answer("helm_releases", "")

        def no_release(machine: FakeMachine) -> None:
            machine.mark_installed()
            machine.answer("helm_releases", "")

        def api_down(machine: FakeMachine) -> None:
            machine.mark_installed()
            machine.answer("helm_list_rc", "1")

        cases = {
            "record only": (
                record_only,
                "The install record /etc/meho/install.json exists, but rke2-server does not run",
                "start RKE2 with 'systemctl start rke2-server'",
            ),
            "no record": (
                no_record,
                "rke2-server runs and the Helm release meho exists, but the install record"
                " /etc/meho/install.json is missing. install.sh changed nothing.",
                HINT,
            ),
            "RKE2 only": (
                rke2_only,
                "rke2-server runs, but the install record /etc/meho/install.json and the Helm"
                " release meho are missing.",
                HINT,
            ),
            "no release": (
                no_release,
                "rke2-server runs and the install record /etc/meho/install.json exists, but the"
                " Helm release meho is missing.",
                HINT,
            ),
            "API does not answer": (
                api_down,
                "rke2-server runs, but its Kubernetes API does not answer",
                "wait a few minutes until RKE2 is ready",
            ),
        }
        for case, (prepare, problem, fix) in cases.items():
            with self.subTest(case=case):
                machine = self.installed_machine()
                prepare(machine)
                before = {path: sha256(path) for path in machine.root.rglob("*") if path.is_file()}

                result = machine.run("--confirm-backup")

                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                fail_lines = [
                    line for line in result.stderr.splitlines() if line.startswith("[FAIL]")
                ]
                self.assertEqual(1, len(fail_lines), result.stderr)
                self.assertIn(problem, fail_lines[0])
                self.assertIn(f"Fix: {fix}", fail_lines[0])
                after = {path: sha256(path) for path in machine.root.rglob("*") if path.is_file()}
                self.assertEqual(before, after)
                for command in (INSTALLER, HELM_UPGRADE, "nft"):
                    self.assertNotIn(command, machine.calls())

    def test_from_disk_does_not_look_for_an_install(self) -> None:
        machine = self.installed_machine()

        result = machine.run("--from-disk")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("systemctl is-active --quiet rke2-server.service", machine.calls())
        self.assertNotIn("helm list", machine.calls())

    def test_update_options_stop_a_first_install(self) -> None:
        package_file = self.machine.home / "os-packages.tar"
        package_file.write_text("not used\n")
        for args in (["--confirm-backup"], ["--os-packages", str(package_file)]):
            with self.subTest(args=args):
                self.assert_fails_once(
                    self.machine.run(*args),
                    "MEHO is not installed on this machine, so there is nothing to update.",
                    "for a first install, leave them out.",
                )
                self.assertEqual([], self.written_files())

    def test_update_options_with_from_disk_are_wrong_usage(self) -> None:
        package_file = self.machine.home / "os-packages.tar"
        package_file.write_text("not used\n")
        for args in (["--confirm-backup"], ["--os-packages", str(package_file)]):
            with self.subTest(args=args):
                self.assert_usage_error(
                    self.machine.run("--from-disk", *args), "--from-disk installs the core again"
                )
                self.assertEqual("", self.machine.calls())

    def test_a_missing_os_package_file_is_wrong_usage(self) -> None:
        missing = str(self.machine.home / "missing.tar")

        self.assert_usage_error(
            self.machine.run("--os-packages", missing), f"--os-packages {missing} is not a"
        )
        self.assertEqual("", self.machine.calls())


class UpdateFlowTests(UpdateTestCase):
    def test_all_three_signs_make_an_update(self) -> None:
        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertIn(
            "[ok]   MEHO 0.36.0 is installed (2026-10-23). This run updates it to 0.37.0.",
            result.stdout,
        )
        self.assertIn(
            "install.sh: MEHO 0.37.0 is installed on meho.example.com. Before this run: MEHO"
            " 0.36.0.",
            result.stdout,
        )
        self.assertNotIn("== Step 1", result.stdout)

    def test_the_steps_run_in_order(self) -> None:
        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        headers = [line for line in result.stdout.splitlines() if line.startswith("== ")]
        self.assertEqual(
            [
                "== Find the install",
                "== Update step 1: check the machine and the versions",
                "== Update step 2: confirm a backup",
                "== Update step 3: update RKE2",
                "== Update step 4: update the MEHO core",
                "== Update step 5: install the meho command and meho-host, and write the"
                " install record",
            ],
            headers,
        )
        calls = self.machine.calls().splitlines()

        def index(prefix: str) -> int:
            return next(i for i, line in enumerate(calls) if line.startswith(prefix))

        self.assertLess(index(INSTALLER), index("systemctl show --property=InvocationID"))
        self.assertLess(index("systemctl show --property=InvocationID"), index(RKE2_RESTART))
        self.assertLess(index(RKE2_RESTART), index("kubectl"))
        self.assertLess(index("kubectl"), index(HELM_UPGRADE))
        helm = calls[index(HELM_UPGRADE)]
        self.assertIn(f"chart/meho-{NEW_VERSION}.tgz", helm)
        self.assertIn(f"--set image.tag=v{NEW_VERSION}", helm)
        self.assertIn(f"-f {self.machine.root}{PLACE_VALUES}", helm)

    def test_the_checks_an_installed_machine_fails_are_skipped(self) -> None:
        self.machine.answer("processes", "rke2\ncontainerd\nkubelet\n")
        self.machine.answer("ss_tcp", "LISTEN 0 4096 0.0.0.0:6443 0.0.0.0:*\n")
        self.machine.ssh_connection = None

        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        commands = {line.split(" ", 1)[0] for line in self.machine.calls().splitlines()}
        for command in ("pgrep", "ss", "nft"):
            with self.subTest(command=command):
                self.assertNotIn(command, commands)
        self.assertNotIn("ip -4 route show", self.machine.calls())
        for check in ("processors", "GiB of memory", "free for /var/lib", "clock", "resolves"):
            with self.subTest(check=check):
                self.assertIn(check, result.stdout)

    def test_the_install_record_shows_the_new_version(self) -> None:
        before = json.loads(self.host_text(INSTALL_RECORD))

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        record = json.loads(self.host_text(INSTALL_RECORD))
        self.assertEqual(NEW_VERSION, record["meho_version"])
        self.assertEqual(NEW_VERSION, record["chart_version"])
        self.assertEqual("v1.37.1+rke2r1", record["rke2_version"])
        self.assertEqual(UPDATE_DATE, record["installed_at"])
        self.assertEqual(before["options"], record["options"])

    def test_the_images_the_manifests_and_the_commands_are_replaced(self) -> None:
        self.machine.host_file("/var/lib/rancher/rke2/server/manifests/local-path-storage.yaml", "")
        download = self.machine.download

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        replaced = {
            "/var/lib/rancher/rke2/agent/images/meho-images.linux-amd64.tar.zst": (
                "images/meho-images.linux-amd64.tar.zst"
            ),
            "/var/lib/rancher/rke2/server/manifests/local-path-storage.yaml": (
                "storage/local-path-storage.yaml"
            ),
            "/usr/local/bin/meho": "bin/meho",
            "/usr/local/sbin/meho-host": "meho-host",
        }
        for host, source in replaced.items():
            with self.subTest(path=host):
                self.assertEqual((download / source).read_text(), self.host_text(host))
        self.assertIn(f"fake meho CLI {NEW_VERSION}", self.host_text("/usr/local/bin/meho"))

    def test_the_firewall_the_one_time_files_and_the_data_stay(self) -> None:
        kept = (*FIREWALL_FILES, *ONE_TIME_FILES, VOLUME_FILE)
        before = {path: sha256(self.machine.host_path(path)) for path in kept}

        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertEqual(before, {path: sha256(self.machine.host_path(path)) for path in kept})
        calls = self.machine.calls()
        for command in ("nft", "get secret", "meho-firewall", "firewalld", "ufw"):
            with self.subTest(command=command):
                self.assertNotIn(command, calls)

    def test_deleted_one_time_files_are_not_written_again(self) -> None:
        for path in ONE_TIME_FILES:
            self.machine.host_path(path).unlink()

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        for path in ONE_TIME_FILES:
            with self.subTest(path=path):
                self.assertFalse(self.machine.host_path(path).exists())

    def test_the_update_waits_for_the_new_run_of_rke2(self) -> None:
        self.machine.answer("rke2_restart_starts_a_new_run", "no")

        line = self.assert_fails_once(
            self.machine.run("--confirm-backup"),
            "not ready after 15 minutes: the new run of rke2-server is active did not happen.",
        )

        self.assertIn("journalctl -u rke2-server", line)
        self.assertNotIn("get nodes", self.machine.calls())
        self.assertNotIn(HELM_UPGRADE, self.machine.calls())

    def test_a_failed_restart_stops_before_the_core(self) -> None:
        self.machine.answer("systemctl_restart_rc", "1")

        self.assert_fails_once(
            self.machine.run("--confirm-backup"), "systemd could not restart rke2-server."
        )
        self.assertNotIn(HELM_UPGRADE, self.machine.calls())

    def test_a_failed_core_update_keeps_the_record_so_a_second_run_updates_again(self) -> None:
        self.machine.answer("helm_rc", "1")

        self.assert_fails_once(
            self.machine.run("--confirm-backup"), "Helm could not install the MEHO core."
        )
        self.assertEqual("0.36.0", json.loads(self.host_text(INSTALL_RECORD))["meho_version"])

        self.machine.answer("helm_rc", "0")
        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertIn("This run updates it to 0.37.0.", result.stdout)


class AnswersTests(InstallTestCase):
    """An install with its own answers, updated on a machine whose own name
    and random tenant id changed since."""

    def setUp(self) -> None:
        super().setUp()
        self.machine.answer("resolvectl_out", "meho-01.example.org: 10.0.0.5\n")
        self.assert_succeeds(
            self.machine.run(
                "--hostname=meho-01.example.org",
                "--first-tenant-id",
                TENANT_ID,
                "--admin-user=first.admin",
                "--cluster-cidr",
                "172.30.0.0/16",
                "--service-cidr",
                "172.31.0.0/20",
            )
        )
        self.machine.mark_installed()
        self.machine.set_download_version(NEW_VERSION)
        self.machine.answer("hostname_fqdn", "other-name.example.org")
        self.machine.host_file(
            "/proc/sys/kernel/random/uuid", "0e2b4d6f-8a1c-4e3d-9b5f-7a9c1e3b5d7f\n"
        )
        self.place_values = self.machine.host_path(PLACE_VALUES).read_text()
        self.rke2_config = self.machine.host_path(RKE2_CONFIG).read_text()
        self.machine.answer("calls.log", "")

    def record(self) -> dict[str, object]:
        loaded: dict[str, object] = json.loads(self.machine.host_path(INSTALL_RECORD).read_text())
        return loaded

    def test_the_update_keeps_the_answers_of_the_install(self) -> None:
        options = self.record()["options"]
        self.machine.host_path(PLACE_VALUES).write_text(self.place_values + "extra: by-hand\n")

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        self.assertEqual(self.place_values, self.machine.host_path(PLACE_VALUES).read_text())
        self.assertEqual(self.rke2_config, self.machine.host_path(RKE2_CONFIG).read_text())
        self.assertEqual(options, self.record()["options"])

    def test_install_options_stop_the_update(self) -> None:
        token_file = self.machine.home / "join-token"
        token_file.write_text("K10test-token\n")
        cases = [
            ["--hostname", "meho-01.example.org"],
            ["--public-address", "10.0.0.5"],
            ["--admin-network", "10.0.50.0/24"],
            ["--first-tenant-id", TENANT_ID],
            ["--admin-user", "first.admin"],
            ["--approver-user", "approver"],
            ["--cluster-cidr=172.30.0.0/16"],
            ["--service-cidr", "172.31.0.0/20"],
            ["--join-address", "10.0.0.20", "--join-token-file", str(token_file)],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.machine.answer("calls.log", "")

                line = self.assert_fails_once(
                    self.machine.run("--confirm-backup", *args),
                    "An update keeps the install's answers.",
                    "To change the SSH range, run 'meho-host admin-network <range>'.",
                )

                names = [arg.split("=")[0] for arg in args if arg.startswith("--")]
                self.assertIn(f"only for a first install: {', '.join(names)}.", line)
                self.assertNotIn(INSTALLER, self.machine.calls())
                place_values = self.machine.host_path(PLACE_VALUES).read_text()
                self.assertEqual(self.place_values, place_values)

    def test_the_join_address_of_the_record_stays_in_rke2s_settings(self) -> None:
        record = self.machine.host_path(INSTALL_RECORD)
        record.write_text(
            record.read_text().replace('"join_address": ""', '"join_address": "10.0.0.20"')
        )

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        config = self.machine.host_path(RKE2_CONFIG).read_text()
        self.assertIn('server: "https://10.0.0.20:9345"\n', config)
        self.assertIn('token-file: "/etc/meho/join-token"\n', config)
        self.assertEqual("10.0.0.20", self.options()["join_address"])

    def test_a_record_of_an_older_install_sh_without_join_address_works(self) -> None:
        record = self.machine.host_path(INSTALL_RECORD)
        record.write_text(record.read_text().replace('    "join_address": "",\n', ""))

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        self.assertNotIn("server:", self.machine.host_path(RKE2_CONFIG).read_text())
        self.assertEqual("", self.options()["join_address"])

    def test_a_record_changed_by_hand_stops_the_update(self) -> None:
        record = self.machine.host_path(INSTALL_RECORD)
        text = record.read_text()
        cases = {
            "no value for: hostname": text.replace("meho-01.example.org", ""),
            "cannot use: first_tenant_id": text.replace(TENANT_ID, "tenant-one"),
            "cannot use: cluster_cidr": text.replace("172.30.0.0/16", "172.30.0.0"),
        }
        for problem, changed in cases.items():
            with self.subTest(problem=problem):
                record.write_text(changed)

                self.assert_fails_once(self.machine.run("--confirm-backup"), problem, HINT)
                self.assertNotIn(INSTALLER, self.machine.calls())

    def options(self) -> dict[str, object]:
        options = self.record()["options"]
        assert isinstance(options, dict)
        return options


if __name__ == "__main__":
    unittest.main()
