#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of meho-host target-networks (#6828): it checks the list with the
rule of lib/targets.sh, writes it to the install record and to
values-place.yaml, sets MEHO_TARGET_SSRF_ALLOWLIST in the ConfigMap
meho-config, and restarts the meho Deployment. The install side is in
test_target_networks.py."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import unittest
from typing import Any

from cluster_harness import ClusterTestCase
from harness import TENANT_ID
from test_core_install import place_values
from test_target_networks import LIST, target_block

RECORD_PATH = "/etc/meho/install.json"
VALUES_PATH = "/etc/meho/values-place.yaml"
CONFIGMAP = "meho-config"
KEY = "MEHO_TARGET_SSRF_ALLOWLIST"
SUPPORT_HINT = "run 'meho-host support-bundle' and send the file to support."


def install_record(target_line: str | None = '    "target_networks": "",\n') -> str:
    """The install record as install.sh writes it. target_line None is the
    record of an older install.sh, without the key."""
    return (
        "{\n"
        '  "meho_version": "0.36.0",\n'
        '  "chart_version": "0.36.0",\n'
        '  "rke2_version": "v1.37.1+rke2r1",\n'
        '  "installed_at": "2026-11-20T08:00:00Z",\n'
        '  "options": {\n'
        '    "hostname": "meho.example.com",\n'
        '    "public_address": "",\n'
        f'    "first_tenant_id": "{TENANT_ID}",\n'
        '    "admin_user": "admin",\n'
        '    "cluster_cidr": "10.42.0.0/16",\n'
        '    "service_cidr": "10.43.0.0/16",\n'
        '    "join_address": "",\n'
        f"{target_line or ''}"
        '    "from_disk": false\n'
        "  }\n"
        "}\n"
    )


def with_list(target_list: str) -> str:
    return install_record(f'    "target_networks": "{target_list}",\n')


