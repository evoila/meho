#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of firststart.sh and banner.sh: the wizard fields and their checks,
what each step changes, the done-markers, the restart after a failed step,
that no secret reaches a log, and the console banner."""

from __future__ import annotations

import time
import unittest

from firststart_harness import (
    JOIN_TOKEN,
    PASSWORD,
    SSH_KEY,
    STATE,
    STATIC_FIELDS,
    STEPS,
    FakeAppliance,
    FirstStartTestCase,
    ovf_env,
)

ALL_MARKERS = sorted(f"{number}-{name}.done" for number, name in enumerate(STEPS, start=1))


class FirstStartTests(FirstStartTestCase):
    def test_a_static_import_sets_up_the_whole_appliance(self) -> None:
        result = self.vm.run()

        self.assert_succeeds(result)
        self.assertEqual("meho.example.com\n", self.vm.read("/etc/hostname"))
        self.assertEqual(
            "network:\n  version: 2\n  renderer: networkd\n  ethernets:\n"
            '    meho:\n      match:\n        name: "en*"\n'
            "      addresses: [10.0.0.10/24]\n"
            "      routes:\n        - to: default\n          via: 10.0.0.1\n"
            "      nameservers:\n        addresses: [10.0.0.2, 10.0.0.3]\n",
            self.vm.read("/etc/netplan/50-meho.yaml"),
        )
        self.assertEqual(0o600, self.vm.mode("/etc/netplan/50-meho.yaml"))
        self.assertEqual(
            "[Time]\nNTP=ntp1.example.com 10.0.0.4\n",
            self.vm.read("/etc/systemd/timesyncd.conf.d/50-meho.conf"),
        )
        self.assertIn(
            "useradd --create-home --shell /bin/bash --groups sudo admin", self.vm.calls()
        )
        self.assertIn("passwd --expire admin", self.vm.calls())
        self.assertEqual(f"{SSH_KEY}\n", self.vm.read("/home/admin/.ssh/authorized_keys"))
        self.assertEqual(0o600, self.vm.mode("/home/admin/.ssh/authorized_keys"))
        self.assertEqual(0o700, self.vm.mode("/home/admin/.ssh"))
        self.assertEqual(["meho-host admin-network 10.0.50.0/24"], self.vm.calls("meho-host"))
        self.assertEqual(
            ["install.sh --from-disk --hostname meho.example.com"], self.vm.calls("install.sh")
        )
        self.assertEqual(ALL_MARKERS, self.vm.markers())
        self.assertTrue(self.vm.host_path("/var/lib/meho/firststart.done").is_file())

    def test_the_steps_run_in_the_order_of_the_design(self) -> None:
        self.assert_succeeds(self.vm.run())

        order = [
            "ssh-keygen -A",
            "growpart /dev/sda 1",
            "resize2fs /dev/sda1",
            "hostnamectl hostname meho.example.com",
            "netplan apply",
            "systemctl restart systemd-timesyncd.service",
            "passwd --expire admin",
            "meho-host admin-network 10.0.50.0/24",
            "timedatectl show --property=NTPSynchronized --value",
            "install.sh --from-disk --hostname meho.example.com",
            "update-grub",
        ]
        calls = self.vm.calls()
        self.assertEqual(order, [call for call in calls if call in order])

    def test_dhcp_needs_no_address_fields(self) -> None:
        self.vm.wizard({"net.mode": "dhcp", "net.ip": "", "net.gateway": "", "net.dns": ""})

        self.assert_succeeds(self.vm.run())

        netplan = self.vm.read("/etc/netplan/50-meho.yaml")
        self.assertIn("      dhcp4: true\n", netplan)
        self.assertNotIn("addresses", netplan)

    def test_an_empty_network_field_means_static(self) -> None:
        self.vm.wizard({"net.mode": ""})

        self.assert_succeeds(self.vm.run())

        self.assertIn("addresses: [10.0.0.10/24]", self.vm.read("/etc/netplan/50-meho.yaml"))

    def test_the_fields_are_read_from_the_cd_rom_without_vmware_tools(self) -> None:
        self.vm.no_answer("guestinfo-ovfenv")
        self.vm.answer("iso-ovfenv", ovf_env(STATIC_FIELDS))

        self.assert_succeeds(self.vm.run())

        self.assertEqual("meho.example.com\n", self.vm.read("/etc/hostname"))

    def test_escaped_characters_reach_the_password_unchanged(self) -> None:
        self.assert_succeeds(self.vm.run())

        self.assertEqual(f"admin:{PASSWORD}\n", (self.vm.fake / "chpasswd_input").read_text())

    def test_a_short_password_is_refused_before_anything_changes(self) -> None:
        self.vm.wizard({"admin.password": "only13letters"})

        result = self.vm.run()

        self.assert_fails_once(result, "shorter than 14 characters", "vApp Options")
        self.assertEqual("ubuntu\n", self.vm.read("/etc/hostname"))
        self.assertEqual([], self.vm.calls("chpasswd"))
        self.assertEqual([], self.vm.calls("netplan"))
        self.assertNotIn("only13letters", self.output(result))

    def test_each_wrong_field_is_refused_before_anything_changes(self) -> None:
        wrong = {
            "hostname": ({"hostname": "meho"}, "not a full DNS name"),
            "net.mode": ({"net.mode": "manual"}, "must be static or dhcp"),
            "net.ip": ({"net.ip": "10.0.0.10"}, "with a prefix"),
            "net.gateway": ({"net.gateway": "gateway"}, "default gateway"),
            "net.dns empty": ({"net.dns": " , "}, "No DNS server"),
            "net.dns": ({"net.dns": "10.0.0.2,dns.example.com"}, "DNS server 'dns.example.com'"),
            "net.ntp empty": ({"net.ntp": ""}, "No time server"),
            "net.ntp": ({"net.ntp": "ntp;reboot"}, "time server 'ntp;reboot'"),
            "net.admin_network": ({"net.admin_network": "10.0.50.7/24"}, "admin network"),
            "net.admin_network empty": ({"net.admin_network": ""}, "admin network"),
            "admin.ssh_key": ({"admin.ssh_key": "not a key"}, "SSH public key"),
            "admin.ssh_key empty": ({"admin.ssh_key": ""}, "SSH public key"),
            "admin.ssh_key two lines": (
                {"admin.ssh_key": f"{SSH_KEY}\n{SSH_KEY}"},
                "SSH public key",
            ),
            "admin.password line break": ({"admin.password": "fourteen-chars\nmore"}, "line break"),
            "cluster.join.address": (
                {"cluster.join.address": "10.0.0.20"},
                "cannot join a cluster",
            ),
            "cluster.join.token": ({"cluster.join.token": JOIN_TOKEN}, "cannot join a cluster"),
        }
        for name, (changes, fragment) in wrong.items():
            with self.subTest(field=name):
                self.vm.wizard(changes)
                self.vm.answer("calls.log", "")

                result = self.vm.run()

                self.assert_fails_once(result, "Step 3 (wizard-fields)", fragment)
                self.assertEqual("ubuntu\n", self.vm.read("/etc/hostname"))
                self.assertEqual([], self.vm.calls("netplan"))
                self.assertEqual([], self.vm.calls("chpasswd"))

    def test_the_join_fields_are_refused_before_anything_changes(self) -> None:
        self.vm.wizard({"cluster.join.address": "10.0.0.20", "cluster.join.token": JOIN_TOKEN})

        result = self.vm.run()

        self.assert_fails_once(
            result, "Step 3 (wizard-fields)", "cannot join a cluster", "empty both join fields"
        )
        self.assertEqual("ubuntu\n", self.vm.read("/etc/hostname"))
        self.assertEqual([], self.vm.calls("chpasswd"))
        self.assertEqual([], self.vm.calls("install.sh"))
        self.assertNotIn(JOIN_TOKEN, self.output(result))

    def test_no_secret_reaches_the_output_the_console_or_a_command_line(self) -> None:
        result = self.vm.run()

        self.assert_succeeds(result)
        bootloader_password = self.vm.read("/var/lib/meho/bootloader-password").strip()
        places = {
            "output": self.output(result),
            "console": self.vm.read("/dev/tty1"),
            "command lines": (self.vm.fake / "calls.log").read_text(),
        }
        for place, text in places.items():
            for secret in (PASSWORD, "Wizard&amp;Pass", bootloader_password):
                with self.subTest(place=place, secret=secret[:6]):
                    self.assertNotIn(secret, text)

    def test_the_progress_shows_on_the_vm_console(self) -> None:
        self.assert_succeeds(self.vm.run())

        console = self.vm.read("/dev/tty1")
        self.assertIn("== Step 3: wizard-fields", console)
        self.assertIn("first start is done", console)

    def test_the_bootloader_password_is_set_and_the_entries_boot_without_it(self) -> None:
        self.assert_succeeds(self.vm.run())

        password = self.vm.read("/var/lib/meho/bootloader-password").strip()
        self.assertRegex(password, r"^[A-Za-x0-9]{24}$")
        self.assertEqual(0o600, self.vm.mode("/var/lib/meho/bootloader-password"))
        script = self.vm.read("/etc/grub.d/01_meho-password")
        self.assertIn('set superusers="root"\nexport superusers\n', script)
        self.assertIn("password_pbkdf2 root grub.pbkdf2.sha512.10000.FAKESALT.FAKEHASH\n", script)
        self.assertEqual(0o700, self.vm.mode("/etc/grub.d/01_meho-password"))
        self.assertIn(
            'CLASS="--class gnu-linux --class gnu --class os --unrestricted"\n',
            self.vm.read("/etc/grub.d/10_linux"),
        )
        self.assertEqual(
            "GRUB_TIMEOUT_STYLE=menu\nGRUB_TIMEOUT=5\nGRUB_RECORDFAIL_TIMEOUT=5\n",
            self.vm.read("/etc/default/grub.d/90-meho-boot-menu.cfg"),
        )

    def test_an_unknown_10_linux_stops_step_6(self) -> None:
        self.vm.host_file("/etc/grub.d/10_linux", 'CLASS="--class something-else"\n')

        result = self.vm.run()

        self.assert_fails_once(result, "Step 6 (bootloader-password)", "CLASS line")
        self.assertEqual([], self.vm.calls("update-grub"))

    def test_a_partition_that_already_fills_the_disk_is_fine(self) -> None:
        for rc, succeeds in (("0", True), ("1", True), ("2", False)):
            with self.subTest(growpart_rc=rc):
                self.vm.answer("growpart_rc", rc)
                for marker in (
                    self.vm.host_path(STATE).glob("*") if self.vm.host_path(STATE).is_dir() else []
                ):
                    marker.unlink()
                self.vm.host_path("/var/lib/meho/firststart.done").unlink(missing_ok=True)

                result = self.vm.run()

                if succeeds:
                    self.assert_succeeds(result)
                else:
                    self.assert_fails_once(result, "Step 2 (disk)", "growpart")

    def test_a_failure_before_the_admin_account_exists_says_to_restart_from_vcenter(self) -> None:
        self.vm.answer("growpart_rc", "2")

        line = self.assert_fails_once(
            self.vm.run(), "Step 2 (disk)", "power the VM off and on in vCenter"
        )

        self.assertNotIn("sudo reboot", line)

    def test_a_clock_that_does_not_synchronise_stops_step_4(self) -> None:
        self.vm.answer("ntp_synchronized", "no\n")

        result = self.vm.run()

        self.assert_fails_once(result, "Step 4 (clock)", "UDP port 123")
        self.assertEqual([], self.vm.calls("install.sh"))

    def test_a_second_run_does_not_repeat_a_finished_step(self) -> None:
        self.assert_succeeds(self.vm.run())
        host_key = self.vm.read("/etc/ssh/ssh_host_ed25519_key")
        self.vm.answer("calls.log", "")

        result = self.vm.run()

        self.assert_succeeds(result)
        self.assertEqual(host_key, self.vm.read("/etc/ssh/ssh_host_ed25519_key"))
        self.assertEqual([], self.vm.calls())
        self.assertIn("step 5 (core) is already done", self.output(result))

    def test_a_run_after_a_failed_step_goes_on_from_that_step(self) -> None:
        self.vm.answer("install_rc", "1")
        failed = self.vm.run()
        self.assert_fails_once(failed, "Step 5 (core)", "install.sh stopped")
        self.assertEqual(ALL_MARKERS[:4], self.vm.markers())
        host_key = self.vm.read("/etc/ssh/ssh_host_ed25519_key")
        # The admin fixed the host name on the console after the failure.
        self.vm.host_file("/etc/hostname", "meho2.example.com\n")
        self.vm.answer("install_rc", "0")
        self.vm.answer("calls.log", "")

        result = self.vm.run()

        self.assert_succeeds(result)
        self.assertEqual(host_key, self.vm.read("/etc/ssh/ssh_host_ed25519_key"))
        self.assertEqual([], self.vm.calls("ssh-keygen"))
        self.assertEqual([], self.vm.calls("chpasswd"))
        self.assertEqual(
            ["install.sh --from-disk --hostname meho2.example.com"], self.vm.calls("install.sh")
        )
        self.assertEqual(ALL_MARKERS, self.vm.markers())
        self.assertFalse(self.vm.host_path(f"{STATE}/failed").exists())

    def test_a_failed_meho_host_stops_step_3(self) -> None:
        self.vm.answer("meho_host_rc", "1")

        result = self.vm.run()

        self.assert_fails_once(result, "Step 3 (wizard-fields)", "exit code 1")
        self.assertEqual(ALL_MARKERS[:2], self.vm.markers())


def console(*answers: str) -> str:
    """What an admin types on the VM console: one line per answer."""
    return "".join(f"{answer}\n" for answer in answers)


# The answers to the console questions, in their order, for the same
# appliance as STATIC_FIELDS.
STATIC_ANSWERS = {
    "hostname": "MEHO.Example.com",
    "net.ntp": "ntp1.example.com,10.0.0.4",
    "net.mode": "static",
    "net.ip": "10.0.0.10/24",
    "net.gateway": "10.0.0.1",
    "net.dns": "10.0.0.2, 10.0.0.3",
    "net.admin_network": "10.0.50.0/24",
    "admin.password": PASSWORD,
    "admin.password again": PASSWORD,
    "admin.ssh_key": SSH_KEY,
}


def with_passwords(*passwords: str) -> list[str]:
    """The static answers, with these answers in place of the two passwords."""
    answers = list(STATIC_ANSWERS.values())
    start = list(STATIC_ANSWERS).index("admin.password")
    answers[start : start + 2] = passwords
    return answers


class ConsoleSetupTests(FirstStartTestCase):
    """An import without vCenter's vApp options: no OVF environment, so first
    start asks for the fields on the VM console."""

    def setUp(self) -> None:
        super().setUp()
        self.vm.no_answer("guestinfo-ovfenv")

    def test_the_console_setup_ends_like_a_wizard_import(self) -> None:
        wizard = FakeAppliance()
        self.addCleanup(wizard.close)
        self.assert_succeeds(wizard.run())

        result = self.vm.run(console(*STATIC_ANSWERS.values()))

        self.assert_succeeds(result)
        for path in (
            "/etc/hostname",
            "/etc/netplan/50-meho.yaml",
            "/etc/systemd/timesyncd.conf.d/50-meho.conf",
            "/home/admin/.ssh/authorized_keys",
        ):
            with self.subTest(path=path):
                self.assertEqual(wizard.read(path), self.vm.read(path))
                self.assertEqual(wizard.mode(path), self.vm.mode(path))
        self.assertEqual(
            (wizard.fake / "chpasswd_input").read_text(),
            (self.vm.fake / "chpasswd_input").read_text(),
        )

        def commands(vm: FakeAppliance) -> list[str]:
            # Only the search for the OVF environment differs.
            search = ("vmtoolsd", "mount", "umount")
            return [
                call.replace(str(vm.root), "")
                for call in vm.calls()
                if call.split(" ", 1)[0] not in search
            ]

        self.assertEqual(commands(wizard), commands(self.vm))
        self.assertEqual(ALL_MARKERS, self.vm.markers())
        self.assertIn("no wizard fields", self.vm.read("/dev/tty1"))

    def test_dhcp_asks_for_no_address(self) -> None:
        answers = dict(STATIC_ANSWERS, **{"net.mode": "dhcp"})
        for key in ("net.ip", "net.gateway", "net.dns"):
            del answers[key]

        result = self.vm.run(console(*answers.values()))

        self.assert_succeeds(result)
        self.assertNotIn("IP address with prefix", self.output(result))
        self.assertIn("      dhcp4: true\n", self.vm.read("/etc/netplan/50-meho.yaml"))

    def test_an_empty_network_answer_means_static(self) -> None:
        answers = dict(STATIC_ANSWERS, **{"net.mode": ""})

        self.assert_succeeds(self.vm.run(console(*answers.values())))

        self.assertIn("addresses: [10.0.0.10/24]", self.vm.read("/etc/netplan/50-meho.yaml"))

    def test_a_wrong_answer_is_asked_again(self) -> None:
        wrong = {
            "hostname": ("meho", "not a full DNS name"),
            "net.ntp": ("ntp;reboot", "time server 'ntp;reboot'"),
            "net.mode": ("manual", "must be static or dhcp"),
            "net.ip": ("10.0.0.10", "with a prefix"),
            "net.gateway": ("gateway", "default gateway"),
            "net.dns": ("", "No DNS server"),
            "net.admin_network": ("10.0.50.7/24", "admin network"),
            "admin.ssh_key": ("not a key", "SSH public key"),
        }
        for key, (answer, fragment) in wrong.items():
            with self.subTest(field=key):
                answers = list(STATIC_ANSWERS.values())
                position = list(STATIC_ANSWERS).index(key)
                answers.insert(position, answer)
                for marker in self.vm.host_path(STATE).glob("*"):
                    marker.unlink()

                result = self.vm.run(console(*answers))

                self.assert_succeeds(result)
                self.assertIn(fragment, self.output(result))
                self.assertIn("Try again.", self.output(result))
                self.assertEqual("meho.example.com\n", self.vm.read("/etc/hostname"))

    def test_a_password_of_13_characters_is_asked_again(self) -> None:
        answers = with_passwords("only13letters", "only13letters", PASSWORD, PASSWORD)

        result = self.vm.run(console(*answers))

        self.assert_succeeds(result)
        self.assertIn("shorter than 14 characters. Try again.", self.output(result))
        self.assertNotIn("only13letters", self.output(result))
        self.assertEqual(f"admin:{PASSWORD}\n", (self.vm.fake / "chpasswd_input").read_text())

    def test_two_different_passwords_are_asked_again(self) -> None:
        answers = with_passwords(PASSWORD, PASSWORD + "x", PASSWORD, PASSWORD)

        result = self.vm.run(console(*answers))

        self.assert_succeeds(result)
        self.assertIn("The two passwords differ. Try again.", self.output(result))
        self.assertEqual(f"admin:{PASSWORD}\n", (self.vm.fake / "chpasswd_input").read_text())

    def test_an_empty_ssh_key_leaves_ssh_closed(self) -> None:
        answers = dict(STATIC_ANSWERS, **{"admin.ssh_key": ""})

        result = self.vm.run(console(*answers.values()))

        self.assert_succeeds(result)
        self.assertFalse(self.vm.host_path("/home/admin/.ssh/authorized_keys").exists())
        self.assertIn("SSH stays closed", self.output(result))
        self.assertEqual(["meho-host admin-network 10.0.50.0/24"], self.vm.calls("meho-host"))

    def test_an_empty_ssh_key_removes_the_key_of_an_earlier_try(self) -> None:
        self.vm.host_file("/home/admin/.ssh/authorized_keys", f"{SSH_KEY}\n")
        answers = dict(STATIC_ANSWERS, **{"admin.ssh_key": ""})

        self.assert_succeeds(self.vm.run(console(*answers.values())))

        self.assertFalse(self.vm.host_path("/home/admin/.ssh/authorized_keys").exists())

    def test_input_that_ends_early_stops_step_3_and_changes_nothing(self) -> None:
        for answered in (0, 3, 7):
            with self.subTest(answered=answered):
                result = self.vm.run(console(*list(STATIC_ANSWERS.values())[:answered]))

                line = self.assert_fails_once(
                    result,
                    "Step 3 (wizard-fields)",
                    "console setup ended",
                    "power the VM off and on",
                )
                self.assertEqual("ubuntu\n", self.vm.read("/etc/hostname"))
                self.assertEqual([], self.vm.calls("netplan"))
                self.assertEqual([], self.vm.calls("chpasswd"))
                self.assertEqual(ALL_MARKERS[:2], self.vm.markers())
                self.assertEqual(f"{line}\n", self.vm.read(f"{STATE}/failed"))

    def test_no_password_reaches_the_output_the_console_or_a_command_line(self) -> None:
        answers = with_passwords(
            "only13letters", "only13letters", PASSWORD, PASSWORD + "x", PASSWORD, PASSWORD
        )

        result = self.vm.run(console(*answers))

        self.assert_succeeds(result)
        places = {
            "output": self.output(result),
            "console": self.vm.read("/dev/tty1"),
            "command lines": (self.vm.fake / "calls.log").read_text(),
        }
        for place, text in places.items():
            for secret in ("only13letters", "Wizard&Pass"):
                with self.subTest(place=place, secret=secret[:6]):
                    self.assertNotIn(secret, text)

    def test_ctrl_c_or_ctrl_backslash_on_the_console_does_not_stop_first_start(self) -> None:
        # The console is the script's terminal, so a key's signal goes to its
        # whole process group, the running command too: kill 0 does the same.
        # Ctrl+Z cannot be tested this way: the kernel drops SIGTSTP for a
        # process group whose parent is in another session.
        for signal in ("INT", "QUIT"):
            with self.subTest(signal=signal):
                self.vm.stub("netplan", f"kill -{signal} 0\n")
                for marker in self.vm.host_path(STATE).glob("*"):
                    marker.unlink()

                result = self.vm.run(console(*STATIC_ANSWERS.values()))

                self.assert_succeeds(result)
                self.assertEqual(ALL_MARKERS, self.vm.markers())

    def test_a_wizard_import_still_needs_the_ssh_key(self) -> None:
        self.vm.wizard({"admin.ssh_key": ""})

        result = self.vm.run(console(*STATIC_ANSWERS.values()))

        self.assert_fails_once(result, "Step 3 (wizard-fields)", "SSH public key", "vApp Options")
        self.assertNotIn("no wizard fields", self.output(result))


class BannerTests(FirstStartTestCase):
    def test_a_finished_first_start_shows_the_address_and_the_one_time_files(self) -> None:
        self.assert_succeeds(self.vm.run())
        self.vm.host_file("/var/lib/meho/first-login-passwords", "x\n")
        self.vm.host_file("/var/lib/meho/unseal-key", "x\n")

        banner = self.vm.banner()

        self.assertTrue(banner.startswith("MEHO appliance 0.36.0\n"), banner)
        self.assertIn("  Console:  https://meho.example.com/ui/\n", banner)
        self.assertIn("  Address:  \\4\n", banner)
        for path in ("first-login-passwords", "unseal-key", "bootloader-password"):
            self.assertIn(f"/var/lib/meho/{path}\n", banner)
        self.assertEqual(0o644, self.vm.mode("/etc/issue"))

    def test_a_deleted_one_time_file_is_no_longer_listed(self) -> None:
        self.assert_succeeds(self.vm.run())
        self.vm.host_path("/var/lib/meho/bootloader-password").unlink()

        self.assertNotIn("bootloader-password", self.vm.banner())

    def test_a_failed_step_shows_its_fail_line(self) -> None:
        self.vm.wizard({"admin.password": "short"})
        line = self.assert_fails_once(self.vm.run())

        banner = self.vm.banner()

        self.assertIn("First start stopped:\n", banner)
        self.assertIn(line, banner)
        self.assertNotIn("Console:", banner)
        self.assertNotIn("Log in as admin", banner)
        self.assertIn("The admin account does not exist yet.", banner)

    def test_a_failed_step_after_the_admin_account_exists_says_to_log_in(self) -> None:
        self.vm.answer("install_rc", "1")
        self.assert_fails_once(self.vm.run(), "Step 5 (core)")

        self.assertIn("Log in as admin to fix it", self.vm.banner())

    def test_a_backslash_in_the_fail_line_is_not_an_escape(self) -> None:
        self.vm.host_file(
            f"{STATE}/failed", "[FAIL] Step 3 (wizard-fields): The host name 'a\\4' is wrong.\n"
        )

        self.assertIn("'a\\\\4'", self.vm.banner())

    def test_an_unfinished_first_start_says_so(self) -> None:
        self.assertIn("First start has not finished.", self.vm.banner())

    def test_the_admin_password_expiry_warning(self) -> None:
        self.assert_succeeds(self.vm.run())
        today = int(time.time()) // 86400
        cases = {
            "must change at the first login": ("0", None),
            "never expires": (str(today), "99999"),
            "expires in 20 days": (str(today - 345), "365"),
            "expired": (str(today - 400), "365"),
            "expires in 100 days": (str(today - 265), "365"),
        }
        for name, (last_change, max_days) in cases.items():
            with self.subTest(case=name):
                self.vm.answer("shadow", f"admin:$y$hash:{last_change}:0:{max_days or ''}:7:::\n")

                banner = self.vm.banner()

                if name == "expires in 20 days":
                    self.assertIn("[WARN] The password of admin expires in 20 days.", banner)
                elif name == "expired":
                    self.assertIn("[WARN] The password of admin has expired.", banner)
                else:
                    self.assertNotIn("[WARN]", banner)


if __name__ == "__main__":
    unittest.main()
