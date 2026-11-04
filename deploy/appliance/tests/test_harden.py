#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of cis/harden.sh, the OS hardening of the appliance build.

The real script runs against a fake build VM, as in test_clean.py:
MEHO_TEST_ROOT points it to a temporary root folder that holds the copy of
the download (with the real lib/firewall.sh), the MEHO firewall file as the
form-2 install renders it, /etc/fstab, Ubuntu's tmp.mount and two AppArmor
profiles. PATH holds stub commands and links to the real tools it needs.
Each stub writes its arguments to calls.log. oscap prints a scan in its own
text form, from the answer file "scan".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

APPLIANCE = Path(__file__).resolve().parents[1]
HARDEN_SH = APPLIANCE / "cis" / "harden.sh"
LINUX_SERVER_LIB = APPLIANCE.parent / "linux-server" / "lib"

DOWNLOAD = "/opt/meho/appliance/download"
FIREWALL = "/etc/meho/firewall.nft"
RESULTS = "results.txt"
PREFIX = "xccdf_org.ssgproject.content_rule_"

# Ubuntu 24.04's /usr/share/systemd/tmp.mount, the part that matters.
UBUNTU_TMP_MOUNT = textwrap.dedent("""\
    [Unit]
    Description=Temporary Directory /tmp

    [Mount]
    What=tmpfs
    Where=/tmp
    Type=tmpfs
    Options=mode=1777,strictatime,nosuid,nodev,size=50%%,nr_inodes=1m

    [Install]
    WantedBy=local-fs.target
    """)

FSTAB = textwrap.dedent("""\
    # /etc/fstab of the cloud image
    LABEL=cloudimg-rootfs / ext4 discard,commit=30,errors=remount-ro 0 1
    tmpfs /dev/shm tmpfs defaults 0 0
    tmpfs /tmp tmpfs defaults,nosuid 0 0
    """)

REAL_TOOLS = (
    "bash",
    "cat",
    "sed",
    "awk",
    "grep",
    "tr",
    "mkdir",
    "mktemp",
    "chmod",
    "mv",
    "rm",
    "ln",
    "install",
    "dirname",
)

STUBS = {
    "apt-get": 'case "$1" in update) exit "$(cat "$MEHO_FAKE/apt_update_rc")" ;; esac\n',
    # oscap xccdf eval [--remediate] --tailoring-file <f> --profile <p> <ds>
    "oscap": textwrap.dedent("""\
        case "$*" in
          *--remediate*) exit "$(cat "$MEHO_FAKE/remediate_rc")" ;;
        esac
        cat "$MEHO_FAKE/scan"
        exit "$(cat "$MEHO_FAKE/scan_rc")"
        """),
    "systemctl": 'case "$1" in is-enabled) exit 0 ;; esac\n',
    "mount": "",
    "sysctl": "",
    "sshd": 'exit "$(cat "$MEHO_FAKE/sshd_rc")"\n',
    "apparmor_parser": "exit 1\n",
    "nft": textwrap.dedent("""\
        case "$*" in
          "-c -f "*) exit "$(cat "$MEHO_FAKE/nft_check_rc")" ;;
          "list chain inet meho prerouting")
            printf 'table inet meho {\\n\\tchain prerouting {\\n'
            printf '\\t\\ttype filter hook prerouting priority mangle; policy drop;\\n'
            printf '\\t}\\n}\\n' ;;
        esac
        exit 0
        """),
}


def scan_output(results: dict[str, str]) -> str:
    """A scan as oscap prints it: a carriage return before each tab."""
    blocks = [
        f"Title\r\tA rule\nRule\r\t{PREFIX}{rule}\nResult\r\t{result}\n"
        for rule, result in results.items()
    ]
    return "--- Starting Evaluation ---\n\n" + "\n".join(blocks)


