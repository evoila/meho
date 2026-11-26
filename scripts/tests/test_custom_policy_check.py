# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test the custom policy check script in its dry mode, against recorded answers.

No install is contacted: every answer comes from a folder of recorded
answers that the test writes (``custom_policy_check_fixture``). Each test
starts from the answers of an install where every check passes and breaks
one thing. The script needs ``jq``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from custom_policy_check_fixture import (
    ALWAYS_SKIPPED,
    OPS,
    PROBE_KEYS,
    SCRIPT,
    audit_id,
    base_env,
    baseline_text,
    call,
    good_answers,
    post_row,
    ready,
    request,
    result_lines,
    row,
    rows,
    write_answers,
)

CHECK_ORDER = ["1", "2", "3", "4", "5", "6", "8", "9", "10", "11", "7"]
NOT_COMPLETE = 3


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "needs bash and jq")
class DryRunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "answers"
        self.dir.mkdir()
        self.answers = good_answers()
        self.baseline = self.dir / "baseline.tsv"

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        write_answers(self.dir, self.answers)
        if not self.baseline.exists():
            self.baseline.write_text(baseline_text())
        env = base_env()
        env.update(OPS)
        env["CPC_CUSTOM_ROLE"] = "Approvers-VMW"
        return subprocess.run(
            ["bash", str(SCRIPT), "--dry-run", str(self.dir), *args],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
            check=False,
        )

    def fails(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return [line for line in result_lines(result.stdout) if line.startswith("[FAIL]")]

    def assert_one_fail(
        self, result: subprocess.CompletedProcess[str], start: str, *parts: str
    ) -> str:
        fails = self.fails(result)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(fails), 1, fails)
        self.assertTrue(fails[0].startswith(f"[FAIL] {start}"), fails[0])
        for part in parts:
            self.assertIn(part, fails[0])
        return fails[0]


class FullRunTest(DryRunTestCase):
    def test_every_check_passes_and_the_run_ends_not_complete(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE, result.stdout + result.stderr)
        lines = result_lines(result.stdout)
        skipped = [line[len("[skip] ") :] for line in lines if line.startswith("[skip]")]
        self.assertEqual(skipped, ALWAYS_SKIPPED)
        self.assertEqual(self.fails(result), [])
        for line in lines:
            if line.startswith("[ok]") and "risk level" not in line:
                self.assertRegex(line, r"(audit|click) rows? [0-9a-f-]{36}", line)
        self.assertIn("passed, 0 failed, 5 not run, 0 not taken back", result.stdout)

    def test_runs_the_checks_in_order_and_ends_with_the_switch_off(self) -> None:
        result = self.run_script()
        order: list[str] = []
        for line in result_lines(result.stdout):
            number = re.match(r"\[(?:ok|FAIL|skip)\]\s+check (\d+)", line)
            assert number is not None, line
            if not order or order[-1] != number.group(1):
                order.append(number.group(1))
        self.assertEqual(order, CHECK_ORDER)
        self.assertIn("Turn the switch CUSTOM_POLICY off", result.stdout)

    def test_each_check_names_the_caller_and_the_path(self) -> None:
        result = self.run_script()
        paths = ("web console", "command line", "MCP", "REST")
        for line in result_lines(result.stdout):
            if "not run:" in line:
                continue
            caller_and_path = re.search(r"check [^(]*\(([^)]*)\)", line)
            assert caller_and_path is not None, line
            self.assertTrue(any(p in caller_and_path.group(1) for p in paths), line)

    def test_a_wrong_answer_prints_a_fail_line_with_its_audit_row(self) -> None:
        self.answers["c6-delete-service"] = call("c6-delete-service", "ok")
        self.answers["c6-delete-service-row"] = row(
            "c6-delete-service", "default:service.caution", "service_agent", "ok"
        )
        line = self.assert_one_fail(
            self.run_script(),
            "check 6 delete (a service login without a grant, a service agent, REST): runs",
            f"audit row {audit_id('c6-delete-service')}",
            "expected waits",
            "expected rule floor:delete_waits",
        )
        self.assertIn("rule default:service.caution", line)

    def test_the_help_names_the_exit_codes(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)
        self.assertIn("3  not complete", result.stdout)
        self.assertIn("Only a person can ask for", result.stdout)


