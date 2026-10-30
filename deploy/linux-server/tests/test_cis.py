#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of RKE2's CIS profile on a form-2 machine (lib/cis.sh, #4636).

They cover:

- RKE2's settings, which turn the profile on and name the pod security file;
- the pod security file: RKE2's own file for the profile, with one more
  exempt namespace, local-path-storage, and no MEHO namespace;
- the host steps: the etcd user and group, and RKE2's kernel settings file,
  copied to /etc/sysctl.d and loaded, after RKE2's installer and before RKE2
  starts, wherever the installer put the file;
- a second run, which is the update and changes nothing;
- cis_state, which meho-host check (#4638) can call.

fixtures/rke2-pod-security-cis.yaml is restrictedPSAConfig of
pkg/rke2/psa.go, and fixtures/rke2-cis-sysctl.conf is
bundle/share/rke2/rke2-cis-sysctl.conf, both from the RKE2 version that
packaging/common.sh pins (v1.37.1+rke2r1). The CI job compares both with
the RKE2 source of that version.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

from harness import (
    FIXTURES,
    LINUX_SERVER,
    RKE2_CIS_SYSCTL,
    FakeMachine,
    InstallTestCase,
    kernel_path,
)

CONFIG = "/etc/rancher/rke2/config.yaml"
POD_SECURITY = "/etc/rancher/rke2/meho-pss.yaml"
SYSCTL_FILE = "/etc/sysctl.d/60-rke2-cis.conf"

RKE2_POD_SECURITY = (FIXTURES / "rke2-pod-security-cis.yaml").read_text()
FIXTURE_SHA256 = {
    "rke2-pod-security-cis.yaml": (
        "420cd34a5b29e928a913dd81f2ab7d72b4ef656543136047391b42410be47688"
    ),
    "rke2-cis-sysctl.conf": "519d9a01c916dd14eba302beb0d915d6a6fc2d96d87a2a6f3553cae29cebc085",
}
STORAGE_NAMESPACE = "local-path-storage"

INSTALLER = "rke2-installer "
USERADD = "useradd -r -c etcd user -s /sbin/nologin -M -U etcd"
SYSCTL_LOAD = "systemctl restart systemd-sysctl.service"
RKE2_START = "systemctl enable --now --no-block rke2-server.service"

LIBS = ("common", "cis")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def exempt_namespaces(pod_security: str) -> list[str]:
    """The list of exempt namespaces of a pod security file."""
    found = re.findall(r"^      namespaces: \[(.*)\]$", pod_security, re.MULTILINE)
    if len(found) != 1:
        raise AssertionError(f"one exempt namespace list expected, found {found}")
    return [name.strip() for name in found[0].split(",")]


def without_comments(text: str) -> str:
    return "".join(line for line in text.splitlines(True) if not line.startswith("#"))


def settings(sysctl_file: str) -> dict[str, str]:
    """The settings of a sysctl.d file as systemd-sysctl reads them."""
    found = {}
    for line in sysctl_file.splitlines():
        line = line.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip().lstrip("-")] = value.strip()
    return found


def run_lib(machine: FakeMachine, snippet: str) -> subprocess.CompletedProcess[str]:
    """Run bash with lib/common.sh and lib/cis.sh sourced, on the fake machine."""
    sources = "".join(f'source "{LINUX_SERVER}/lib/{name}.sh"\n' for name in LIBS)
    env = {
        "PATH": f"{machine.stubs}:{machine.tools}",
        "MEHO_TEST_ROOT": str(machine.root),
        "MEHO_FAKE": str(machine.fake),
        "LC_ALL": "C",
    }
    return subprocess.run(
        [str(machine.tools / "bash"), "-c", "set -euo pipefail\n" + sources + snippet],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )


class CisTestCase(InstallTestCase):
    def host_text(self, path: str) -> str:
        return self.machine.host_path(path).read_text()

    def kernel(self, key: str) -> str:
        return self.host_text(kernel_path(key)).strip()

    def call_index(self, prefix: str) -> int:
        """The index of the first call that starts with the prefix."""
        calls = self.machine.calls().splitlines()
        found = [index for index, line in enumerate(calls) if line.startswith(prefix)]
        if not found:
            self.fail(f"no call starts with {prefix!r}:\n" + "\n".join(calls))
        return found[0]

    def new_machine(self) -> FakeMachine:
        machine = FakeMachine()
        self.addCleanup(machine.close)
        return machine


