#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of the target networks on form 2 (#6828), on the side of
install.sh: --target-networks goes to the install record and, as
config.targetSsrfAllowlist, to values-place.yaml; the update writes the
same list again; the chart puts it into MEHO_TARGET_SSRF_ALLOWLIST of the
ConfigMap meho-config. The rule of the list is lib/targets.sh, which
meho-host uses too (test_meho_host_target_networks.py)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import unittest

from harness import HOST_LOCK, LINUX_SERVER, LOCK_HELD_LINE, InstallTestCase, held_lock
from test_core_install import CHART_DIR, INSTALL_RECORD, PLACE_VALUES, place_values
from update_harness import INSTALLER, NEW_VERSION, run_update_lib

LIST = "10.20.0.0/16,10.30.0.5:443,10.40.0.0/24"
NO_LIST_LINE = (
    "  Target networks:       none, so MEHO accepts no target on a private address. Name the "
    "networks of the systems it manages with: sudo meho-host target-networks <list>"
)


def target_block(target_list: str = LIST) -> str:
    """The last block of values-place.yaml with a list."""
    return f'config:\n  targetSsrfAllowlist: "{target_list}"\n'


def parse(target_list: str, pod_range: str = "10.42.0.0/16") -> tuple[bool, str]:
    """parse_target_networks of lib/targets.sh with lib/net.sh, as install.sh
    sources them: (accepted, the list or the problem)."""
    script = (
        f'source "{LINUX_SERVER}/lib/net.sh"\nsource "{LINUX_SERVER}/lib/targets.sh"\n'
        'if parse_target_networks "$1" "$2" 10.43.0.0/16; then printf "%s" "$TARGET_NETWORKS"; '
        'else printf "%s" "$TARGET_NETWORKS_PROBLEM"; exit 1; fi\n'
    )
    result = subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\n" + script, "parse", target_list, pod_range],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode not in (0, 1) or result.stderr:
        raise AssertionError(result.stderr)
    return result.returncode == 0, result.stdout


class RuleTests(unittest.TestCase):
    """The rule of lib/targets.sh."""

    def test_good_lists_are_kept_in_lower_case_without_spaces(self) -> None:
        cases = {
            "10.20.0.0/16": "10.20.0.0/16",
            "10.20.0.0/16, 10.30.0.5:443 ,10.40.0.0/24": LIST,
            "10.30.0.6:8443,10.30.0.7:65535,10.30.0.8:1": "10.30.0.6:8443,10.30.0.7:65535,10.30.0.8:1",
            "10.20.0.0/16,,": "10.20.0.0/16",
            "none": "",
            "NONE": "",
        }
        for given, kept in cases.items():
            with self.subTest(given=given):
                self.assertEqual((True, kept), parse(given))

    def test_a_wrong_entry_is_refused_with_its_problem(self) -> None:
        cases = {
            "10.20.0.7/24": "10.20.0.7/24 does not start at the first address of its range.",
            "10.20.0.0/33": "10.20.0.0/33 is not an IPv4 range such as 10.20.0.0/16.",
            "10.20.0.0/16:443": "10.20.0.0/16:443 has a port, which only one address",
            "10.1.2.300": "10.1.2.300 is not an IPv4 address such as 10.20.0.5.",
            "10.30.0.5:0": "10.30.0.5:0 has a port that is not a number from 1 to 65535.",
            "10.30.0.5:65536": "10.30.0.5:65536 has a port that is not a number from 1 to 65535.",
            "10.30.0.5:08080": "10.30.0.5:08080 has a port that is not a number from 1 to 65535.",
            "fd00::/64": "fd00::/64 is not an IPv4 entry.",
            "[fd00::5]:443": "[fd00::5]:443 is not an IPv4 entry.",
            "v_center": "v_center is not an IPv4 range or an IPv4 address.",
            "-vcenter": "-vcenter is not an IPv4 range or an IPv4 address.",
            "10.20.0.0/16 10.30.0.0/16": "'10.20.0.0/16 10.30.0.0/16' holds a space. Put a comma",
            "none,10.20.0.0/16": "none stands alone: it empties the list.",
            ",": "the list is empty.",
            "10.20.0.0/16,\n10.30.0.0/16": "the list holds a line break. Give it on one line",
            "10.20.0.0/16\r": "the list holds a line break.",
            "none\n": "the list holds a line break.",
        }
        for given, problem in cases.items():
            with self.subTest(given=given):
                accepted, text = parse(given)
                self.assertFalse(accepted, text)
                self.assertIn(problem, text)

    def test_no_entry_opens_meho_itself(self) -> None:
        others = "Name only the networks of the systems that MEHO manages"
        cases = {
            "10.0.0.0/8": "overlaps 10.42.0.0/16, the pod range of the built-in Kubernetes.",
            "0.0.0.0/0": "overlaps 10.42.0.0/16, the pod range of the built-in Kubernetes.",
            "10.43.0.1": "overlaps 10.43.0.0/16, the service range of the built-in Kubernetes.",
            "127.0.0.1:8000": "overlaps 127.0.0.0/8, the addresses of the MEHO pod itself.",
            "0.0.0.0": "overlaps 0.0.0.0/8, the addresses of the MEHO pod itself.",
            "169.254.169.254:80": "overlaps 169.254.0.0/16, the link-local range.",
        }
        for given, problem in cases.items():
            with self.subTest(given=given):
                accepted, text = parse(given)
                self.assertFalse(accepted, text)
                self.assertIn(problem, text)
                self.assertIn(others, text)

    def test_a_host_name_is_refused(self) -> None:
        # The guard would trust a listed name without checking its address,
        # and inside the meho pod a short name can point to MEHO's own
        # services. So the list takes no name at all.
        for given in (
            "vcenter.example.com",
            "VCenter.Example.com:443",
            "nsx",
            "localhost",
            "kubernetes.default",
            "meho-postgres",
            "meho-vault.meho.svc.cluster.local",
            "10.20.0.0/16,vcenter.example.com",
        ):
            with self.subTest(given=given):
                accepted, text = parse(given)
                self.assertFalse(accepted, text)
                self.assertIn(" is a host name. ", text)
                self.assertIn("the list takes only ranges and addresses", text)
                self.assertIn("for example 10.20.0.0/16", text)

    def test_the_pod_range_of_the_install_is_the_one_checked(self) -> None:
        self.assertEqual((True, "10.42.0.0/16"), parse("10.42.0.0/16", pod_range="172.30.0.0/16"))
        accepted, text = parse("172.30.5.0/24", pod_range="172.30.0.0/16")
        self.assertFalse(accepted)
        self.assertIn("overlaps 172.30.0.0/16, the pod range", text)


