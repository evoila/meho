#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Checks of the MEHO firewall file with the real nft and the real kernel.

install.sh runs on the fake machine and writes the file. Then the real nft
checks it ("nft -c", check only) and loads it, in a new user and network
namespace (unshare --map-root-user --net). The namespace has its own empty
firewall, so the machine that runs the tests keeps its own rules.

The tests need nft, unshare, user namespaces, and a kernel with the
nftables parts the rules use (ct and fib for the inet family). Without them
the tests are skipped, and the reason says what is missing. The CI job sets
MEHO_REQUIRE_NFT_CHECK=1, which turns that skip into a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import InstallTestCase

RULES = "/etc/meho/firewall.nft"
REQUIRED = os.environ.get("MEHO_REQUIRE_NFT_CHECK") == "1"

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
        self.assertIn("elements = { 10.0.0.0/24 }", result.stdout)


if __name__ == "__main__":
    unittest.main()
