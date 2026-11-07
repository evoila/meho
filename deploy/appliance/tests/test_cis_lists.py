#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests that the CIS tailoring and its two lists agree: every rule the
scan leaves out has its reason in not-applicable.md and the other way round,
and no exception is also "not applicable"."""

from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

CIS = Path(__file__).resolve().parents[1] / "cis"
XCCDF = "{http://checklists.nist.gov/xccdf/1.2}"
PREFIX = "xccdf_org.ssgproject.content_rule_"
SCAN = "xccdf_com.evoila.meho_profile_appliance"
REMEDIATE = "xccdf_com.evoila.meho_profile_appliance_remediate"
CIS_PROFILE = "xccdf_org.ssgproject.content_profile_cis_level1_server"
# The first column of a table row, as harden.sh reads it.
RULE_ROW = re.compile(r"^\| `([A-Za-z0-9_.-]+)` \|", re.MULTILINE)


def rules_in(markdown: str) -> list[str]:
    return RULE_ROW.findall((CIS / markdown).read_text())


def profiles() -> dict[str, ET.Element]:
    root = ET.parse(CIS / "tailoring.xml").getroot()
    return {profile.get("id"): profile for profile in root.iter(f"{XCCDF}Profile")}


def left_out(profile: ET.Element) -> list[str]:
    rules = []
    for select in profile.iter(f"{XCCDF}select"):
        if select.get("selected") == "false":
            idref = select.get("idref")
            assert idref.startswith(PREFIX), idref
            rules.append(idref.removeprefix(PREFIX))
    return rules


class CisListTests(unittest.TestCase):
    def test_the_scan_extends_cis_level_1_server_and_the_fixes_extend_the_scan(self) -> None:
        found = profiles()

        self.assertEqual({SCAN, REMEDIATE}, set(found))
        self.assertEqual(CIS_PROFILE, found[SCAN].get("extends"))
        self.assertEqual(SCAN, found[REMEDIATE].get("extends"))

    def test_each_rule_the_scan_leaves_out_has_its_reason(self) -> None:
        scan = left_out(profiles()[SCAN])

        self.assertEqual(len(scan), len(set(scan)))
        self.assertEqual(sorted(scan), sorted(rules_in("not-applicable.md")))

    def test_the_fixes_leave_out_only_rules_the_scan_still_checks(self) -> None:
        scan = set(left_out(profiles()[SCAN]))
        fixes_only = left_out(profiles()[REMEDIATE])

        self.assertTrue(fixes_only)
        self.assertEqual(set(), scan & set(fixes_only))

    def test_an_exception_is_never_also_not_applicable(self) -> None:
        exceptions = rules_in("exceptions.md")

        self.assertTrue(exceptions)
        self.assertEqual(len(exceptions), len(set(exceptions)))
        self.assertEqual(set(), set(exceptions) & set(rules_in("not-applicable.md")))


if __name__ == "__main__":
    unittest.main()