class InstallTargetNetworksTests(InstallTestCase):
    def record_options(self) -> dict[str, object]:
        record = json.loads(self.machine.host_path(INSTALL_RECORD).read_text())
        options: dict[str, object] = record["options"]
        return options

    def test_the_list_goes_to_the_record_and_the_values(self) -> None:
        result = self.machine.run(
            "--target-networks", "10.20.0.0/16, 10.30.0.5:443,10.40.0.0/24"
        )

        self.assert_succeeds(result)
        self.assertEqual(LIST, self.record_options()["target_networks"])
        self.assertEqual(
            place_values() + target_block(), self.machine.host_path(PLACE_VALUES).read_text()
        )
        self.assertIn(f"  Target networks:       {LIST}\n", result.stdout)

    def test_without_the_option_the_list_is_empty(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertEqual("", self.record_options()["target_networks"])
        self.assertEqual(place_values(), self.machine.host_path(PLACE_VALUES).read_text())
        self.assertIn(NO_LIST_LINE, result.stdout)

    def test_none_is_the_empty_list(self) -> None:
        self.assert_succeeds(self.machine.run("--target-networks=none"))

        self.assertEqual("", self.record_options()["target_networks"])
        self.assertEqual(place_values(), self.machine.host_path(PLACE_VALUES).read_text())

    def test_a_wrong_entry_stops_before_any_change(self) -> None:
        cases = {
            "10.20.0.7/24": "--target-networks: 10.20.0.7/24 does not start at the first address",
            "10.0.0.0/8": "--target-networks: 10.0.0.0/8 overlaps 10.42.0.0/16, the pod range",
            "10.20.0.0/16,\n10.30.0.0/16": "--target-networks: the list holds a line break.",
            "": "--target-networks needs a value.",
        }
        for value, message in cases.items():
            with self.subTest(value=value):
                result = self.machine.run("--target-networks", value)

                self.assert_usage_error(result, message)
                self.assertEqual([], self.written_files())
                self.assertEqual("", self.machine.calls())

    def test_the_list_is_checked_against_the_ranges_of_the_install(self) -> None:
        result = self.machine.run(
            "--cluster-cidr", "172.30.0.0/16", "--target-networks", "172.30.8.0/24"
        )

        self.assert_usage_error(result, "172.30.8.0/24 overlaps 172.30.0.0/16, the pod range")
        self.assertEqual([], self.written_files())


class FromDiskTargetNetworksTests(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.machine.download / "rke2")
        shutil.rmtree(self.machine.download / "images")
        self.machine.install_rke2_files()

    def test_from_disk_writes_the_list_it_is_given(self) -> None:
        self.assert_succeeds(self.machine.run("--from-disk", "--target-networks", LIST))

        values = self.machine.host_path(PLACE_VALUES).read_text()
        self.assertTrue(values.endswith(target_block()), values)
        record = json.loads(self.machine.host_path(INSTALL_RECORD).read_text())
        self.assertEqual(LIST, record["options"]["target_networks"])

    def test_a_held_lock_stops_from_disk_before_any_change(self) -> None:
        with held_lock(self.machine.host_path(HOST_LOCK)):
            result = self.machine.run("--from-disk", "--target-networks", LIST)

        line = self.assert_fails_once(result)
        self.assertEqual(LOCK_HELD_LINE.format(command="install.sh"), line)
        self.assertEqual([], self.written_files())


class HostLockTests(InstallTestCase):
    """install.sh holds the lock of lib/common.sh for its whole run."""

    def test_a_held_lock_stops_a_first_install_before_any_change(self) -> None:
        with held_lock(self.machine.host_path(HOST_LOCK)):
            result = self.machine.run("--target-networks", LIST)

        line = self.assert_fails_once(result)
        self.assertEqual(LOCK_HELD_LINE.format(command="install.sh"), line)
        self.assertEqual([], self.written_files())
        self.assertNotIn("helm", self.machine.calls())

    def run_with_umask_022(self) -> None:
        old = os.umask(0o022)
        try:
            self.assert_succeeds(self.machine.run("--target-networks", LIST))
        finally:
            os.umask(old)

    def assert_only_root_may_open_the_lock(self) -> None:
        # flock takes the lock on a file that is open only for reading, so
        # another account must not be able to open the folder or the file.
        lock = self.machine.host_path(HOST_LOCK)
        self.assertEqual(0o700, stat.S_IMODE(lock.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(lock.stat().st_mode))

    def test_only_root_may_open_a_new_lock(self) -> None:
        self.run_with_umask_022()

        self.assert_only_root_may_open_the_lock()

    def test_an_old_lock_that_others_may_read_is_closed(self) -> None:
        lock = self.machine.host_path(HOST_LOCK)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.parent.chmod(0o755)
        lock.write_text("")
        lock.chmod(0o644)

        self.run_with_umask_022()

        self.assert_only_root_may_open_the_lock()

    def test_the_lock_ends_with_the_run(self) -> None:
        self.assert_succeeds(self.machine.run("--target-networks", LIST))

        # The run ended, so the lock is free again.
        with held_lock(self.machine.host_path(HOST_LOCK)):
            pass


class UpdateTargetNetworksTests(InstallTestCase):
    """An install with a list, updated to MEHO 0.37.0."""

    def setUp(self) -> None:
        super().setUp()
        self.assert_succeeds(self.machine.run("--target-networks", LIST))
        self.machine.mark_installed()
        self.machine.set_download_version(NEW_VERSION)
        self.machine.answer("calls.log", "")

    def options(self) -> dict[str, object]:
        record = json.loads(self.machine.host_path(INSTALL_RECORD).read_text())
        options: dict[str, object] = record["options"]
        return options

    def test_the_update_keeps_the_list(self) -> None:
        values = self.machine.host_path(PLACE_VALUES)
        values.write_text(place_values())

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        self.assertEqual(place_values() + target_block(), values.read_text())
        self.assertEqual(LIST, self.options()["target_networks"])

    def test_a_record_of_an_older_install_sh_gives_the_empty_list(self) -> None:
        record = self.machine.host_path(INSTALL_RECORD)
        record.write_text(record.read_text().replace(f'    "target_networks": "{LIST}",\n', ""))

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        self.assertEqual(place_values(), self.machine.host_path(PLACE_VALUES).read_text())
        self.assertEqual("", self.options()["target_networks"])

    def test_the_update_refuses_the_option_and_names_the_command(self) -> None:
        line = self.assert_fails_once(
            self.machine.run("--confirm-backup", "--target-networks", "10.50.0.0/16"),
            "only for a first install: --target-networks.",
            "To change the target networks, run 'meho-host target-networks <list>'.",
        )

        self.assertIn("To change the SSH range", line)
        self.assertNotIn(INSTALLER, self.machine.calls())
        self.assertEqual(LIST, self.options()["target_networks"])

    def test_a_held_lock_stops_the_update_before_it_reads_the_record(self) -> None:
        # A list that breaks the rule would stop the update with a message
        # about the record. The lock comes first, so the update never reads it.
        record = self.machine.host_path(INSTALL_RECORD)
        record.write_text(record.read_text().replace(LIST, "10.0.0.0/8"))
        before = (record.read_text(), self.machine.host_path(PLACE_VALUES).read_text())

        with held_lock(self.machine.host_path(HOST_LOCK)):
            result = self.machine.run("--confirm-backup")

        line = self.assert_fails_once(result)
        self.assertEqual(LOCK_HELD_LINE.format(command="install.sh"), line)
        self.assertNotIn("Update step", result.stdout)
        self.assertEqual(
            before, (record.read_text(), self.machine.host_path(PLACE_VALUES).read_text())
        )
        self.assertNotIn("helm", self.machine.calls())

    def test_meho_host_cannot_change_the_list_while_the_update_runs(self) -> None:
        # While Helm updates the core, the admin empties the list with
        # meho-host. The update read the list at its start and writes it
        # again at its end. So meho-host must stop and say that it changed
        # nothing, instead of a success that the update then undoes.
        helm = self.machine.download / "bin" / "helm"
        stub = helm.with_name("helm-stub")
        helm.rename(stub)
        helm.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = upgrade ]; then\n'
            f'    "{self.machine.download}/meho-host" target-networks none '
            '> "$MEHO_FAKE/meho-host.out" 2>&1\n'
            '    echo "exit code $?" >> "$MEHO_FAKE/meho-host.out"\n'
            "fi\n"
            f'exec "{stub}" "$@"\n'
        )
        helm.chmod(0o755)

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        self.assertEqual(
            LOCK_HELD_LINE.format(command="the command") + "\nexit code 1\n",
            (self.machine.fake / "meho-host.out").read_text(),
        )
        self.assertEqual(LIST, self.options()["target_networks"])
        self.assertEqual(
            place_values() + target_block(), self.machine.host_path(PLACE_VALUES).read_text()
        )

    def test_a_list_changed_by_hand_stops_the_update(self) -> None:
        record = self.machine.host_path(INSTALL_RECORD)
        record.write_text(record.read_text().replace(LIST, "10.0.0.0/8"))

        self.assert_fails_once(
            self.machine.run("--confirm-backup"),
            "has values that install.sh cannot use: target_networks.",
            "run 'meho-host support-bundle'",
        )
        self.assertNotIn(INSTALLER, self.machine.calls())