class FixtureTests(unittest.TestCase):
    def test_the_fixtures_are_the_files_of_the_pinned_rke2_version(self) -> None:
        for name, expected in FIXTURE_SHA256.items():
            with self.subTest(fixture=name):
                digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
                self.assertEqual(expected, digest)

    def test_rke2s_own_file_exempts_three_namespaces_of_its_own(self) -> None:
        self.assertEqual(
            ["kube-system", "compliance-operator-system", "tigera-operator"],
            exempt_namespaces(RKE2_POD_SECURITY),
        )


class PodSecurityTests(CisTestCase):
    def test_rke2s_settings_turn_the_profile_on_and_name_the_pod_security_file(self) -> None:
        self.assert_succeeds(self.machine.run())

        config = self.host_text(CONFIG)
        self.assertIn("\nprofile: cis\n", config)
        self.assertIn(f'\npod-security-admission-config-file: "{POD_SECURITY}"\n', config)
        self.assertTrue(self.machine.host_path(POD_SECURITY).is_file())

    def test_the_file_is_rke2s_own_with_the_storage_namespace_exempt(self) -> None:
        self.assert_succeeds(self.machine.run())

        ours = self.host_text(POD_SECURITY)
        rke2_line = "tigera-operator]\n"
        expected = RKE2_POD_SECURITY.replace(rke2_line, f"tigera-operator, {STORAGE_NAMESPACE}]\n")
        self.assertNotEqual(RKE2_POD_SECURITY, expected)
        self.assertEqual(expected, without_comments(ours))
        self.assertTrue(ours.startswith("# Written by MEHO install.sh."), ours)
        self.assertEqual(0o600, mode(self.machine.host_path(POD_SECURITY)))

    def test_only_the_storage_namespace_is_added_and_no_meho_namespace(self) -> None:
        self.assert_succeeds(self.machine.run())

        ours = exempt_namespaces(self.host_text(POD_SECURITY))
        self.assertEqual([*exempt_namespaces(RKE2_POD_SECURITY), STORAGE_NAMESPACE], ours)
        self.assertNotIn("meho", ours)
        self.assertIn('restricted"', self.host_text(POD_SECURITY))

    def test_the_exempt_namespace_is_the_one_of_the_storage_manifest(self) -> None:
        manifest = (LINUX_SERVER / "storage" / "local-path-storage.yaml").read_text()

        namespaces = re.findall(r"^kind: Namespace\nmetadata:\n  name: (\S+)$", manifest, re.M)

        self.assertEqual([STORAGE_NAMESPACE], namespaces)
        self.assertEqual(
            {STORAGE_NAMESPACE}, set(re.findall(r"^  namespace: (\S+)$", manifest, re.M))
        )

    def test_from_disk_writes_the_settings_and_runs_the_host_steps(self) -> None:
        shutil.rmtree(self.machine.download / "rke2")
        shutil.rmtree(self.machine.download / "images")
        self.machine.install_rke2_files()

        result = self.machine.run("--from-disk")

        self.assert_succeeds(result)
        self.assertIn("\nprofile: cis\n", self.host_text(CONFIG))
        self.assertTrue(self.machine.host_path(POD_SECURITY).is_file())
        self.assertLess(self.call_index("useradd "), self.call_index(RKE2_START))
        self.assertLess(self.call_index(SYSCTL_LOAD), self.call_index(RKE2_START))


