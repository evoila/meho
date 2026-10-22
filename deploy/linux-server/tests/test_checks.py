#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of step 1 of install.sh: check the machine.

Every check passes on a healthy machine. Each check also has a failing case:
it prints exactly one [FAIL] line with the fix, exits 1 and changes nothing.
"""

from __future__ import annotations

import unittest

from harness import ADDRESS_LINE, InstallTestCase

PASSING_LINES = {
    "root": "[ok]   running as root",
    "operating system": "[ok]   Ubuntu 24.04.3 LTS on x86-64 with systemd",
    "download": "[ok]   the download is complete",
    "processors": "[ok]   4 processors (at least 4)",
    "memory": "[ok]   15.5 GiB of memory (the machine needs at least 8 GB)",
    "disk": "[ok]   200 GB free for /var/lib (at least 80 GB)",
    "other runtime": "[ok]   no other Kubernetes or container runtime",
    "ports": (
        "[ok]   the ports RKE2 needs are free: "
        "TCP 80, 443, 6443, 9345, 10250, 2379, 2380, 2381, 9099; UDP 8472"
    ),
    "clock": "[ok]   the clock is synchronised",
    "host name": "[ok]   meho.example.com is a fully qualified name",
    "DNS": "[ok]   meho.example.com resolves through DNS to 10.0.0.5",
    "address": "[ok]   10.0.0.5 is an address of this machine",
    "ranges": (
        "[ok]   the pod range 10.42.0.0/16 and the service range 10.43.0.0/16"
        " are free on this machine"
    ),
}


class PassingChecksTests(InstallTestCase):
    def test_every_check_passes_on_a_healthy_machine(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        for check, line in PASSING_LINES.items():
            with self.subTest(check=check):
                self.assertIn(line, result.stdout)

    def test_another_linux_with_systemd_gets_a_warning_and_the_install_goes_on(self) -> None:
        self.machine.host_file(
            "/etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 12"\nID=debian\nVERSION_ID="12"\n'
        )

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn("[WARN] Debian GNU/Linux 12 is not tested with MEHO.", result.stderr)

    def test_a_machine_sold_as_8_gb_passes(self) -> None:
        self.machine.host_file("/proc/meminfo", "MemTotal:        7841792 kB\n")

        self.assert_succeeds(self.machine.run())

    def test_the_host_name_option_is_used_in_lower_case(self) -> None:
        result = self.machine.run("--hostname", "MEHO.Example.COM")

        self.assert_succeeds(result)
        self.assertIn("[ok]   meho.example.com is a fully qualified name", result.stdout)

    def test_an_alias_in_the_dns_answer_is_not_read_as_an_address(self) -> None:
        self.machine.answer(
            "resolvectl_out",
            "meho.example.com: 10.0.0.5                     -- link: ens18\n"
            "                  (lb.10.9.9.9.example.net)\n",
        )

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn("resolves through DNS to 10.0.0.5\n", result.stdout)

    def test_without_systemd_resolved_getent_asks_only_dns(self) -> None:
        self.machine.answer("active_units", "")

        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertIn("getent -s dns ahostsv4 meho.example.com", self.machine.calls())

    def test_the_public_address_option_accepts_a_translated_address(self) -> None:
        self.machine.answer("resolvectl_out", "meho.example.com: 192.0.2.50\n")

        result = self.machine.run("--public-address", "192.0.2.50")

        self.assert_succeeds(result)
        self.assertIn(
            "[ok]   meho.example.com points to the address given with --public-address",
            result.stdout,
        )

    def test_custom_ranges_that_are_free_pass(self) -> None:
        result = self.machine.run(
            "--cluster-cidr", "172.30.0.0/16", "--service-cidr", "172.31.0.0/16"
        )

        self.assert_succeeds(result)
        self.assertIn(
            "the pod range 172.30.0.0/16 and the service range 172.31.0.0/16 are free",
            result.stdout,
        )


class FailingChecksTests(InstallTestCase):
    def tearDown(self) -> None:
        self.assertEqual([], self.written_files(), "a failed check must not change the machine")

    def test_not_root(self) -> None:
        self.machine.answer("uid", "1000")

        self.assert_fails_once(self.machine.run(), "does not run as root", "sudo ./install.sh")

    def test_not_x86_64(self) -> None:
        self.machine.answer("arch", "aarch64")

        self.assert_fails_once(
            self.machine.run(), "This machine is aarch64", "use an x86-64 machine"
        )

    def test_no_systemd(self) -> None:
        self.machine.host_path("/run/systemd/system").rmdir()

        self.assert_fails_once(self.machine.run(), "does not run systemd", "Ubuntu 24.04 LTS")

    def test_incomplete_download(self) -> None:
        (self.machine.download / "rke2" / "sha256sum-amd64.txt").unlink()
        (self.machine.download / "images" / "meho-images.linux-amd64.tar.zst").unlink()

        self.assert_fails_once(
            self.machine.run(),
            "Missing: rke2/sha256sum-amd64.txt, images/meho-images.linux-amd64.tar.zst.",
            "unpack the whole download again",
        )

    def test_too_few_processors(self) -> None:
        self.machine.answer("cpus", "2")

        self.assert_fails_once(self.machine.run(), "has 2 processors", "at least 4 processors")

    def test_too_little_memory(self) -> None:
        self.machine.host_file("/proc/meminfo", "MemTotal:        5952276 kB\n")

        self.assert_fails_once(
            self.machine.run(), "has 5.7 GiB of memory", "at least 8 GB of memory"
        )

    def test_too_little_free_disk(self) -> None:
        self.machine.answer("disk_free_kib", str(50 * 10**9 // 1024))

        self.assert_fails_once(
            self.machine.run(), "/var/lib has 50 GB free", "make the file system of /var/lib larger"
        )

    def test_another_runtime_on_the_path(self) -> None:
        self.machine.add_stub("containerd")

        self.assert_fails_once(
            self.machine.run(), "runtime is on this machine: containerd.", "remove containerd"
        )

    def test_another_runtime_that_runs(self) -> None:
        self.machine.answer("processes", "dockerd\nk3s\n")

        self.assert_fails_once(self.machine.run(), "on this machine: k3s, dockerd.")

    def test_ports_in_use(self) -> None:
        self.machine.answer(
            "ss_tcp", "LISTEN 0 511 0.0.0.0:443 0.0.0.0:*\nLISTEN 0 4096 [::]:6443 [::]:*\n"
        )
        self.machine.answer("ss_udp", "UNCONN 0 0 0.0.0.0:8472 0.0.0.0:*\n")

        self.assert_fails_once(
            self.machine.run(), "in use: 443/tcp, 6443/tcp, 8472/udp.", "ss -tulpn"
        )

    def test_clock_not_synchronised(self) -> None:
        self.machine.answer("ntp_synchronized", "no")

        self.assert_fails_once(
            self.machine.run(), "is not synchronised", "timedatectl set-ntp true"
        )

    def test_host_name_that_is_not_fully_qualified(self) -> None:
        self.machine.answer("hostname_fqdn", "meho")

        self.assert_fails_once(
            self.machine.run(), "'meho' is not a fully qualified DNS name", "--hostname"
        )

    def test_host_name_that_is_an_address(self) -> None:
        self.assert_fails_once(
            self.machine.run("--hostname", "10.0.0.5"), "is not a fully qualified DNS name"
        )

    def test_host_name_that_resolves_only_through_etc_hosts(self) -> None:
        self.machine.host_file("/etc/hosts", "10.0.0.5 meho.example.com meho\n")
        self.machine.answer(
            "resolvectl_out", "meho.example.com: Name 'meho.example.com' not found\n"
        )
        self.machine.answer("resolvectl_rc", "1")

        result = self.machine.run()

        self.assert_fails_once(
            result,
            "does not resolve through DNS. An entry in /etc/hosts does not count.",
            "create a DNS A record for meho.example.com",
        )
        self.assertIn(
            "resolvectl query --legend=no --synthesize=no --cache=no -4 meho.example.com",
            self.machine.calls(),
        )

    def test_host_name_not_in_dns_without_systemd_resolved(self) -> None:
        self.machine.answer("active_units", "")
        self.machine.answer("getent_out", "")
        self.machine.answer("getent_rc", "2")

        self.assert_fails_once(self.machine.run(), "does not resolve through DNS")

    def test_host_name_that_points_to_another_machine(self) -> None:
        self.machine.answer("resolvectl_out", "meho.example.com: 192.0.2.50\n")

        self.assert_fails_once(
            self.machine.run(),
            "resolves to 192.0.2.50, which is not an address of this machine.",
            "--public-address",
        )

    def test_host_name_that_does_not_point_to_the_public_address(self) -> None:
        self.machine.answer("resolvectl_out", "meho.example.com: 192.0.2.50\n")

        self.assert_fails_once(
            self.machine.run("--public-address", "192.0.2.60"),
            "not to the address given with --public-address (192.0.2.60)",
        )

    def test_pod_range_that_overlaps_a_route(self) -> None:
        self.machine.answer(
            "ip_route", "default via 10.0.0.1 dev ens18\n10.42.7.0/24 via 10.0.0.1 dev ens18\n"
        )

        self.assert_fails_once(
            self.machine.run(), "The pod range 10.42.0.0/16 overlaps 10.42.7.0/24", "--cluster-cidr"
        )

    def test_service_range_that_overlaps_an_address(self) -> None:
        self.machine.answer(
            "ip_addr", ADDRESS_LINE + "3: ens19    inet 10.43.0.10/24 scope global ens19\\\n"
        )

        self.assert_fails_once(
            self.machine.run(),
            "The service range 10.43.0.0/16 overlaps 10.43.0.10/24",
            "--service-cidr",
        )

    def test_ranges_that_overlap_each_other(self) -> None:
        result = self.machine.run(
            "--cluster-cidr", "10.42.0.0/16", "--service-cidr", "10.42.128.0/17"
        )

        self.assert_fails_once(
            result, "and the service range 10.42.128.0/17 overlap.", "two separate ranges"
        )


if __name__ == "__main__":
    unittest.main()
