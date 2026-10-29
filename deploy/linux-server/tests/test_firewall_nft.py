#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Checks of the MEHO firewall file with the real nft and the real kernel.

install.sh runs on the fake machine and writes the file. Then the real nft
checks it ("nft -c", check only) and loads it, in a new user and network
namespace (unshare --map-root-user --net). The namespace has its own empty
firewall, so the machine that runs the tests keeps its own rules. One test
then changes the SSH rule with meho-host admin-network, which checks and
loads the new file with the real nft too. Another reads the loaded table
with firewall_state, so its check of the chain prerouting matches the real
output of nft.

One test also loads the variant of the MEHO appliance, where the chains
input, forward and output have policy drop, and opens a TCP connection over
lo with it. Programs on the machine (the kubelet, the API server, etcd)
reach each other that way.

The tests need nft, unshare, user namespaces, and a kernel with the
nftables parts the rules use (ct and fib for the inet family). Without them
the tests are skipped, and the reason says what is missing. The CI job sets
MEHO_REQUIRE_NFT_CHECK=1, which turns that skip into a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import LINUX_SERVER, InstallTestCase

RULES = "/etc/meho/firewall.nft"
REQUIRED = os.environ.get("MEHO_REQUIRE_NFT_CHECK") == "1"
APPLIANCE_CHAINS = ("input", "forward", "output")

# The smallest rule set with the kernel parts the MEHO rules need.
PROBE = """\
table inet meho_probe {
    chain prerouting {
        type filter hook prerouting priority mangle; policy accept;
        ct state established accept
        fib daddr type local accept
    }
}
"""

# Stand-ins for the tables that kube-proxy and Canal write next to ours.
OTHER_TABLES = """\
table ip filter {
    chain INPUT {
        type filter hook input priority filter; policy accept;
        counter
    }
}
table ip6 filter {
    chain FORWARD {
        type filter hook forward priority filter; policy accept;
    }
}
"""

# Runs in the namespace: turn lo on (a new network namespace starts with lo
# down; SIOCSIFFLAGS from netdevice(7)), then open a TCP connection to
# 127.0.0.1 and send two bytes. A dropped packet makes the connect time out.
LOOPBACK_CONNECTION = """\
import fcntl
import socket
import struct

SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1
with socket.socket() as control:
    request = struct.pack("16sH22x", b"lo", 0)
    flags = struct.unpack_from("16sH", fcntl.ioctl(control, SIOCGIFFLAGS, request))[1]
    fcntl.ioctl(control, SIOCSIFFLAGS, struct.pack("16sH22x", b"lo", flags | IFF_UP))
with socket.create_server(("127.0.0.1", 0)) as server:
    with socket.create_connection(server.getsockname(), timeout=5) as client:
        peer, _ = server.accept()
        with peer:
            client.sendall(b"ok")
            assert peer.recv(2) == b"ok"
"""