class HostStepTests(CisTestCase):
    def test_the_etcd_user_and_group_are_made_as_the_hardening_guide_says(self) -> None:
        self.assert_succeeds(self.machine.run())

        self.assertIn(USERADD, self.machine.calls().splitlines())
        self.assertRegex(self.host_text("/etc/passwd"), r"(?m)^etcd:")
        self.assertRegex(self.host_text("/etc/group"), r"(?m)^etcd:")

    def test_rke2s_kernel_settings_are_copied_and_loaded(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertEqual(RKE2_CIS_SYSCTL, self.host_text(SYSCTL_FILE))
        self.assertEqual(0o644, mode(self.machine.host_path(SYSCTL_FILE)))
        self.assertIn(SYSCTL_LOAD, self.machine.calls().splitlines())
        rke2_settings = settings(RKE2_CIS_SYSCTL)
        self.assertEqual(4, len(rke2_settings))
        for key, value in rke2_settings.items():
            with self.subTest(key=key):
                self.assertEqual(value, self.kernel(key))
        self.assertIn("[ok]   loaded RKE2's kernel settings for its CIS profile", result.stdout)

    def test_the_host_steps_run_after_the_installer_and_before_rke2_starts(self) -> None:
        self.assert_succeeds(self.machine.run())

        installer = self.call_index(INSTALLER)
        useradd = self.call_index(USERADD)
        load = self.call_index(SYSCTL_LOAD)
        start = self.call_index(RKE2_START)
        self.assertLess(installer, useradd)
        self.assertLess(useradd, load)
        self.assertLess(load, start)

    def test_the_values_come_from_rke2s_file(self) -> None:
        other = "# another RKE2 version\nvm.panic_on_oom = 0\n\n-kernel.panic=20\n; old\n"
        self.machine.answer("rke2_cis_sysctl", other)

        self.assert_succeeds(self.machine.run())

        self.assertEqual(other, self.host_text(SYSCTL_FILE))
        self.assertEqual("20", self.kernel("kernel.panic"))
        self.assertEqual("0", self.kernel("vm.overcommit_memory"))

    def test_the_file_is_found_where_the_installer_put_it(self) -> None:
        cases = {
            "Ubuntu, tarball": "/usr/local/share/rke2",
            "tarball, /usr/local read-only or a mount point": "/opt/rke2/share/rke2",
            "RHEL, RPM": "/usr/share/rke2",
        }
        for case, share_dir in cases.items():
            with self.subTest(case=case):
                machine = self.new_machine()
                machine.answer("rke2_share_dir", share_dir)

                result = machine.run()

                self.assert_succeeds(result)
                copied = f"from {share_dir}/rke2-cis-sysctl.conf to {SYSCTL_FILE}"
                self.assertIn(copied, result.stdout)
                self.assertEqual(RKE2_CIS_SYSCTL, machine.host_path(SYSCTL_FILE).read_text())

    def test_a_missing_settings_file_stops_before_rke2_starts(self) -> None:
        self.machine.answer("rke2_share_dir", "/var/tmp/elsewhere")

        line = self.assert_fails_once(
            self.machine.run(), "kernel settings file for its CIS profile"
        )

        self.assertIn("/usr/local/share/rke2/rke2-cis-sysctl.conf", line)
        self.assertIn("/usr/share/rke2/rke2-cis-sysctl.conf", line)
        self.assertNotIn(RKE2_START, self.machine.calls())

    def test_a_later_file_that_sets_another_value_stops_before_rke2_starts(self) -> None:
        self.machine.host_file("/etc/sysctl.d/99-local.conf", "vm.overcommit_memory = 0\n")

        line = self.assert_fails_once(
            self.machine.run(), "vm.overcommit_memory is 0, but RKE2 needs 1"
        )

        self.assertIn("/etc/sysctl.conf", line)
        self.assertNotIn(RKE2_START, self.machine.calls())

    def test_a_failed_load_stops_with_the_log_command(self) -> None:
        self.machine.answer("sysctl_restart_rc", "1")

        self.assert_fails_once(
            self.machine.run(),
            "systemd could not load the kernel settings.",
            "journalctl -u systemd-sysctl.service -e",
        )
        self.assertNotIn(RKE2_START, self.machine.calls())

    def test_a_failed_useradd_stops_before_rke2_starts(self) -> None:
        self.machine.answer("useradd_rc", "1")

        self.assert_fails_once(
            self.machine.run(), "useradd could not create the etcd user and group."
        )
        self.assertNotIn(RKE2_START, self.machine.calls())

    def test_an_existing_etcd_group_gets_only_the_user(self) -> None:
        self.machine.host_file("/etc/group", "root:x:0:\netcd:x:990:\n")

        self.assert_succeeds(self.machine.run())

        calls = self.machine.calls()
        self.assertIn("useradd -r -c etcd user -s /sbin/nologin -M -g etcd etcd\n", calls)
        self.assertNotIn("groupadd", calls)
        self.assertEqual(1, self.host_text("/etc/group").count("etcd:"))

    def test_an_existing_etcd_user_gets_only_the_group(self) -> None:
        self.machine.host_file(
            "/etc/passwd",
            "root:x:0:0:root:/root:/bin/bash\netcd:x:991:991::/nonexistent:/sbin/nologin\n",
        )

        self.assert_succeeds(self.machine.run())

        calls = self.machine.calls()
        self.assertIn("groupadd -r etcd\n", calls)
        self.assertNotIn("useradd", calls)

    def test_settings_that_the_kernel_has_already_are_not_loaded_again(self) -> None:
        for key, value in settings(RKE2_CIS_SYSCTL).items():
            self.machine.host_file(kernel_path(key), value + "\n")

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertNotIn(SYSCTL_LOAD, self.machine.calls())
        self.assertEqual(RKE2_CIS_SYSCTL, self.host_text(SYSCTL_FILE))
        self.assertIn("[ok]   the kernel has RKE2's settings for its CIS profile", result.stdout)


class SecondRunTests(CisTestCase):
    WATCHED = ("/etc/passwd", "/etc/group", SYSCTL_FILE, POD_SECURITY, CONFIG)

    def snapshot(self) -> dict[str, tuple[str, int]]:
        found = {}
        for path in self.WATCHED:
            host = self.machine.host_path(path)
            found[path] = (host.read_text(), mode(host))
        for key in settings(RKE2_CIS_SYSCTL):
            found[key] = (self.kernel(key), 0)
        return found

    def test_a_second_run_changes_nothing(self) -> None:
        self.assert_succeeds(self.machine.run())
        self.machine.mark_installed()
        before = self.snapshot()
        self.machine.answer("calls.log", "")

        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertEqual(before, self.snapshot())
        calls = self.machine.calls()
        for command in ("useradd", "groupadd", SYSCTL_LOAD):
            with self.subTest(command=command):
                self.assertNotIn(command, calls)
        self.assertIn("[ok]   the etcd user and group for RKE2's etcd exist", result.stdout)
        self.assertNotIn("copied RKE2's kernel settings", result.stdout)

    def test_a_changed_settings_file_is_replaced_by_rke2s_file(self) -> None:
        self.assert_succeeds(self.machine.run())
        self.machine.mark_installed()
        self.machine.host_file(SYSCTL_FILE, "vm.overcommit_memory=0\n")

        self.assert_succeeds(self.machine.run("--confirm-backup"))

        self.assertEqual(RKE2_CIS_SYSCTL, self.host_text(SYSCTL_FILE))
        self.assertEqual("1", self.kernel("vm.overcommit_memory"))


class CisStateTests(CisTestCase):
    def installed(self) -> FakeMachine:
        machine = self.new_machine()
        result = machine.run()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return machine

    def test_on_after_the_install(self) -> None:
        machine = self.installed()
        calls = machine.calls()

        result = run_lib(machine, "cis_state")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("on\n", result.stdout)
        self.assertEqual(calls, machine.calls())

    def test_off_names_the_reason(self) -> None:
        def no_profile(machine: FakeMachine) -> None:
            config = machine.host_path(CONFIG)
            config.write_text(config.read_text().replace("profile: cis\n", ""))

        def no_etcd_group(machine: FakeMachine) -> None:
            machine.host_file("/etc/group", "root:x:0:\n")

        def other_kernel_value(machine: FakeMachine) -> None:
            machine.host_file(kernel_path("vm.overcommit_memory"), "0\n")

        cases = {
            "no profile": (
                no_profile,
                "off: RKE2's settings (/etc/rancher/rke2/config.yaml) do not set profile: cis",
            ),
            "no pod security file": (
                lambda machine: machine.host_path(POD_SECURITY).unlink(),
                f"off: the pod security file {POD_SECURITY} is missing, so RKE2 cannot start",
            ),
            "no etcd group": (
                no_etcd_group,
                "off: the etcd user or group is missing in /etc/passwd or /etc/group,"
                " so RKE2 does not start with the profile",
            ),
            "no kernel settings file": (
                lambda machine: machine.host_path(SYSCTL_FILE).unlink(),
                f"off: {SYSCTL_FILE} is missing, so the next start of the machine does not"
                " load RKE2's kernel settings",
            ),
            "another kernel value": (
                other_kernel_value,
                "off: the kernel setting vm.overcommit_memory is 0, but RKE2 needs 1,"
                " so RKE2 does not start with the profile",
            ),
        }
        for case, (change, line) in cases.items():
            with self.subTest(case=case):
                machine = self.installed()
                change(machine)

                result = run_lib(machine, "cis_state")

                self.assertEqual(1, result.returncode, result.stderr)
                self.assertEqual(line + "\n", result.stdout)

    def test_a_quoted_profile_counts(self) -> None:
        machine = self.installed()
        config = machine.host_path(CONFIG)
        config.write_text(config.read_text().replace("profile: cis\n", 'profile: "cis"\n'))

        result = run_lib(machine, "cis_state")

        self.assertEqual("on\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