class RecordReadTests(InstallTestCase):
    def test_read_install_record_reads_the_list(self) -> None:
        self.assert_succeeds(self.machine.run("--target-networks", LIST))

        result = run_update_lib(self.machine, "read_install_record\nrecord_value target_networks\n")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(LIST, result.stdout)


@unittest.skipUnless(
    shutil.which("helm") or os.environ.get("MEHO_REQUIRE_HELM") == "1",
    "helm is not on PATH; set MEHO_REQUIRE_HELM=1 to fail instead",
)
class TargetNetworksChartRenderTests(InstallTestCase):
    def meho_config(self, *args: str) -> str:
        """The ConfigMap meho-config, from helm template with the two values
        files of install_core."""
        self.assert_succeeds(self.machine.run(*args))
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
        configmaps = [
            document
            for document in result.stdout.split("\n---\n")
            if "kind: ConfigMap\n" in document and "\n  name: meho-config\n" in document
        ]
        self.assertEqual(1, len(configmaps), result.stdout)
        return configmaps[0]

    def test_the_list_reaches_the_configmap_of_meho(self) -> None:
        config = self.meho_config("--target-networks", LIST)

        self.assertIn(f'  MEHO_TARGET_SSRF_ALLOWLIST: "{LIST}"\n', config)

    def test_without_a_list_the_guard_stays_fully_on(self) -> None:
        config = self.meho_config()

        self.assertIn('  MEHO_TARGET_SSRF_ALLOWLIST: ""\n', config)


if __name__ == "__main__":
    unittest.main()
