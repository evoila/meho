# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test the custom policy check script in its dry mode, against recorded answers.

No install is contacted: every answer comes from a folder of recorded
answers that the test writes. The script needs ``jq``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/acceptance/custom-policy-check.sh"

PROBE_COUNT = 16
CHECK_ORDER = ["1", "2", "3", "4", "5", "6", "8", "9", "10", "11", "7"]


def audit_id(step: str) -> str:
    """A fixed audit row id per step, so the output can be checked."""
    number = sum(ord(c) * (i + 1) for i, c in enumerate(step))
    return f"00000000-0000-4000-8000-{number:012d}"


def call(step: str, status: str, *, request: str = "", code: str = "") -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if request:
        extras["approval_request_id"] = request
    if code:
        extras["error_code"] = code
    return {"code": 200, "body": {"status": status, "audit_id": audit_id(step), "extras": extras}}


def row(step: str, rule: str, kind: str, status: str = "awaiting_approval") -> dict[str, Any]:
    return {
        "code": 200,
        "body": {
            "id": audit_id(step),
            "policy_rule": rule,
            "caller_kind": kind,
            "result_status": status,
        },
    }


def rows(step: str, path: str) -> dict[str, Any]:
    return {"code": 200, "body": {"rows": [{"id": audit_id(step), "path": path}]}}


def request(status: str) -> dict[str, Any]:
    return {"code": 200, "body": {"status": status}}