def render_firewall_file() -> str:
    """The MEHO firewall file as the form-2 install writes it."""
    script = (
        f'source "{LINUX_SERVER_LIB}/common.sh"; source "{LINUX_SERVER_LIB}/firewall.sh"; '
        'render_firewall_rules 10.0.50.0/24 "" 10.42.0.0/16 10.43.0.0/16'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = f'printf \'%s %s\\n\' "{path.name}" "$*" >> "$MEHO_FAKE/calls.log"\n'
    path.write_text("#!/bin/sh\n" + log + body)
    path.chmod(0o755)


class FakeBuildVM:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="meho-harden-test-")
        base = Path(self._tempdir.name)
        self.root = base / "root"
        self.fake = base / "fake"
        self.stubs = base / "stubs"
        self.tools = base / "tools"
        for folder in (self.root, self.fake, self.stubs, self.tools):
            folder.mkdir()
        for tool in REAL_TOOLS:
            found = shutil.which(tool)
            if found is None:
                raise RuntimeError(f"the tests need '{tool}' on PATH")
            (self.tools / tool).symlink_to(found)
        for name, body in STUBS.items():
            _write_script(self.stubs / name, body)
        self.content = base / "ssg-ubuntu2404-ds.xml"
        self.content.write_text("<data-stream/>\n")
        self.results = base / RESULTS
        self.host_file(
            f"{DOWNLOAD}/lib/firewall.sh", (LINUX_SERVER_LIB / "firewall.sh").read_text()
        )
        self.firewall = render_firewall_file()
        self.host_file(FIREWALL, self.firewall)
        self.host_file("/etc/fstab", FSTAB)
        self.host_file("/usr/share/systemd/tmp.mount", UBUNTU_TMP_MOUNT)
        self.host_file(
            "/etc/apparmor.d/1password",
            "profile 1password /opt/1Password/1password flags=(unconfined) {\n}\n",
        )
        self.host_file("/etc/apparmor.d/usr.sbin.rsyslogd", "profile rsyslogd {\n}\n")
        self.host_path("/run").mkdir()
        self.answer("apt_update_rc", "0")
        self.answer("remediate_rc", "2")
        self.answer("scan_rc", "2")
        self.answer("nft_check_rc", "0")
        self.answer("sshd_rc", "0")
        self.answer("scan", scan_output({"package_aide_installed": "pass"}))
        self.answer("calls.log", "")

    def close(self) -> None:
        self._tempdir.cleanup()

    def answer(self, name: str, content: str) -> None:
        (self.fake / name).write_text(content)

    def host_path(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def host_file(self, path: str, content: str) -> None:
        target = self.host_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def read(self, path: str) -> str:
        return self.host_path(path).read_text()

    def calls(self, command: str | None = None) -> list[str]:
        lines = [line.rstrip() for line in (self.fake / "calls.log").read_text().splitlines()]
        if command is None:
            return lines
        return [line for line in lines if line.split(" ", 1)[0] == command]

    def run(self) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}:{self.tools}",
            "MEHO_TEST_ROOT": str(self.root),
            "MEHO_FAKE": str(self.fake),
            "LC_ALL": "C",
        }
        return subprocess.run(
            [str(HARDEN_SH), "--content", str(self.content), "--results", str(self.results)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )


class HardenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = FakeBuildVM()
        self.addCleanup(self.vm.close)

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_succeeds(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, result.returncode, self.output(result))
        self.assertNotIn("[FAIL]", self.output(result))

    def assert_fails(self, result: subprocess.CompletedProcess[str], fragment: str) -> None:
        self.assertEqual(1, result.returncode, self.output(result))
        fail_lines = [line for line in result.stderr.splitlines() if line.startswith("[FAIL]")]
        self.assertEqual(1, len(fail_lines), self.output(result))
        self.assertIn(fragment, fail_lines[0])
        self.assertIn(" Fix: ", fail_lines[0])

    def test_the_packages_go_in_and_ufw_goes(self) -> None:
        self.assert_succeeds(self.vm.run())

        self.assertEqual(
            [
                "apt-get update -q",
                "apt-get install -y -q --no-install-recommends "
                "aide aide-common openscap-scanner libpam-pwquality "
                "cracklib-runtime apparmor-utils",
                "apt-get purge -y -q ufw",
            ],
            self.vm.calls("apt-get"),
        )

    def test_the_scanner_files_stay_on_the_appliance(self) -> None:
        self.assert_succeeds(self.vm.run())

        cis = "/opt/meho/appliance/cis"
        self.assertEqual("<data-stream/>\n", self.vm.read(f"{cis}/ssg-ubuntu2404-ds.xml"))
        for name in ("tailoring.xml", "not-applicable.md", "exceptions.md"):
            with self.subTest(file=name):
                self.assertEqual(
                    (APPLIANCE / "cis" / name).read_text(), self.vm.read(f"{cis}/{name}")
                )

    def test_script_checks_run_through_the_runner_from_the_noexec_tmp(self) -> None:
        # OpenSCAP uses the runner when it cannot execute a check it wrote
        # under the noexec /tmp; a check without its x bit stands in here.
        self.assert_succeeds(self.vm.run())

        runner = self.vm.host_path("/usr/local/bin/oscap-run-sce-script")
        self.assertEqual(0o755, runner.stat().st_mode & 0o777)
        check = self.vm.host_path("/tmp/oscap.test/check.sh")
        check.parent.mkdir(parents=True)
        check.write_text('#!/bin/bash\nexit "$XCCDF_RESULT_FAIL"\n')
        check.chmod(0o644)
        result = subprocess.run(
            [str(runner), str(check)],
            capture_output=True,
            text=True,
            env={"XCCDF_RESULT_FAIL": "102"},
            check=False,
            timeout=10,
        )
        self.assertEqual(102, result.returncode, result.stderr)

    def test_the_fixes_run_first_then_the_settings_then_the_firewall_then_the_scan(self) -> None:
        self.assert_succeeds(self.vm.run())

        calls = self.vm.calls()
        oscap = [index for index, call in enumerate(calls) if call.startswith("oscap ")]
        self.assertEqual(2, len(oscap), calls)
        fixes, scan = oscap
        self.assertIn("--remediate", calls[fixes])
        self.assertIn("--profile xccdf_com.evoila.meho_profile_appliance_remediate ", calls[fixes])
        self.assertNotIn("--remediate", calls[scan])
        self.assertIn("--profile xccdf_com.evoila.meho_profile_appliance ", calls[scan])
        mount = calls.index("mount -o remount,nodev,nosuid,noexec /dev/shm")
        load = calls.index(f"nft -f {self.vm.host_path(FIREWALL)}")
        self.assertLess(fixes, mount)
        self.assertLess(mount, load)
        self.assertLess(load, scan)

    def test_dev_shm_and_tmp_get_nodev_nosuid_and_noexec(self) -> None:
        self.assert_succeeds(self.vm.run())

        fstab = self.vm.read("/etc/fstab")
        self.assertEqual(
            "# /etc/fstab of the cloud image\n"
            "LABEL=cloudimg-rootfs / ext4 discard,commit=30,errors=remount-ro 0 1\n"
            "tmpfs /dev/shm tmpfs defaults,nodev,nosuid,noexec 0 0\n",
            fstab,
        )
        self.assertIn(
            "Options=mode=1777,strictatime,nosuid,nodev,size=50%%,nr_inodes=1m,noexec\n",
            self.vm.read("/etc/systemd/system/tmp.mount"),
        )
        self.assertIn("systemctl enable --now tmp.mount", self.vm.calls())

    def test_only_the_unconfined_apparmor_profiles_are_turned_off(self) -> None:
        self.assert_succeeds(self.vm.run())

        disabled = self.vm.host_path("/etc/apparmor.d/disable")
        self.assertEqual(["1password"], sorted(path.name for path in disabled.iterdir()))
        self.assertEqual("/etc/apparmor.d/1password", os.readlink(disabled / "1password"))
        self.assertEqual(
            ["apparmor_parser -R /etc/apparmor.d/1password"], self.vm.calls("apparmor_parser")
        )

    def test_ipv6_is_off_and_aide_skips_the_busy_folders(self) -> None:
        self.assert_succeeds(self.vm.run())

        sysctl = self.vm.read("/etc/sysctl.d/60-meho-ipv6-off.conf")
        self.assertIn("net.ipv6.conf.all.disable_ipv6 = 1\n", sysctl)
        self.assertIn("net.ipv6.conf.default.disable_ipv6 = 1\n", sysctl)
        aide = self.vm.read("/etc/aide/aide.conf.d/90_meho_appliance").splitlines()
        self.assertEqual(
            [
                "!/var/lib/rancher",
                "!/var/lib/kubelet",
                "!/var/lib/meho/volumes",
                "!/var/log",
                "!/run",
            ],
            aide[1:],
        )

    def test_ssh_takes_keys_only_and_never_root(self) -> None:
        self.assert_succeeds(self.vm.run())

        dropin = "/etc/ssh/sshd_config.d/05-meho-appliance.conf"
        lines = self.vm.read(dropin).splitlines()
        for setting in (
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "PermitRootLogin no",
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, lines)
        self.assertEqual(0o600, self.vm.host_path(dropin).stat().st_mode & 0o777)
        self.assertIn("sshd -t", self.vm.calls())

    def test_settings_sshd_refuses_stop_the_hardening(self) -> None:
        self.vm.answer("sshd_rc", "1")

        self.assert_fails(self.vm.run(), "sshd does not accept its settings")

    def test_nftables_service_loads_the_meho_firewall_without_a_flush(self) -> None:
        self.assert_succeeds(self.vm.run())

        conf = self.vm.read("/etc/nftables.conf")
        self.assertIn(f'\ninclude "{FIREWALL}"\n', conf)
        self.assertNotIn("\nflush ruleset", conf)
        self.assertEqual(
            "[Service]\nExecStop=\n",
            self.vm.read("/etc/systemd/system/nftables.service.d/meho-no-flush.conf").split(
                "\n", 2
            )[2],
        )
        self.assertIn("systemctl enable --now nftables.service", self.vm.calls())

    def test_the_firewall_blocks_by_default_in_all_three_directions(self) -> None:
        self.assert_succeeds(self.vm.run())

        rules = self.vm.read(FIREWALL)
        for hook in ("input", "forward", "output"):
            with self.subTest(hook=hook):
                self.assertIn(f"type filter hook {hook} priority filter; policy drop;", rules)
        self.assertNotIn("policy accept;", rules)
        # Nothing else in the file changes.
        self.assertEqual(self.vm.firewall.replace("policy accept;", "policy drop;"), rules)
        self.assertEqual(0o600, self.vm.host_path(FIREWALL).stat().st_mode & 0o777)

    def test_a_firewall_file_in_another_form_stops_the_hardening_unchanged(self) -> None:
        changed = self.vm.firewall.replace(
            "type filter hook forward priority filter; policy accept;",
            "type filter hook forward priority 0; policy accept;",
        )
        self.vm.host_file(FIREWALL, changed)

        result = self.vm.run()

        self.assert_fails(result, "no chain for the hook forward")
        self.assertEqual(changed, self.vm.read(FIREWALL))
        self.assertEqual(1, len(self.vm.calls("oscap")))

    def test_a_firewall_file_nft_refuses_is_not_used(self) -> None:
        self.vm.answer("nft_check_rc", "1")

        result = self.vm.run()

        self.assert_fails(result, "nft does not accept the firewall file")
        self.assertEqual(self.vm.firewall, self.vm.read(FIREWALL))
        self.assertEqual([], [c for c in self.vm.calls("nft") if c.startswith("nft -f")])

    def test_no_archive_stops_step_1(self) -> None:
        self.vm.answer("apt_update_rc", "100")

        result = self.vm.run()

        self.assert_fails(result, "apt-get update failed")
        self.assertIn("Ubuntu archive", result.stderr)
        self.assertEqual([], self.vm.calls("oscap"))

    def test_oscap_exit_code_2_is_a_failing_rule_and_1_an_error(self) -> None:
        for rc, fails in (("0", False), ("2", False), ("1", True)):
            with self.subTest(remediate_rc=rc):
                self.vm.answer("remediate_rc", rc)

                result = self.vm.run()

                if fails:
                    self.assert_fails(result, "while it ran the fixes")
                else:
                    self.assert_succeeds(result)

    def test_the_result_list_and_the_warnings(self) -> None:
        self.vm.answer(
            "scan",
            scan_output(
                {
                    "package_aide_installed": "pass",
                    "grub2_password": "fail",
                    "aide_build_database": "pass",
                    "sshd_set_max_auth_tries": "fail",
                    "service_cron_enabled": "notapplicable",
                    "file_permissions_grub2_cfg": "error",
                }
            ),
        )

        result = self.vm.run()

        self.assert_succeeds(result)
        lines = [
            line for line in self.vm.results.read_text().splitlines() if not line.startswith("#")
        ]
        self.assertEqual(
            [
                "pass package_aide_installed",
                "fail grub2_password",
                "pass aide_build_database",
                "fail sshd_set_max_auth_tries",
                "notapplicable service_cron_enabled",
                "error file_permissions_grub2_cfg",
            ],
            lines,
        )
        out = result.stdout
        self.assertIn("[note] grub2_password: fail, an exception in exceptions.md", out)
        self.assertIn("[note] aide_build_database passes, so it can leave exceptions.md", out)
        self.assertIn(
            "[WARN] sshd_set_max_auth_tries: fail, and it is in neither "
            "not-applicable.md nor exceptions.md",
            out,
        )
        self.assertIn("[WARN] file_permissions_grub2_cfg: error,", out)
        self.assertIn("[WARN] 2 rules fail and are in neither list.", out)
        warnings = [line for line in out.splitlines() if line.startswith("[WARN]")]
        self.assertEqual(3, len(warnings), warnings)

    def test_a_scan_oscap_cannot_run_stops(self) -> None:
        self.vm.answer("scan_rc", "1")

        self.assert_fails(self.vm.run(), "oscap could not scan")

    def test_a_second_run_changes_nothing_more(self) -> None:
        self.assert_succeeds(self.vm.run())
        files = {
            path: self.vm.read(path)
            for path in (
                "/etc/fstab",
                FIREWALL,
                "/etc/systemd/system/tmp.mount",
                "/etc/nftables.conf",
            )
        }

        self.assert_succeeds(self.vm.run())

        for path, content in files.items():
            with self.subTest(path=path):
                self.assertEqual(content, self.vm.read(path))


if __name__ == "__main__":
    unittest.main()
