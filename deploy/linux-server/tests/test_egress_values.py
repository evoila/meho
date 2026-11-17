#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of the outgoing rule of the MEHO pod that install.sh writes to
values-place.yaml on form 2: every address outside the built-in Kubernetes,
on every port, but not the pod range, the service range or the addresses of
this machine. Also the NetworkPolicy that the chart makes from it. The rule
to the console address through the Traefik pod is the chart's own
(networkPolicy.ingressControllerEgress in values-single-node.yaml)."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

from harness import ADDRESS_LINE, LINUX_SERVER, STUBS, InstallTestCase
from test_core_install import CHART_DIR, PLACE_VALUES, egress_values
from update_harness import NEW_VERSION

SECOND_ADDRESS_LINE = (
    "3: ens19    inet 192.168.40.7/24 brd 192.168.40.255 scope global ens19\\"
    "       valid_lft forever preferred_lft forever\n"
)
# RKE2's flannel.1 interface has an address of the pod range; kube-ipvs0 would
# have one of the service range.
RANGE_ADDRESS_LINES = (
    "5: flannel.1    inet 172.30.0.0/32 scope global flannel.1\\"
    "       valid_lft forever preferred_lft forever\n"
    "6: kube-ipvs0    inet 172.31.0.10/32 scope global kube-ipvs0\\"
    "       valid_lft forever preferred_lft forever\n"
)
NO_ADDRESS_WARNING = (
    "[WARN] No IPv4 address of this machine was found, so the network rule of MEHO cannot "
    "keep the ports of this machine closed to MEHO. MEHO still reaches the systems it manages."
)
OWN_RANGES = ("--cluster-cidr", "172.30.0.0/16", "--service-cidr", "172.31.0.0/20")
# The place values of an install from before the outgoing rules.
OLD_PLACE_VALUES = 'ingress:\n  host: "meho.example.com"\n'


class EgressValuesTests(InstallTestCase):
    def place_values(self) -> str:
        return self.machine.host_path(PLACE_VALUES).read_text()

    def test_meho_reaches_every_address_outside_the_cluster(self) -> None:
        result = self.machine.run()

        self.assert_succeeds(result)
        self.assertTrue(self.place_values().endswith(egress_values()), self.place_values())
        self.assertNotIn(NO_ADDRESS_WARNING, result.stderr)

    def test_the_ranges_of_the_install_stay_closed(self) -> None:
        self.assert_succeeds(self.machine.run(*OWN_RANGES))

        expected = egress_values("172.30.0.0/16", "172.31.0.0/20")
        self.assertTrue(self.place_values().endswith(expected), self.place_values())

    def test_every_address_of_this_machine_stays_closed(self) -> None:
        self.machine.answer("ip_addr_global", SECOND_ADDRESS_LINE + ADDRESS_LINE + ADDRESS_LINE)

        self.assert_succeeds(self.machine.run())

        expected = egress_values(addresses=("10.0.0.5", "192.168.40.7"))
        self.assertTrue(self.place_values().endswith(expected), self.place_values())


class FromDiskEgressTests(InstallTestCase):
    """--from-disk runs no DNS check, so it reaches the rules also when the
    addresses of this machine cannot be read."""

    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.machine.download / "rke2")
        shutil.rmtree(self.machine.download / "images")
        self.machine.install_rke2_files()

    def assert_rules_without_addresses(self) -> None:
        result = self.machine.run("--from-disk", "--hostname", "new-name.example.org")

        self.assert_succeeds(result)
        self.assertIn(NO_ADDRESS_WARNING, result.stderr)
        place_values = self.machine.host_path(PLACE_VALUES).read_text()
        self.assertTrue(place_values.endswith(egress_values(addresses=())), place_values)

    def test_a_machine_without_an_address_gets_the_rules_and_a_warning(self) -> None:
        self.machine.answer("ip_addr_global", "")

        self.assert_rules_without_addresses()

    def test_a_failed_read_of_the_addresses_gets_the_rules_and_a_warning(self) -> None:
        self.machine.add_stub(
            "ip",
            STUBS["ip"].replace(
                '"-4 -o addr show scope global") cat "$MEHO_FAKE/ip_addr_global" ;;',
                '"-4 -o addr show scope global") exit 1 ;;',
            ),
        )

        self.assert_rules_without_addresses()


class UpdateEgressTests(InstallTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assert_succeeds(self.machine.run(*OWN_RANGES))
        self.machine.mark_installed()
        self.machine.set_download_version(NEW_VERSION)

    def place_values_after_update(self) -> str:
        self.assert_succeeds(self.machine.run("--confirm-backup"))
        return self.machine.host_path(PLACE_VALUES).read_text()

    def test_the_update_writes_the_rules_with_the_ranges_of_the_install(self) -> None:
        self.machine.host_path(PLACE_VALUES).write_text(OLD_PLACE_VALUES)

        place_values = self.place_values_after_update()

        expected = egress_values("172.30.0.0/16", "172.31.0.0/20")
        self.assertTrue(place_values.endswith(expected), place_values)

    def test_an_address_inside_the_two_ranges_is_left_out(self) -> None:
        self.machine.answer("ip_addr_global", ADDRESS_LINE + RANGE_ADDRESS_LINES)

        place_values = self.place_values_after_update()

        expected = egress_values("172.30.0.0/16", "172.31.0.0/20")
        self.assertTrue(place_values.endswith(expected), place_values)


@unittest.skipUnless(
    shutil.which("helm") or os.environ.get("MEHO_REQUIRE_HELM") == "1",
    "helm is not on PATH; set MEHO_REQUIRE_HELM=1 to fail instead",
)
class EgressChartRenderTests(InstallTestCase):
    def networkpolicy_of_meho(self) -> str:
        """The NetworkPolicy of the MEHO pod, from helm template with the
        two values files of install_core."""
        self.assert_succeeds(self.machine.run(*OWN_RANGES))
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
        policies = [
            document
            for document in result.stdout.split("\n---\n")
            if "kind: NetworkPolicy\n" in document and "\n  name: meho\n" in document
        ]
        self.assertEqual(1, len(policies), result.stdout)
        return policies[0]

    def test_the_networkpolicy_of_meho_opens_every_address_outside_the_cluster(self) -> None:
        egress = self.networkpolicy_of_meho().split("\n  egress:\n", 1)[1]

        self.assertIn(
            "    - to:\n"
            "      - ipBlock:\n"
            "          cidr: 0.0.0.0/0\n"
            "          except:\n"
            "          - 172.30.0.0/16\n"
            "          - 172.31.0.0/20\n"
            "          - 10.0.0.5/32",
            egress,
        )


if __name__ == "__main__":
    unittest.main()