class StartTest(DryRunTestCase):
    def test_refuses_an_install_that_is_not_in_sandbox_mode(self) -> None:
        self.answers["ready"] = {"code": 200, "body": {"status": "ready", "sandbox_mode": "off"}}
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("not in sandbox mode", result.stderr)
        self.assertEqual(result_lines(result.stdout), [])

    def test_refuses_when_ready_does_not_answer(self) -> None:
        del self.answers["ready"]
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("not in sandbox mode", result.stderr)

    def test_refuses_a_ready_answer_that_is_not_200_ready(self) -> None:
        for status in ("not_ready", "draining"):
            with self.subTest(status=status):
                self.answers["ready"] = ready(True, code=503, status=status)
                result = self.run_script()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(f"HTTP 503 with status {status}", result.stderr)
                self.assertEqual(result_lines(result.stdout), [])

    def test_prints_the_version_of_the_install(self) -> None:
        result = self.run_script()
        self.assertIn("[note] the install's version: abc1234 (chart 1.2.3)", result.stdout)


class SwitchTest(DryRunTestCase):
    """Point 5: the switch is read at the start, for the baseline, and in check 7."""

    STATES: tuple[tuple[str, bool | None], ...] = (("on", True), ("off", False), ("unknown", None))

    def test_a_run_starts_only_with_the_switch_on(self) -> None:
        for name, value in self.STATES:
            with self.subTest(switch=name):
                self.answers["ready"] = ready(value)
                result = self.run_script()
                if name == "on":
                    self.assertEqual(result.returncode, NOT_COMPLETE, result.stderr)
                else:
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(f"the switch CUSTOM_POLICY is {name}", result.stderr)
                    self.assertEqual(result_lines(result.stdout), [])

    def test_the_baseline_is_recorded_only_with_the_switch_off(self) -> None:
        out = self.dir / "new-baseline.tsv"
        for name, value in self.STATES:
            with self.subTest(switch=name):
                self.answers["ready"] = ready(value)
                result = self.run_script("--record-baseline", str(out))
                if name == "off":
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(out.exists())
                    out.unlink()
                else:
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(f"the switch CUSTOM_POLICY is {name}", result.stderr)
                    self.assertFalse(out.exists())

    def test_check_7_fails_when_the_switch_is_not_off(self) -> None:
        for name, value in self.STATES:
            with self.subTest(switch=name):
                self.answers["c7-ready"] = ready(value)
                result = self.run_script()
                if name == "off":
                    self.assertEqual(result.returncode, NOT_COMPLETE)
                else:
                    self.assert_one_fail(
                        result, "check 7 (REST /ready)", f"the switch CUSTOM_POLICY is {name}"
                    )
                    self.assertNotIn("REST policy explain)", result.stdout)