class TargetNetworksTestCase(ClusterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.host.host_file(RECORD_PATH, install_record())
        self.host.host_file(VALUES_PATH, place_values())
        for path in (RECORD_PATH, VALUES_PATH):
            self.host.host_path(path).chmod(0o600)
        state = self.host.state()
        state["configmaps"][CONFIGMAP] = {KEY: "", "LOG_LEVEL": "INFO"}
        self.host.save(state)

    def run_command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.host.run("target-networks", *args)

    def text(self, path: str) -> str | None:
        """The text of a file on the machine, or None when it does not exist."""
        file = self.host.host_path(path)
        return file.read_text() if file.exists() else None

    def files(self) -> dict[str, Any]:
        """The two files, the names in /etc/meho and the ConfigMap."""
        return {
            "record": self.text(RECORD_PATH),
            "values": self.text(VALUES_PATH),
            "names": sorted(os.listdir(self.host.host_path("/etc/meho"))),
            "configmap": self.host.configmap(CONFIGMAP),
        }

    def assert_nothing_changed(self, before: dict[str, Any]) -> None:
        self.assertEqual(before, self.files())
        self.assertEqual([], self.kubectl_calls(" patch "))
        self.assertEqual(0, self.host.deployment_restarts())


class TargetNetworksTests(TargetNetworksTestCase):
    def test_the_list_goes_to_the_files_and_to_meho_and_meho_restarts(self) -> None:
        result = self.run_command("10.20.0.0/16,VCenter.Example.com:443,10.30.0.5")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual("", result.stderr)
        self.assertEqual(with_list(LIST), self.text(RECORD_PATH))
        record = json.loads(self.host.host_path(RECORD_PATH).read_text())
        self.assertEqual(LIST, record["options"]["target_networks"])
        self.assertEqual(place_values() + target_block(), self.text(VALUES_PATH))
        for path in (RECORD_PATH, VALUES_PATH):
            with self.subTest(path=path):
                mode = stat.S_IMODE(self.host.host_path(path).stat().st_mode)
                self.assertEqual(0o600, mode)
        self.assertEqual({KEY: LIST, "LOG_LEVEL": "INFO"}, self.host.configmap(CONFIGMAP))
        self.assertEqual(1, self.host.deployment_restarts())
        calls = self.host.calls().splitlines()
        patch = next(i for i, line in enumerate(calls) if " patch configmap meho-config " in line)
        restart = next(
            i for i, line in enumerate(calls) if " rollout restart deployment/meho" in line
        )
        self.assertLess(patch, restart)
        self.assertEqual(
            "[ok]   wrote the target networks to /etc/meho/install.json and "
            "/etc/meho/values-place.yaml\n"
            "[ok]   MEHO restarts and then accepts a target on a private address only inside: "
            f"{LIST}. The console is away for about a minute.\n",
            result.stdout,
        )

    def test_a_new_list_replaces_the_old_one(self) -> None:
        self.assertEqual(0, self.run_command(LIST).returncode)

        result = self.run_command("10.50.0.0/16")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(with_list("10.50.0.0/16"), self.text(RECORD_PATH))
        self.assertEqual(place_values() + target_block("10.50.0.0/16"), self.text(VALUES_PATH))
        self.assertEqual({KEY: "10.50.0.0/16", "LOG_LEVEL": "INFO"}, self.host.configmap(CONFIGMAP))
        self.assertEqual(2, self.host.deployment_restarts())

    def test_none_empties_the_list(self) -> None:
        self.assertEqual(0, self.run_command(LIST).returncode)

        result = self.run_command("none")

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(install_record(), self.text(RECORD_PATH))
        self.assertEqual(place_values(), self.text(VALUES_PATH))
        self.assertEqual({KEY: "", "LOG_LEVEL": "INFO"}, self.host.configmap(CONFIGMAP))
        self.assertEqual(2, self.host.deployment_restarts())
        self.assertIn(
            "[ok]   MEHO restarts and then accepts no target on a private address. The console "
            "is away for about a minute.\n",
            result.stdout,
        )

    def test_a_record_of_an_older_install_sh_gets_the_key(self) -> None:
        self.host.host_file(RECORD_PATH, install_record(target_line=None))

        result = self.run_command(LIST)

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(with_list(LIST), self.text(RECORD_PATH))

    def test_the_help_names_the_command(self) -> None:
        result = self.host.run("--help")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("  target-networks <list>\n  target-networks none\n", result.stdout)
        self.assertIn("sudo meho-host target-networks 10.20.0.0/16,", result.stdout)


class TargetNetworksFailureTests(TargetNetworksTestCase):
    def test_a_wrong_entry_changes_nothing(self) -> None:
        cases = {
            "10.0.0.0/8": "target-networks: 10.0.0.0/8 overlaps 10.42.0.0/16, the pod range",
            "10.20.0.7/24": "target-networks: 10.20.0.7/24 does not start at the first address",
            "10.20.0.0/16:443": "target-networks: 10.20.0.0/16:443 has a port",
            "localhost": "target-networks: localhost is a name of the MEHO pod itself",
        }
        before = self.files()
        for value, message in cases.items():
            with self.subTest(value=value):
                self.assert_usage_error(self.run_command(value), message)
                self.assert_nothing_changed(before)

    def test_a_list_with_a_line_break_keeps_the_list_in_force(self) -> None:
        self.assertEqual(0, self.run_command(LIST).returncode)
        before = self.files()

        result = self.run_command("10.20.0.0/16,\n10.30.0.0/16")

        self.assert_usage_error(
            result, "target-networks: the list holds a line break. Give it on one line"
        )
        self.assertEqual(before, self.files())
        self.assertEqual({KEY: LIST, "LOG_LEVEL": "INFO"}, self.host.configmap(CONFIGMAP))
        self.assertEqual(1, self.host.deployment_restarts())

    def test_wrong_usage(self) -> None:
        for args in ((), ("10.20.0.0/16,", "10.30.0.0/16")):
            with self.subTest(args=args):
                self.assert_usage_error(
                    self.run_command(*args), "target-networks takes one value: a list with commas"
                )

    def test_not_root_changes_nothing(self) -> None:
        self.host.answer("uid", "1000")
        before = self.files()

        result = self.run_command(LIST)

        self.assertEqual(1, result.returncode)
        self.assertIn("[FAIL] meho-host does not run as root.", result.stderr)
        self.assert_nothing_changed(before)

    def test_a_failed_patch_warns_and_meho_keeps_the_old_list(self) -> None:
        self.host.fail("patch")

        result = self.run_command(LIST)

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            "[WARN] the ConfigMap meho-config could not be changed, so MEHO still uses the old "
            "target networks. Run the command again.\n",
            result.stderr,
        )
        self.assertEqual(with_list(LIST), self.text(RECORD_PATH))
        self.assertEqual({KEY: "", "LOG_LEVEL": "INFO"}, self.host.configmap(CONFIGMAP))
        self.assertEqual(0, self.host.deployment_restarts())
        self.assertNotIn("MEHO restarts", result.stdout)

    def test_a_failed_restart_warns(self) -> None:
        self.host.fail("restart")

        result = self.run_command(LIST)

        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual(
            "[WARN] MEHO could not be restarted, so it uses the new target networks only after "
            "its next restart. Run the command again.\n",
            result.stderr,
        )
        self.assertEqual({KEY: LIST, "LOG_LEVEL": "INFO"}, self.host.configmap(CONFIGMAP))

    def test_a_record_in_another_form_changes_nothing(self) -> None:
        record = install_record(target_line=None).replace('    "from_disk": false\n', "")
        self.host.host_file(
            RECORD_PATH, record.replace('"join_address": "",', '"join_address": ""')
        )
        before = self.files()

        result = self.run_command(LIST)

        self.assert_one_fail_line(
            result,
            "[FAIL] /etc/meho/install.json is not in the form install.sh writes, so meho-host "
            f"cannot change it. Nothing was changed. Fix: {SUPPORT_HINT}",
        )
        self.assert_nothing_changed(before)

    def test_a_record_without_the_ranges_changes_nothing(self) -> None:
        self.host.host_file(RECORD_PATH, install_record().replace("10.43.0.0/16", ""))
        before = self.files()

        result = self.run_command(LIST)

        self.assert_one_fail_line(
            result,
            "[FAIL] /etc/meho/install.json names no pod range and service range, so meho-host "
            f"cannot check the list. Nothing was changed. Fix: {SUPPORT_HINT}",
        )
        self.assert_nothing_changed(before)

    def test_values_with_a_config_block_of_their_own_change_nothing(self) -> None:
        blocks = {
            "another key": 'config:\n  logLevel: "DEBUG"\n',
            "an empty line in the block": 'config:\n\n  logLevel: "DEBUG"\n',
            "an empty line after the list": target_block() + "\n",
            "a comment in the block": f'config:\n# set by hand\n  targetSsrfAllowlist: "{LIST}"\n',
            "a value on the heading": "config: {}\n",
        }
        for name, block in blocks.items():
            with self.subTest(block=name):
                self.host.host_file(VALUES_PATH, place_values() + block)
                before = self.files()

                result = self.run_command(LIST)

                self.assert_one_fail_line(
                    result,
                    "[FAIL] /etc/meho/values-place.yaml has a config block that install.sh did "
                    f"not write, so meho-host cannot change it. Nothing was changed. Fix: "
                    f"{SUPPORT_HINT}",
                )
                self.assert_nothing_changed(before)

    def test_without_the_values_file_nothing_changes(self) -> None:
        self.host.host_path(VALUES_PATH).unlink()
        before = self.files()

        result = self.run_command(LIST)

        self.assert_one_fail_line(
            result,
            "[FAIL] /etc/meho/values-place.yaml does not exist, so meho-host cannot change it. "
            f"Nothing was changed. Fix: {SUPPORT_HINT}",
        )
        self.assert_nothing_changed(before)


if __name__ == "__main__":
    unittest.main()