def good_answers() -> dict[str, dict[str, Any]]:
    """The answers of an install where every check passes."""
    a: dict[str, dict[str, Any]] = {"ready": {"code": 200, "body": {"sandbox_mode": "on"}}}

    a["c1-row"] = {
        "code": 200,
        "body": {"rows": [row("c1", "override:lower", "person", "ok")["body"]]},
    }
    a["c1-show"] = row("c1", "override:lower", "person", "ok")

    a["c2-call"] = call("c2", "awaiting_approval", request="req-2")
    a["c2-call-row"] = row("c2", "floor:token_runs_only_safe", "personal_agent")
    a["c2-operator-request"] = request("pending")
    a["c2-operator-rows"] = rows("c2-op", "/ui/approvals/req-2/approve")
    a["c2-admin-request"] = request("approved")
    a["c2-admin-rows"] = rows("c2-admin", "/ui/approvals/req-2/approve")

    a["c3-nopreview"] = call("c3-np", "error", code="preview_binding_required")
    a["c3-preview"] = {"code": 200, "body": {"status": "ok", "preview_hash": "abc"}}
    a["c3-call"] = call("c3", "awaiting_approval", request="req-3")
    a["c3-call-row"] = row("c3", "floor:destructive_waits", "personal_agent")

    a["c4-agent"] = call("c4a", "ok")
    a["c4-agent-row"] = row("c4a", "tenant_rule:personal_agent.caution", "personal_agent", "ok")
    a["c4-service"] = call("c4s", "awaiting_approval", request="req-4")
    a["c4-service-row"] = row("c4s", "default:service.caution_write", "service_agent")

    a["c5-call"] = call("c5", "awaiting_approval", request="req-5")
    a["c5-call-row"] = row("c5", "default:service.dangerous", "service_agent")
    a["c5-without-request"] = request("pending")
    a["c5-without-rows"] = rows("c5-without", "/ui/approvals/req-5/approve")
    a["c5-with-request"] = request("approved")
    a["c5-with-rows"] = rows("c5-with", "/ui/approvals/req-5/approve")

    fixed = {
        "c6-cred-read": ("floor:credential_waits", "service_agent"),
        "c6-cred-write": ("floor:credential_waits", "personal_agent"),
        "c6-rights": ("floor:rights_change_waits", "personal_agent"),
        "c6-policy": ("floor:policy_change_waits", "service_agent"),
        "c6-destructive": ("floor:destructive_waits", "service_agent"),
        "c6-delete-person": ("floor:delete_waits", "personal_agent"),
        "c6-delete-agent": ("floor:delete_waits", "service_agent"),
        "c6-delete-service": ("floor:delete_waits", "service_agent"),
        "c6-delete-lowered": ("floor:delete_waits", "personal_agent"),
        "c6-address": ("floor:caller_address_waits", "personal_agent"),
        "c6-root": ("floor:root_command_waits", "service_agent"),
    }
    for step, (rule, kind) in fixed.items():
        a[step] = call(step, "awaiting_approval", request=f"req-{step}")
        a[f"{step}-row"] = row(step, rule, kind)
    a["c6-destructive-preview"] = {"code": 200, "body": {"preview_hash": "def"}}
    a["c6-delete-grant"] = call("c6-delete-grant", "ok")
    a["c6-delete-grant-row"] = row(
        "c6-delete-grant", "standing_grant:sandbox_delete_grant", "service_agent", "ok"
    )
    a["c6-agent-approve"] = {"code": 403, "body": {"detail": "only a person approves"}}
    a["c6-agent-approve-request"] = request("pending")
    a["c6-agent-approve-rows"] = rows(
        "c6-agent-approve", "/api/v1/approvals/req-c6-delete-person/approve"
    )

    a["c8-console"] = {"code": 0, "body": "y"}
    a["c8-console-rows"] = rows("c8-console", "/ui/operations/write/override")
    a["c8-token"] = {"code": 422, "body": {"mistakes": ["a write is never below caution"]}}
    a["c8-token-rows"] = rows("c8-token", "/api/v1/policy/import")

    a["c9-fresh"] = {"code": 0, "body": "y"}
    a["c9-rows"] = rows("c9", "/ui/operations/write/override")

    a["c10-web"] = call("c10-web", "awaiting_approval", request="req-10a")
    a["c10-web-row"] = row("c10-web", "floor:token_runs_only_safe", "personal_agent")
    a["c10-web-approve-request"] = request("approved")
    a["c10-web-approve-rows"] = rows("c10-web-approve", "/ui/approvals/req-10a/approve")
    a["c10-cli"] = call("c10-cli", "awaiting_approval", request="req-10b")
    a["c10-cli-approve"] = {"code": 1, "body": {"detail": "approve your own agent in the console"}}
    a["c10-cli-request"] = request("pending")
    a["c10-cli-rows"] = rows("c10-cli", "/api/v1/approvals/req-10b/approve")

    a["c11-run"] = {"code": 202, "body": {"sensor_id": "s"}}
    a["c11-row"] = {
        "code": 200,
        "body": {"rows": [row("c11", "default:system", "system", "ok")["body"]]},
    }

    for i in range(1, PROBE_COUNT + 1):
        for prefix in ("c7", "baseline"):
            a[f"{prefix}-{i}"] = {"code": 200, "body": {"answer": "wait"}}
            a[f"{prefix}-{i}-row"] = rows(f"{prefix}-{i}", "/api/v1/policy/explain")
    return a


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs bash and jq")
class CustomPolicyCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "answers"
        self.dir.mkdir()
        self.answers = good_answers()
        baseline = "".join(
            f"{key}\twait\n"
            for key in (
                "PERSON_TOKEN LOWERED",
                "PERSON_TOKEN RELEVELLED",
                "PERSON_TOKEN ALWAYS_WAIT",
                "PERSON_TOKEN AGENT_RULE",
                "SERVICE_TOKEN AGENT_RULE",
                "SERVICE_TOKEN APPROVER",
                "AGENT_TOKEN CREDENTIAL_READ",
                "AGENT_TOKEN CREDENTIAL_WRITE",
                "PERSON_TOKEN RIGHTS_CHANGE",
                "AGENT_TOKEN POLICY_CHANGE",
                "SERVICE_TOKEN DESTRUCTIVE",
                "PERSON_TOKEN DELETE",
                "DELETE_GRANT_TOKEN DELETE",
                "PERSON_TOKEN CALLER_ADDRESS",
                "SERVICE_TOKEN ROOT_COMMAND",
                "PERSON_TOKEN WRITE",
            )
        )
        (self.dir / "baseline.tsv").write_text(baseline)

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        for step, answer in self.answers.items():
            (self.dir / f"{step}.json").write_text(json.dumps(answer))
        env = {
            key: value for key, value in os.environ.items() if not key.startswith(("CPC_", "MEHO_"))
        }
        return subprocess.run(
            ["bash", str(SCRIPT), "--dry-run", str(self.dir), *args],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )

    def result_lines(self, stdout: str) -> list[str]:
        return [line for line in stdout.splitlines() if line.startswith(("[ok]", "[FAIL]"))]

    def test_every_check_passes_with_audit_row_ids(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.result_lines(result.stdout)
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue(line.startswith("[ok]"), line)
            self.assertRegex(line, r"audit rows? [0-9a-f-]{36}", line)
        self.assertIn("passed, 0 failed", result.stdout)

    def test_a_wrong_answer_prints_a_fail_line_with_its_audit_row(self) -> None:
        # A delete by a service login without a grant that runs at once
        # breaks the fixed rule.
        self.answers["c6-delete-service"] = call("c6-delete-service", "ok")
        self.answers["c6-delete-service-row"] = row(
            "c6-delete-service", "default:service.caution", "service_agent", "ok"
        )
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        fails = [line for line in self.result_lines(result.stdout) if line.startswith("[FAIL]")]
        self.assertEqual(len(fails), 1, fails)
        self.assertIn(
            "check 6 delete (a service login without a grant, a service agent, REST): runs",
            fails[0],
        )
        self.assertIn(f"audit row {audit_id('c6-delete-service')}", fails[0])
        self.assertIn("expected waits", fails[0])
        self.assertIn("expected rule floor:delete_waits", fails[0])
        self.assertIn("1 failed", result.stdout)

    def test_a_shadow_rule_does_not_count(self) -> None:
        self.answers["c3-call-row"] = row(
            "c3", "floor:destructive_waits (shadow)", "personal_agent"
        )
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected rule floor:destructive_waits", result.stdout)

    def test_check_11_needs_the_caller_kind_system(self) -> None:
        self.answers["c11-row"] = {
            "code": 200,
            "body": {"rows": [row("c11", "default:system", "runner", "ok")["body"]]},
        }
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"\[FAIL\] check 11 .*caller kind runner")

    def test_runs_the_checks_in_order_and_ends_with_the_switch_off(self) -> None:
        result = self.run_script()
        order: list[str] = []
        for line in self.result_lines(result.stdout):
            number = re.match(r"\[(?:ok|FAIL)\]\s+check (\d+)", line)
            assert number is not None, line
            if not order or order[-1] != number.group(1):
                order.append(number.group(1))
        self.assertEqual(order, CHECK_ORDER)
        self.assertIn("Turn the switch CUSTOM_POLICY off", result.stdout)

    def test_each_check_names_the_caller_and_the_path(self) -> None:
        result = self.run_script()
        paths = ("web console", "command line", "MCP", "REST")
        for line in self.result_lines(result.stdout):
            caller_and_path = re.search(r"check [^(]*\(([^)]*)\)", line)
            assert caller_and_path is not None, line
            self.assertTrue(any(p in caller_and_path.group(1) for p in paths), line)
        self.assertIn("command line", result.stdout)
        self.assertIn("MCP", result.stdout)

    def test_check_7_fails_when_an_answer_changed(self) -> None:
        self.answers["c7-12"] = {"code": 200, "body": {"answer": "run"}}
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"\[FAIL\] check 7 \(PERSON_TOKEN DELETE.*run, before wait")

    def test_refuses_an_install_that_is_not_in_sandbox_mode(self) -> None:
        self.answers["ready"] = {"code": 200, "body": {"sandbox_mode": "off"}}
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("not in sandbox mode", result.stderr)
        self.assertEqual(self.result_lines(result.stdout), [])

    def test_refuses_when_ready_does_not_answer(self) -> None:
        del self.answers["ready"]
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("not in sandbox mode", result.stderr)

    def test_record_baseline_writes_one_answer_per_probe(self) -> None:
        out = self.dir / "new-baseline.tsv"
        result = self.run_script("--record-baseline", str(out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = out.read_text().splitlines()
        self.assertEqual(len(lines), PROBE_COUNT)
        self.assertTrue(all(line.endswith("\twait") for line in lines))

    def test_a_live_run_names_the_missing_settings(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CPC_", "MEHO_"))}
        env["MEHO_BACKPLANE"] = "https://meho.sandbox.example.test"
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("CPC_ADMIN_TOKEN", result.stderr)

    def test_help(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)


class NoSecretsInTheScriptTest(unittest.TestCase):
    def test_the_script_holds_no_token_password_host_or_address(self) -> None:
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r"https?://")
        self.assertNotRegex(text, r"\b\d{1,3}(\.\d{1,3}){3}\b")
        self.assertNotRegex(text, r"Bearer [A-Za-z0-9._-]{8,}")
        self.assertNotRegex(text, r"eyJ[A-Za-z0-9_-]{10,}")
        self.assertNotRegex(text.lower(), r"password\s*=")


if __name__ == "__main__":
    unittest.main()