def in_namespace(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["unshare", "--map-root-user", "--net", *command],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def why_nft_cannot_run() -> str | None:
    """None when this machine can check nftables rules, else the reason."""
    for tool in ("nft", "unshare"):
        if shutil.which(tool) is None:
            return f"'{tool}' is not on PATH"
    with tempfile.NamedTemporaryFile("w", suffix=".nft", delete=False) as probe:
        probe.write(PROBE)
    try:
        result = in_namespace("nft", "-c", "-f", probe.name)
    finally:
        os.unlink(probe.name)
    if result.returncode != 0:
        return "nft cannot check ct and fib rules in a user and network namespace here: " + (
            result.stderr.strip() or f"exit code {result.returncode}"
        )
    return None


class NftCheckTests(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        reason = why_nft_cannot_run()
        if reason is not None:
            if REQUIRED:
                self.fail(f"MEHO_REQUIRE_NFT_CHECK=1, but {reason}")
            self.skipTest(reason)

    def rules_file(self, *args: str, session: str | None = "10.0.0.9 50022 10.0.0.5 22") -> Path:
        self.machine.ssh_connection = session
        result = self.machine.run(*args)
        self.assert_succeeds(result)
        return self.machine.host_path(RULES)

    def test_nft_accepts_the_file_for_each_admin_network(self) -> None:
        cases = {
            "a range": (("--admin-network", "10.0.0.0/24"), "10.0.0.9 50022 10.0.0.5 22"),
            "one address": (("--admin-network", "10.0.0.9"), "10.0.0.9 50022 10.0.0.5 22"),
            "the SSH session": ((), "10.0.0.9 50022 10.0.0.5 22"),
            "none": (("--admin-network", "none"), None),
            "custom ranges": (
                ("--cluster-cidr", "172.30.0.0/16", "--service-cidr", "172.31.0.0/20"),
                "10.0.0.9 50022 10.0.0.5 22",
            ),
        }
        for case, (args, session) in cases.items():
            with self.subTest(case=case):
                rules = self.rules_file(*args, session=session)

                result = in_namespace("nft", "-c", "-f", str(rules))

                self.assertEqual(0, result.returncode, result.stderr)

    def test_nft_accepts_the_file_after_meho_host_admin_network(self) -> None:
        # meho-host runs the real nft -c -f and nft -f in the namespace, where
        # the mapped user is root. No SSH session, so it never asks.
        rules = self.rules_file("--admin-network", "10.0.0.0/24")
        installed = rules.read_text()
        cases = {"10.0.60.0/24": "elements = { 10.0.60.0/24 }", "none": None}
        for value, elements in cases.items():
            with self.subTest(admin_network=value):
                rules.write_text(installed)

                result = in_namespace(
                    "sh",
                    "-c",
                    'unset SSH_CLIENT SSH_CONNECTION; MEHO_TEST_ROOT="$1" "$2" admin-network "$3"'
                    " && nft list set inet meho admin_network",
                    "sh",
                    str(self.machine.root),
                    str(LINUX_SERVER / "meho-host"),
                    value,
                )

                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                if elements is None:
                    self.assertNotIn("elements", result.stdout)
                else:
                    self.assertIn(elements, result.stdout)

    def test_firewall_state_reads_the_real_nft_output(self) -> None:
        # firewall_state runs with the real nft in the namespace. A stub
        # systemctl says that meho-firewall.service is enabled.
        rules = self.rules_file("--admin-network", "10.0.0.0/24")
        installed = rules.read_text()
        stubs = self.machine.home / "state-stubs"
        stubs.mkdir()
        systemctl = stubs / "systemctl"
        systemctl.write_text('#!/bin/sh\n[ "$1 $3" = "is-enabled meho-firewall.service" ]\n')
        systemctl.chmod(0o755)
        snippet = (
            f'source "{LINUX_SERVER}/lib/common.sh"; source "{LINUX_SERVER}/lib/net.sh"; '
            f'source "{LINUX_SERVER}/lib/firewall.sh"; firewall_state'
        )
        no_drop = "off: the table inet meho has no chain prerouting with policy drop"
        cases = {
            "the installed file": (installed, 0, "on\n"),
            "policy accept": (installed.replace("policy drop;", "policy accept;"), 1, no_drop),
            "not loaded": (None, 1, "off: the table inet meho is not loaded\n"),
        }
        for case, (content, returncode, expected) in cases.items():
            with self.subTest(case=case):
                load = ""
                if content is not None:
                    rules.write_text(content)
                    load = f'nft -f "{rules}" && '
                result = in_namespace(
                    "env",
                    f"PATH={stubs}:{os.environ.get('PATH', '')}",
                    "bash",
                    "-c",
                    load + snippet,
                )

                self.assertEqual(returncode, result.returncode, result.stdout + result.stderr)
                self.assertTrue(result.stdout.startswith(expected), result.stdout)

    def test_loading_twice_replaces_only_the_meho_table(self) -> None:
        rules = self.rules_file("--admin-network", "10.0.0.0/24")
        other = self.machine.home / "other-tables.nft"
        other.write_text(OTHER_TABLES)

        result = in_namespace(
            "sh",
            "-c",
            'nft -f "$1" && nft -f "$2" && nft -f "$2"'
            " && nft list tables && nft list table inet meho",
            "sh",
            str(other),
            str(rules),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        tables = [line for line in result.stdout.splitlines() if line.startswith("table ")]
        self.assertEqual(
            ["table ip filter", "table ip6 filter", "table inet meho", "table inet meho {"], tables
        )
        self.assertIn("type filter hook prerouting priority mangle; policy drop;", result.stdout)
        for hook in APPLIANCE_CHAINS:
            self.assertIn(f"type filter hook {hook} priority filter; policy accept;", result.stdout)
        self.assertIn("elements = { 10.0.0.0/24 }", result.stdout)

    def test_the_appliance_variant_loads_and_lets_lo_through(self) -> None:
        rules = self.rules_file("--admin-network", "10.0.0.0/24").read_text()
        self.assertEqual(len(APPLIANCE_CHAINS), rules.count("policy accept;"))
        appliance = self.machine.home / "appliance.nft"
        appliance.write_text(rules.replace("policy accept;", "policy drop;"))

        result = in_namespace(
            "sh",
            "-c",
            'nft -c -f "$1" && nft -f "$1" && "$2" -I -c "$3" && nft list table inet meho',
            "sh",
            str(appliance),
            sys.executable,
            LOOPBACK_CONNECTION,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("type filter hook prerouting priority mangle; policy drop;", result.stdout)
        for hook in APPLIANCE_CHAINS:
            self.assertIn(f"type filter hook {hook} priority filter; policy drop;", result.stdout)


if __name__ == "__main__":
    unittest.main()
