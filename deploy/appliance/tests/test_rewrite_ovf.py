#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of rewrite-ovf.sh: our product section replaces the cloud image's,
the two OVF transports are declared, and the result is well-formed XML.
The fixture is the descriptor of the Ubuntu 24.04 cloud image OVA
(release 20261017), unchanged."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

APPLIANCE = Path(__file__).resolve().parents[1]
REWRITE = APPLIANCE / "rewrite-ovf.sh"
SECTION = APPLIANCE / "product-section.xml"
FIXTURE = APPLIANCE / "tests" / "fixtures" / "ubuntu-noble-24.04-cloudimg.ovf"
OVF = "{http://schemas.dmtf.org/ovf/envelope/1}"

WIZARD_KEYS = [
    "hostname",
    "net.ntp",
    "net.mode",
    "net.ip",
    "net.gateway",
    "net.dns",
    "net.admin_network",
    "admin.password",
    "admin.ssh_key",
    "cluster.join.address",
    "cluster.join.token",
    "cluster.vip",
]


class RewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="meho-ovf-test-")
        self.addCleanup(self.tempdir.cleanup)
        self.ovf = Path(self.tempdir.name) / "appliance.ovf"
        shutil.copy2(FIXTURE, self.ovf)

    def run_rewrite(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = (
            [str(REWRITE), *args] if args else [str(REWRITE), str(self.ovf), str(SECTION), "0.36.0"]
        )
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)

    def test_our_section_replaces_the_cloud_init_section(self) -> None:
        result = self.run_rewrite()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        tree = ET.parse(self.ovf)
        sections = tree.findall(f".//{OVF}ProductSection")
        self.assertEqual(1, len(sections))
        keys = [prop.get(f"{OVF}key") for prop in sections[0].findall(f"{OVF}Property")]
        self.assertEqual(WIZARD_KEYS, keys)
        self.assertEqual("0.36.0", sections[0].find(f"{OVF}Version").text)
        self.assertEqual("MEHO appliance", sections[0].find(f"{OVF}Product").text)
        self.assertNotIn("Cloud-Init", self.ovf.read_text())
        self.assertNotIn("@VERSION@", self.ovf.read_text())

    def test_passwords_are_hidden_and_the_mode_has_two_values(self) -> None:
        self.run_rewrite()

        props = {p.get(f"{OVF}key"): p for p in ET.parse(self.ovf).iter(f"{OVF}Property")}
        for key in ("admin.password", "cluster.join.token"):
            with self.subTest(key=key):
                self.assertEqual("true", props[key].get(f"{OVF}password"))
        self.assertEqual("MinLen(14)", props["admin.password"].get(f"{OVF}qualifiers"))
        self.assertEqual('ValueMap{"static","dhcp"}', props["net.mode"].get(f"{OVF}qualifiers"))
        self.assertEqual("static", props["net.mode"].get(f"{OVF}value"))
        for key, prop in props.items():
            with self.subTest(key=key):
                self.assertEqual("true", prop.get(f"{OVF}userConfigurable"))

    def test_both_transports_are_declared_on_the_one_hardware_section(self) -> None:
        self.run_rewrite()

        hardware = ET.parse(self.ovf).findall(f".//{OVF}VirtualHardwareSection")
        self.assertEqual(1, len(hardware))
        self.assertEqual("com.vmware.guestInfo iso", hardware[0].get(f"{OVF}transport"))
        # Everything else is as the export wrote it.
        before = ET.parse(FIXTURE).find(f".//{OVF}VirtualHardwareSection")
        self.assertEqual(len(list(before)), len(list(hardware[0])))
        self.assertEqual(
            ET.parse(FIXTURE).find(f".//{OVF}DiskSection/{OVF}Disk").attrib,
            ET.parse(self.ovf).find(f".//{OVF}DiskSection/{OVF}Disk").attrib,
        )

    def test_a_file_without_a_hardware_section_is_refused_unchanged(self) -> None:
        self.ovf.write_text("<Envelope><VirtualSystem/></Envelope>\n")

        result = self.run_rewrite()

        self.assertEqual(1, result.returncode)
        self.assertIn("[FAIL]", result.stderr)
        self.assertEqual("<Envelope><VirtualSystem/></Envelope>\n", self.ovf.read_text())

    def test_wrong_usage_is_exit_2(self) -> None:
        result = self.run_rewrite(str(self.ovf))

        self.assertEqual(2, result.returncode)
        self.assertIn("three arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