class BaselineTest(DryRunTestCase):
    def test_record_baseline_writes_one_answer_per_probe(self) -> None:
        self.answers["ready"] = ready(False)
        out = self.dir / "new-baseline.tsv"
        result = self.run_script("--record-baseline", str(out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(out.read_text(), baseline_text())

    def test_record_baseline_refuses_a_failed_probe_and_keeps_the_earlier_file(self) -> None:
        self.answers["ready"] = ready(False)
        out = self.dir / "kept-baseline.tsv"
        out.write_text("the earlier baseline\n")
        broken = {
            "HTTP 500": {"code": 500, "body": {"detail": "boom"}},
            "no answer": {"code": 200, "body": {"status": "error"}},
        }
        for name, answer in broken.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["ready"] = ready(False)
                self.answers["baseline-3"] = answer
                result = self.run_script("--record-baseline", str(out))
                self.assertEqual(result.returncode, 1)
                self.assertIn("[FAIL] baseline (as check 3", result.stdout)
                self.assertIn("no baseline saved", result.stderr)
                self.assertEqual(out.read_text(), "the earlier baseline\n")
        self.answers = good_answers()
        self.answers["ready"] = ready(False)
        del self.answers["baseline-4-row"]
        result = self.run_script("--record-baseline", str(out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no audit row", result.stdout)
        self.assertEqual(out.read_text(), "the earlier baseline\n")


class Check7Test(DryRunTestCase):
    def test_check_7_fails_when_an_answer_changed(self) -> None:
        self.answers["c7-13"] = {"code": 200, "body": {"status": "ok", "answer": "run"}}
        self.assert_one_fail(
            self.run_script(),
            "check 7 (as check 6: PERSON_TOKEN DELETE, token",
            "run, before wait",
        )

    def test_check_7_fails_when_explain_fails_now_and_before(self) -> None:
        self.baseline.write_text(baseline_text("no answer"))
        self.answers["c7-1"] = {"code": 500, "body": {"detail": "boom"}}
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        fails = self.fails(result)
        self.assertEqual(len(fails), len(PROBE_KEYS), fails)
        self.assertIn("HTTP 500", fails[0])
        self.assertIn("no valid baseline answer", fails[1])

    def test_each_probe_repeats_the_caller_and_the_way_in_of_its_check(self) -> None:
        result = self.run_script()
        probes = re.findall(
            r"check 7 \(as check (\d+): (\S+) (\S+), (web console|token), REST policy explain\)",
            result.stdout,
        )
        listed = [
            f"{c} {t} {o} {'web' if w == 'web console' else 'token'}" for c, t, o, w in probes
        ]
        self.assertEqual(listed, PROBE_KEYS)
        # The probe next to its check: the caller and the way in of that check.
        expected = {
            "1 PERSON_TOKEN LOWERED web": "check 1 (the Operator, a person, web console)",
            "2 PERSON_TOKEN RELEVELLED token": "check 2 (an Operator's token, a personal agent, REST)",
            "6 PERSON_TOKEN CREDENTIAL_WRITE token": "check 6 credential write (the Operator's meho login",
            "6 AGENT_TOKEN POLICY_CHANGE token": "check 6 change of the policy (an agent login",
            "6 PERSON_TOKEN POLICY_CHANGE token": "check 6 change of the policy (an Operator's AI tool",
            "6 DELETE_GRANT_TOKEN DELETE token": "check 6 delete (a service login with a delete grant",
            "10 PERSON_TOKEN OWN_AGENT token": "check 10 (the Operator's AI tool, a personal agent, MCP)",
        }  # fmt: skip
        for key, check_line in expected.items():
            self.assertIn(key, PROBE_KEYS)
            self.assertIn(check_line, result.stdout)
        used = {name.removeprefix("CPC_OP_") for name in OPS}
        probed = {key.split()[2] for key in PROBE_KEYS}
        self.assertEqual(used, probed)


class AnswerForAnotherReasonTest(DryRunTestCase):
    """Point 4: each check fails on no answer, or an answer for another reason."""

    def test_refused_clicks_need_the_click_row_of_another_person(self) -> None:
        for step, check in (
            ("c2-operator", "check 2 (a second Operator"),
            ("c5-without", "check 5 (a person without"),
        ):
            with self.subTest(step=step):
                self.answers = good_answers()
                self.answers[f"{step}-rows"] = rows()
                self.assert_one_fail(self.run_script(), check, "expected the audit row of a click")
                self.answers = good_answers()
                req = "req-2" if step == "c2-operator" else "req-5"
                self.answers[f"{step}-rows"] = rows(
                    post_row(step, f"/ui/approvals/{req}/approve", "operator-1"),
                    {"id": audit_id("lookup"), "method": "GET", "path": f"/api/v1/approvals/{req}"},
                )
                self.assert_one_fail(self.run_script(), check, "by another person than the sender")

    def test_check_6_agent_approve_passes_only_on_403_human_principal_required(self) -> None:
        broken: dict[str, tuple[dict[str, Any], str]] = {
            "fresh sign-in": ({"code": 401, "body": {"detail": "fresh_sign_in_required: x"}}, "pending"),
            "broken token": ({"code": 401, "body": {"detail": "invalid_token"}}, "pending"),
            "not found": ({"code": 404, "body": {"detail": "approval_request_not_found"}}, "pending"),
            "approved": ({"code": 200, "body": {"status": "approved"}}, "approved"),
            "another 403": ({"code": 403, "body": {"detail": "insufficient_role"}}, "pending"),
            "approved after": ({"code": 403, "body": {"detail": "human_principal_required: x"}}, "approved"),
        }  # fmt: skip
        for name, (answer, status) in broken.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c6-agent-approve"] = answer
                self.answers["c6-agent-approve-request"] = request(status)
                self.assert_one_fail(self.run_script(), "check 6 agent approve (an agent login")

    def test_check_6_agent_approve_needs_the_row_of_the_attempt(self) -> None:
        self.answers["c6-agent-approve-rows"] = rows()
        self.assert_one_fail(
            self.run_script(), "check 6 agent approve", "expected the audit row of the attempt"
        )

    def test_check_8_token_needs_the_mistake_of_the_write_floor(self) -> None:
        raise_not_lower = (
            "`classifications[0]` would set `k8s.annotate` to safe, below its level in the code "
            "(caution). A file can raise a risk level, not lower it."
        )
        broken = {
            "401": {"code": 401, "body": {"detail": "invalid_token"}},
            "raise, not lower": {"code": 200, "body": {"ok": False, "mistakes": [raise_not_lower]}},
            "ok": {"code": 200, "body": {"ok": True, "mistakes": []}},
        }
        for name, answer in broken.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c8-token"] = answer
                self.assert_one_fail(self.run_script(), "check 8 (an Admin's token, REST")

    def test_checks_8_and_9_in_the_console_need_their_audit_rows(self) -> None:
        for step, check in (
            ("c8-console-rows", "check 8 (an Admin, a person, web console)"),
            ("c9-rows", "check 9"),
        ):
            with self.subTest(step=step):
                self.answers = good_answers()
                self.answers[step] = rows()
                self.assert_one_fail(self.run_script(), check, "audit row")

    def test_check_8_in_the_console_fails_when_the_write_became_safe(self) -> None:
        self.answers["c8-level"] = {"code": 200, "body": {"safety_level": "safe"}}
        self.assert_one_fail(
            self.run_script(), "check 8 (an Admin, a person, web console)", "at least caution"
        )

    def test_check_9_fails_on_a_no(self) -> None:
        self.answers["c9-fresh"] = {"code": 0, "body": "n"}
        self.assert_one_fail(self.run_script(), "check 9", "expected a fresh sign-in each time")

    def test_waiting_rows_need_their_rule(self) -> None:
        cases = {
            "c5-call-row": row("c5", "default:service.dangerous (shadow)", "service_agent"),
            "c10-web-row": row("c10-web", "", "personal_agent"),
        }
        for step, answer in cases.items():
            with self.subTest(step=step):
                self.answers = good_answers()
                self.answers[step] = answer
                self.assert_one_fail(self.run_script(), "check ", "expected rule")

    def test_check_3_without_a_preview_needs_its_audit_row(self) -> None:
        del self.answers["c3-nopreview-row"]
        self.assert_one_fail(self.run_script(), "check 3 (", "without a preview", "no audit row")

    def test_check_6_a_policy_change_by_an_agent_must_be_refused(self) -> None:
        self.answers["c6-policy-agent"] = call("c6-policy-agent", "awaiting_approval", request="r")
        self.answers["c6-policy-agent-row"] = row(
            "c6-policy-agent", "floor:policy_change_waits", "service_agent"
        )
        self.answers["withdraw-r-status"] = request("pending")
        self.answers["withdraw-r"] = {"code": 200, "body": {"status": "withdrawn"}}
        self.assert_one_fail(
            self.run_script(), "check 6 change of the policy (an agent login", "expected refused"
        )


class RiskLevelTest(DryRunTestCase):
    """Codex findings 6 and 10: the risk level and the override of this run."""

    def test_check_1_needs_the_override_of_this_run(self) -> None:
        self.answers["c1-row"] = rows(row("c1", "override:ov-9", "person", "ok")["body"])
        self.assert_one_fail(
            self.run_script(), "check 1", "expected the rule of the override override:ov-1"
        )
        self.answers = good_answers()
        self.answers["c1-overrides"] = {"code": 200, "body": {"overrides": []}}
        self.assert_one_fail(self.run_script(), "check 1", "no override of this run")

    def test_check_2_reads_the_level_dangerous_first(self) -> None:
        self.answers["c2-level"] = {"code": 200, "body": {"safety_level": "caution"}}
        self.assert_one_fail(
            self.run_script(), "check 2 (an Operator's token, REST preview): risk level caution"
        )

    def test_check_6_fails_when_the_lowering_did_not_change_the_level(self) -> None:
        self.answers["c6-delete-lowered-preview"] = {
            "code": 200,
            "body": {"preview_hash": "h", "safety_level": "destructive"},
        }
        self.assert_one_fail(
            self.run_script(), "check 6 delete after a lowering", "expected a lower risk level"
        )

    def test_the_root_command_waits_for_its_rule_and_never_counts(self) -> None:
        result = self.run_script()
        self.assertIn(
            "[skip] check 6 root command (a service login, a service agent, REST): "
            "not run: waits for #7051",
            result.stdout,
        )
        self.assertNotRegex(result.stdout, r"\[ok\]\s+check 6 root command")
        self.assertEqual(result.returncode, NOT_COMPLETE)


class Check11Test(DryRunTestCase):
    """Point 10: check 11 covers the logins of MEHO's own code by their caller kind."""

    def sensor_row(self, kind: str | None) -> dict[str, Any]:
        body = row("c11", "default:system", kind or "", "ok")["body"]
        if kind is None:
            del body["caller_kind"]
        body.update(target_name="web-a", principal_sub="__sensor__")
        return rows(body)

    def test_waits_for_the_caller_kind_on_the_audit_row(self) -> None:
        self.answers["c11-row"] = self.sensor_row(None)
        result = self.run_script()
        self.assertIn("[skip] check 11: not run: waits for #7073", result.stdout)
        self.assertNotRegex(result.stdout, r"\[ok\]\s+check 11")
        self.assertEqual(result.returncode, NOT_COMPLETE)

    def test_the_check_runner_needs_the_kind_system_on_the_row_of_its_run(self) -> None:
        self.assertIn("[ok]   check 11 (the check runner", self.run_script().stdout)
        for kind in ("runner", "personal_agent"):
            with self.subTest(kind=kind):
                self.answers["c11-row"] = self.sensor_row(kind)
                self.assert_one_fail(
                    self.run_script(), "check 11 (the check runner", f"caller kind {kind}"
                )
        other = self.sensor_row("system")
        other["body"]["rows"][0]["target_name"] = "web-b"
        self.answers["c11-row"] = other
        self.assert_one_fail(self.run_script(), "check 11 (the check runner", "no audit row")

    def test_the_web_console_reader_needs_the_kind_person(self) -> None:
        self.assertIn("[ok]   check 11 (the web console reader", self.run_script().stdout)
        self.answers["c1-row"] = rows(row("c1", "override:ov-1", "system", "ok")["body"])
        result = self.run_script()
        self.assertRegex(
            result.stdout, r"\[FAIL\] check 11 \(the web console reader.*caller kind system"
        )
        self.answers = good_answers()
        del self.answers["p1"]
        result = self.run_script()
        self.assertIn(
            "[skip] check 11 (the web console reader): not run: waits for the person step 1",
            result.stdout,
        )


class PersonStepTest(DryRunTestCase):
    def test_a_person_step_without_an_answer_skips_the_steps_after_it(self) -> None:
        del self.answers["p4"]
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        self.assertIn(
            "[skip] check 4 (an Admin, a person, web console): needs a person", result.stdout
        )
        self.assertEqual(result.stdout.count("not run: waits for the person step 4"), 2)

    def test_a_no_by_the_person_does_the_same(self) -> None:
        self.answers["p6-delete"] = {"code": 0, "body": "n"}
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        self.assertEqual(result.stdout.count("not run: waits for the person step 6"), 5)
        self.assertNotRegex(result.stdout, r"\[(ok|FAIL)\]\s+check 6 delete")

    def test_the_console_approvals_need_a_person(self) -> None:
        for step in ("p2-admin", "p5-with", "p10-web"):
            del self.answers[step]
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        for line in (
            "[skip] check 2 (an Admin, a person, web console): needs a person",
            "[skip] check 5 (a person with the custom role, web console): needs a person",
            "[skip] check 10 (the Operator, a person, web console): needs a person",
            "[skip] check 10 (command line approve): needs a person",
        ):
            self.assertIn(line, result.stdout)

    def test_what_could_not_be_taken_back_is_named(self) -> None:
        self.answers["withdraw-req-3"] = {"code": 403, "body": {"detail": "not_request_sender"}}
        self.answers["takeback-overrides"] = self.answers["c1-overrides"]
        self.answers["takeback-agent-rule"] = {"code": 200, "body": {"answer": "run"}}
        self.answers["takeback-role"] = {
            "code": 200,
            "body": {"answer": "wait", "approvers": {"groups": ["Approvers-VMW"]}},
        }
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        left = [line for line in result.stdout.splitlines() if line.startswith("[left]")]
        self.assertEqual(len(left), 4, left)
        self.assertIn("request req-3 (sent with PERSON_TOKEN): not withdrawn, HTTP 403", left[0])
        self.assertIn("the lowering of k8s.scale: still there", left[1])
        self.assertIn("the agent rule of check 4", left[2])
        self.assertIn("the custom role Approvers-VMW of check 5", left[3])


class NoSecretsInTheScriptTest(unittest.TestCase):
    def test_the_script_holds_no_token_password_host_or_address(self) -> None:
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r"https?://")
        self.assertNotRegex(text, r"\b\d{1,3}(\.\d{1,3}){3}\b")
        self.assertNotRegex(text, r"Bearer [A-Za-z0-9._-]{8,}")
        self.assertNotRegex(text, r"eyJ[A-Za-z0-9_-]{10,}")
        self.assertNotRegex(text.lower(), r"password\s*=")

    def test_the_script_never_decides_a_request(self) -> None:
        text = SCRIPT.read_text()
        self.assertNotIn("/decide", text)
        self.assertNotRegex(text, r"meho_cli\s+\S+\s+approvals")
        self.assertNotRegex(text, r"(?m)^\s*meho\s+approvals")
        self.assertEqual(
            len(re.findall(r"rest [\w-]+ POST \"/api/v1/approvals/[^\"]*/approve\"", text)), 1
        )


if __name__ == "__main__":
    unittest.main()
