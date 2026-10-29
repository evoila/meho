# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Exercise the unseal check's predicates without running cluster operations."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/check-openbao-unseal.sh"


class UnsealConditionTests(unittest.TestCase):
    def setUp(self) -> None:
        source = SCRIPT.read_text()
        self.helpers = source[
            source.index("uid() {") : source.index("key_file_gone() {")
        ]

    def predicate(self, name: str, **values: str) -> bool:
        # Only function definitions are loaded. All external reads are local
        # functions; kubectl, Helm, and a vault are never contacted.
        script = (
            self.helpers
            + """
uid() { printf '%s' "$CURRENT_UID"; }
server_restarts() { printf '%s' "$CURRENT_RESTARTS"; }
seal_rc() { printf '%s' "$ANSWER"; }
k() { printf '%s' "$READY_STATUS"; }
if "$PREDICATE"; then exit 0; else exit 1; fi
"""
        )
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "PREDICATE": name, "ANSWER": "0", **values},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode == 0

    def test_pod_uid_comparison_stays_literal(self) -> None:
        self.assertTrue(self.predicate("new_pod", CURRENT_UID="uid1", OLD_UID="uid*"))
        self.assertFalse(self.predicate("new_pod", CURRENT_UID="uid*", OLD_UID="uid*"))
        self.assertFalse(self.predicate("new_pod", CURRENT_UID="", OLD_UID="old"))

    def test_ready_requires_the_exact_status(self) -> None:
        self.assertTrue(self.predicate("ready", READY_STATUS="True"))
        self.assertFalse(self.predicate("ready", READY_STATUS="False"))
        self.assertFalse(self.predicate("ready", READY_STATUS=""))

    def test_restart_count_uses_decimal_and_requires_a_response(self) -> None:
        self.assertTrue(
            self.predicate(
                "restarted_and_answers", CURRENT_RESTARTS="009", RESTARTS="008"
            )
        )
        self.assertFalse(
            self.predicate("restarted_and_answers", CURRENT_RESTARTS="8", RESTARTS="9")
        )
        self.assertFalse(
            self.predicate("restarted_and_answers", CURRENT_RESTARTS="0", RESTARTS="0")
        )
        self.assertFalse(
            self.predicate(
                "restarted_and_answers", CURRENT_RESTARTS="9", RESTARTS="8", ANSWER="1"
            )
        )

    def test_invalid_restart_expression_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            expression = f'a[$(touch "{marker}")]'
            for current, previous in [
                (expression, "1"),
                ("2", expression),
                ("", "1"),
                ("x", "1"),
            ]:
                with self.subTest(current=current, previous=previous):
                    self.assertFalse(
                        self.predicate(
                            "restarted_and_answers",
                            CURRENT_RESTARTS=current,
                            RESTARTS=previous,
                        )
                    )
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
