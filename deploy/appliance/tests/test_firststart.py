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
        self.assertIn("useradd --create-home --shell /bin/bash --groups sudo admin", self.vm.calls())
        self.assertIn("passwd --expire admin", self.vm.calls())
        self.assertEqual(f"{SSH_KEY}\n", self.vm.read("/home/admin/.ssh/authorized_keys"))
        self.assertEqual(0o600, self.vm.mode("/home/admin/.ssh/authorized_keys"))
        self.assertEqual(0o700, self.vm.mode("/home/admin/.ssh"))
        self.assertEqual(["meho-host admin-network 10.0.50.0/24"], self.vm.calls("meho-host"))
        self.assertEqual(["install.sh --from-disk --hostname meho.example.com"], self.vm.calls("install.sh"))
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

    def test_no_wizard_fields_stops_at_step_3_and_changes_nothing(self) -> None:
        self.vm.no_answer("guestinfo-ovfenv")

        result = self.vm.run()

        line = self.assert_fails_once(result, "Step 3 (wizard-fields)", "no wizard fields", "ovftool")
        self.assertEqual("ubuntu\n", self.vm.read("/etc/hostname"))
        self.assertFalse(self.vm.host_path("/etc/netplan/50-meho.yaml").exists())
        self.assertEqual(ALL_MARKERS[:2], self.vm.markers())
        self.assertEqual(f"{line}\n", self.vm.read(f"{STATE}/failed"))

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
            "admin.ssh_key two lines": ({"admin.ssh_key": f"{SSH_KEY}\n{SSH_KEY}"}, "SSH public key"),
            "admin.password line break": ({"admin.password": "fourteen-chars\nmore"}, "line break"),
            "cluster.join half": ({"cluster.join.address": "10.0.0.20"}, "Give both"),
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

    def test_the_join_fields_go_to_install_sh_and_the_token_file_goes_away(self) -> None:
        self.vm.wizard({"cluster.join.address": "10.0.0.20", "cluster.join.token": JOIN_TOKEN})

        self.assert_succeeds(self.vm.run())

        self.assertEqual(
            [
                "install.sh --from-disk --hostname meho.example.com "
                "--join-address 10.0.0.20 --join-token-file /run/meho-join-token"
            ],
            self.vm.calls("install.sh"),
        )
        self.assertEqual(f"{JOIN_TOKEN}\n", (self.vm.fake / "join_token_seen").read_text())
        self.assertFalse(self.vm.host_path("/run/meho-join-token").exists())

    def test_no_secret_reaches_the_output_the_console_or_a_command_line(self) -> None:
        self.vm.wizard({"cluster.join.address": "10.0.0.20", "cluster.join.token": JOIN_TOKEN})

        result = self.vm.run()

        self.assert_succeeds(result)
        bootloader_password = self.vm.read("/var/lib/meho/bootloader-password").strip()
        places = {
            "output": self.output(result),
            "console": self.vm.read("/dev/tty1"),
            "command lines": (self.vm.fake / "calls.log").read_text(),
        }
        for place, text in places.items():
            for secret in (PASSWORD, "Wizard&amp;Pass", JOIN_TOKEN, bootloader_password):
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

    def test_an_unknown_10_linux_stops_step_6(self) -> None:
        self.vm.host_file("/etc/grub.d/10_linux", 'CLASS="--class something-else"\n')

        result = self.vm.run()

        self.assert_fails_once(result, "Step 6 (bootloader-password)", "CLASS line")
        self.assertEqual([], self.vm.calls("update-grub"))

    def test_a_partition_that_already_fills_the_disk_is_fine(self) -> None:
        for rc, succeeds in (("0", True), ("1", True), ("2", False)):
            with self.subTest(growpart_rc=rc):
                self.vm.answer("growpart_rc", rc)
                for marker in self.vm.host_path(STATE).glob("*") if self.vm.host_path(STATE).is_dir() else []:
                    marker.unlink()
                self.vm.host_path("/var/lib/meho/firststart.done").unlink(missing_ok=True)

                result = self.vm.run()

                if succeeds:
                    self.assert_succeeds(result)
                else:
                    self.assert_fails_once(result, "Step 2 (disk)", "growpart")

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
        self.assertEqual(["install.sh --from-disk --hostname meho2.example.com"], self.vm.calls("install.sh"))
        self.assertEqual(ALL_MARKERS, self.vm.markers())
        self.assertFalse(self.vm.host_path(f"{STATE}/failed").exists())

    def test_a_failed_meho_host_stops_step_3(self) -> None:
        self.vm.answer("meho_host_rc", "1")

        result = self.vm.run()

        self.assert_fails_once(result, "Step 3 (wizard-fields)", "exit code 1")
        self.assertEqual(ALL_MARKERS[:2], self.vm.markers())


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

    def test_a_backslash_in_the_fail_line_is_not_an_escape(self) -> None:
        self.vm.host_file(f"{STATE}/failed", "[FAIL] Step 3 (wizard-fields): The host name 'a\\4' is wrong.\n")

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
