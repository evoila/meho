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
    C1_EDIT_AT,
    C1_START,
    FAR_FUTURE,
    OPS,
    PARAMS,
    PROBE_KEYS,
    REMOVAL_NOT_CONFIRMED,
    ROOT,
    SCRIPT,
    SENSOR_ID,
    WORK_REF,
    audit_id,
    base_env,
    baseline_text,
    call,
    check_9_rows,
    edit_row,
    good_answers,
    post_row,
    preview,
    ready,
    request,
    result_lines,
    row,
    rows,
    run_row,
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
        env.update(PARAMS)
        env["CPC_SENSOR_ID"] = SENSOR_ID
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
                self.assertRegex(line, r"rows? [0-9a-f-]{36}", line)
        self.assertIn("passed, 0 failed, 6 not run, 2 not taken back", result.stdout)

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
    """The switch is read at the start, for the baseline, and in check 7."""

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


class SwitchOffBaselineTest(DryRunTestCase):
    """With the switch off, MEHO has no policy import operation to explain."""

    def unknown(self) -> dict[str, Any]:
        return {
            "code": 200,
            "body": {
                "status": "error",
                "error": "unknown_connector: policy",
                "extras": {"error_code": "unknown_connector"},
            },
        }

    def policy_probes(self) -> list[int]:
        return [i + 1 for i, key in enumerate(PROBE_KEYS) if "POLICY_CHANGE" in key]

    def test_the_policy_import_probes_save_unknown_connector(self) -> None:
        self.answers["ready"] = ready(False)
        for i in self.policy_probes():
            self.answers[f"baseline-{i}"] = self.unknown()
        out = self.dir / "new-baseline.tsv"
        result = self.run_script("--record-baseline", str(out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = out.read_text().splitlines()
        self.assertEqual(len(lines), len(PROBE_KEYS))
        for i in self.policy_probes():
            self.assertEqual(lines[i - 1].split("\t")[1], "unknown_connector")

    def test_check_7_expects_unknown_connector_again(self) -> None:
        text = self.baseline_with_unknown()
        self.baseline.write_text(text)
        for i in self.policy_probes():
            self.answers[f"c7-{i}"] = self.unknown()
        self.assertEqual(self.fails(self.run_script()), [])
        self.answers[f"c7-{self.policy_probes()[0]}"] = {
            "code": 200,
            "body": {"status": "ok", "answer": "wait"},
        }
        self.assert_one_fail(self.run_script(), "check 7 (as check 6: AGENT_TOKEN POLICY_CHANGE")

    def test_another_error_still_fails(self) -> None:
        self.answers["ready"] = ready(False)
        other = self.unknown()
        other["body"]["extras"]["error_code"] = "unknown_operation"
        self.answers["baseline-1"] = other
        self.answers[f"baseline-{self.policy_probes()[0]}"] = self.unknown()
        result = self.run_script("--record-baseline", str(self.dir / "x.tsv"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("[FAIL] baseline (as check 1", result.stdout)

    def baseline_with_unknown(self) -> str:
        lines = baseline_text().splitlines(keepends=True)
        for i in self.policy_probes():
            key, _, _, verdict = lines[i - 1].rstrip("\n").split("\t")
            lines[i - 1] = f"{key}\tunknown_connector\tnull\t{verdict}\n"
        return "".join(lines)


OPERATOR_DELETE_PROBE = PROBE_KEYS.index("6 PERSON_TOKEN DELETE token") + 1
OWN_APPROVAL_SKIP = (
    "[skip] check 7 (the owner's own approval of the request of check 10, web console and "
    "command line): not run: waits for #7132"
)


class Check7Test(DryRunTestCase):
    def test_check_7_fails_when_an_answer_changed(self) -> None:
        self.answers[f"c7-{OPERATOR_DELETE_PROBE}"] = {
            "code": 200,
            "body": {"status": "ok", "answer": "run"},
        }
        self.assert_one_fail(
            self.run_script(),
            "check 7 (as check 6: PERSON_TOKEN DELETE, token",
            "expected the answer of before",
        )

    def test_check_7_fails_when_only_who_may_approve_changed(self) -> None:
        self.answers[f"c7-{OPERATOR_DELETE_PROBE}"] = {
            "code": 200,
            "body": {"status": "ok", "answer": "wait", "approvers": {"groups": ["Operators"]}},
        }
        line = self.assert_one_fail(
            self.run_script(),
            "check 7 (as check 6: PERSON_TOKEN DELETE, token",
            "approvers of before",
        )
        self.assertNotIn("expected the answer of before", line)

    def test_the_owners_own_approval_is_skipped_and_the_run_is_not_complete(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE, result.stdout + result.stderr)
        self.assertEqual(self.fails(result), [])
        lines = result_lines(result.stdout)
        self.assertEqual(lines.count(OWN_APPROVAL_SKIP), 1, lines)
        self.assertNotIn(OWN_APPROVAL_SKIP.replace("[skip]", "[ok]  "), lines)

    def test_check_7_fails_when_only_the_operators_policy_import_changed(self) -> None:
        probe = PROBE_KEYS.index("6 PERSON_TOKEN POLICY_CHANGE token") + 1
        self.answers[f"c7-{probe}"] = {"code": 200, "body": {"status": "ok", "answer": "deny"}}
        self.assert_one_fail(
            self.run_script(),
            "check 7 (as check 6: PERSON_TOKEN POLICY_CHANGE, token",
            "expected the answer of before",
        )

    def test_check_7_probes_the_policy_import_of_all_three_logins(self) -> None:
        policy = [key for key in PROBE_KEYS if "POLICY_CHANGE" in key]
        self.assertEqual(
            policy,
            [
                "6 AGENT_TOKEN POLICY_CHANGE token",
                "6 PERSON_TOKEN POLICY_CHANGE token",
                "6 ADMIN_TOKEN POLICY_CHANGE token",
            ],
        )
        baseline = [line.split("\t")[0] for line in baseline_text().splitlines()]
        self.assertTrue(set(policy) <= set(baseline))

    def test_check_7_compares_the_preview_with_the_params(self) -> None:
        step = f"c7-{PROBE_KEYS.index('6 PERSON_TOKEN CREDENTIAL_WRITE token') + 1}-preview"
        self.answers[step] = {
            "code": 200,
            "body": {"status": "ok", "policy_verdict": "auto-execute"},
        }
        self.assert_one_fail(
            self.run_script(),
            "check 7 (as check 6: PERSON_TOKEN CREDENTIAL_WRITE",
            "preview verdict",
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
            "6 ADMIN_TOKEN POLICY_CHANGE token": "check 6 change of the policy (an Admin's AI tool",
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
    """Each check fails on no answer, or on an answer for another reason."""

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
                sender = "operator-1" if step == "c2-operator" else "service-1"
                self.answers[f"{step}-rows"] = rows(
                    post_row(step, f"/ui/approvals/{req}/approve", sender),
                    {"id": audit_id("lookup"), "method": "GET", "path": f"/api/v1/approvals/{req}"},
                )
                self.assert_one_fail(self.run_script(), check, "by another person than the sender")

    def test_a_refused_click_with_an_old_sign_in_fails(self) -> None:
        """A 401 click row, or a sign-in redirect, is a refusal for another reason."""
        for step, req, check in (
            ("c2-operator", "req-2", "check 2 (a second Operator"),
            ("c5-without", "req-5", "check 5 (a person without"),
        ):
            for code in (401, 303, 500):
                with self.subTest(step=step, code=code):
                    self.answers = good_answers()
                    self.answers[f"{step}-rows"] = rows(
                        post_row(step, f"/ui/approvals/{req}/approve", "operator-2", code)
                    )
                    self.assert_one_fail(
                        self.run_script(),
                        check,
                        "a click failed for another reason",
                        f"HTTP {code}",
                    )

    def test_a_refused_click_needs_the_refusal_of_its_rule(self) -> None:
        for step, check in (("c2-operator", "check 2"), ("c5-without", "check 5")):
            with self.subTest(step=step):
                self.answers = good_answers()
                self.answers[f"{step}-refusal"] = {"code": 0, "body": "n"}
                self.assert_one_fail(self.run_script(), check, "expected the refusal")

    def test_an_approval_by_another_person_needs_their_click(self) -> None:
        self.answers["c2-admin-request"] = request("approved", "operator-1", "admin-9")
        self.assert_one_fail(
            self.run_script(), "check 2 (an Admin", "expected the audit row of the approver's click"
        )
        self.answers = good_answers()
        self.answers["c10-web-request"] = request("approved", "operator-1", "admin-1")
        self.assert_one_fail(self.run_script(), "check 10 (the Operator", "approval of the sender")

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

    def test_check_8_token_fails_on_an_answer_that_is_not_a_refusal(self) -> None:
        broken = {
            "401": {"code": 401, "body": {"detail": "invalid_token"}},
            "ok": {"code": 200, "body": {"ok": True, "mistakes": []}},
        }
        for name, answer in broken.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c8-token"] = answer
                self.assert_one_fail(self.run_script(), "check 8 (an Admin's token, REST")

    def test_check_8_token_fails_on_the_refusal_of_another_rule(self) -> None:
        """MEHO says "at least caution" for a secret and a caller's address too."""
        other_rules = {
            "secret": "`classifications[0]` would make `k8s.annotate` safe or not waiting. "
            "`k8s.annotate` shows a secret, so it stays at least caution and always waits.",
            "address": "`classifications[0]`: `k8s.annotate` connects to an address the caller "
            "gives, so it stays at least caution and always waits.",
        }
        for name, mistake in other_rules.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c8-token"] = {
                    "code": 200,
                    "body": {"ok": False, "mistakes": [mistake]},
                }
                self.assert_one_fail(
                    self.run_script(), "check 8 (an Admin's token, REST", "another rule"
                )

    def test_check_8_token_does_not_run_while_the_write_rule_has_no_mistake(self) -> None:
        result = self.run_script()
        self.assertIn(
            "[skip] check 8 (an Admin's token, REST, a policy file preview): "
            "not run: waits for #6966",
            result.stdout,
        )
        self.assertNotRegex(result.stdout, r"\[ok\]\s+check 8 \(an Admin's token")

    def test_checks_8_and_9_count_only_the_edits_of_the_named_operation(self) -> None:
        other_pages = rows(
            post_row("c8-x", "/ui/approvals/req-2/approve", "admin-1", 400),
            post_row("c8-y", "/ui/connectors/registry/k8s/operations/k8s.scale", "admin-1", 400),
        )
        cases = {
            "c8-console-rows": (other_pages, "check 8 (an Admin, a person, web console)"),
            "c9-rows": (rows(edit_row("c9-a", "k8s.scale"), edit_row("c9-b", "k8s.scale", removed=True)), "check 9"),
        }  # fmt: skip
        for step, (answer, check) in cases.items():
            with self.subTest(step=step):
                self.answers = good_answers()
                self.answers[step] = answer
                self.assert_one_fail(self.run_script(), check, "k8s.annotate")

    def test_check_8_in_the_console_fails_when_the_edit_was_saved(self) -> None:
        self.answers["c8-edits"] = rows(edit_row("c8-edit", "k8s.annotate"))
        self.assert_one_fail(
            self.run_script(), "check 8 (an Admin, a person, web console)", "expected no edit"
        )

    def test_checks_8_and_9_in_the_console_need_their_audit_rows(self) -> None:
        for step, check in (
            ("c8-console-rows", "check 8 (an Admin, a person, web console)"),
            ("c9-rows", "check 9"),
        ):
            with self.subTest(step=step):
                self.answers = good_answers()
                self.answers[step] = rows()
                self.assert_one_fail(self.run_script(), check, "row")

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
    """The risk level of a preview, and the override that this run made."""

    def override(
        self, op: str = "k8s.scale", set_at: str = FAR_FUTURE, **extra: Any
    ) -> dict[str, Any]:
        entry = {
            "connector_id": "k8s",
            "op_id": op,
            "safety_level": "caution",
            "set_at": set_at,
            "set_by_sub": "admin-1",
        }
        entry.update(extra)
        return {"code": 200, "body": {"overrides": [entry]}}

    def test_check_1_finds_the_override_by_its_own_fields(self) -> None:
        """In the override list, rule_ref names a classification entry, not the override."""
        for rule_ref in ("", "taxonomy.default", "classifications[3]"):
            with self.subTest(rule_ref=rule_ref):
                self.answers["c1-overrides"] = self.override(rule_ref=rule_ref)
                self.assertRegex(self.run_script().stdout, r"\[ok\]\s+check 1 \(the Operator")

    def test_check_1_fails_on_an_override_of_another_operation_or_from_before(self) -> None:
        cases = {
            "another operation": self.override(op="k8s.restart"),
            "set before the step": self.override(set_at="2025-01-01T00:00:00Z"),
            "set by another person": self.override(set_by_sub="admin-7"),
        }
        for name, answer in cases.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c1-overrides"] = answer
                self.assert_one_fail(self.run_script(), "check 1", "no override of this run")

    def test_check_1_needs_a_rule_of_the_source_override(self) -> None:
        self.answers["c1-row"] = rows(run_row("c1", "default:person.web.caution", "person"))
        self.assert_one_fail(self.run_script(), "check 1", "expected a rule of the source override")

    def test_check_2_reads_the_level_dangerous_first(self) -> None:
        self.answers["c2-level"] = {"code": 200, "body": {"safety_level": "caution"}}
        self.assert_one_fail(
            self.run_script(), "check 2 (an Operator's token, REST preview): risk level caution"
        )

    def test_check_6_the_lowering_passes_only_one_level_lower(self) -> None:
        for level in ("destructive", "dangerous", "safe", ""):
            with self.subTest(level=level):
                self.answers = good_answers()
                self.answers["c6-delete-person-preview"] = preview(
                    "dangerous", "h-c6-delete-person"
                )
                self.answers["c6-delete-lowered-preview"] = {
                    "code": 200,
                    "body": {"preview_hash": "h", "safety_level": level},
                }
                self.assert_one_fail(
                    self.run_script(), "check 6 delete after a lowering", "one risk level lower"
                )

    def test_a_failed_preview_sends_no_call(self) -> None:
        for answer in (
            {"code": 500, "body": {"detail": "boom"}},
            {"code": 200, "body": {"status": "ok", "safety_level": "destructive"}},
        ):
            with self.subTest(answer=answer):
                self.answers = good_answers()
                self.answers["c3-call-preview"] = answer
                line = self.assert_one_fail(self.run_script(), "check 3 (", "no call sent")
                self.assertIn("the preview failed", line)

    def test_the_root_command_waits_by_its_own_rule(self) -> None:
        self.assertRegex(
            self.run_script().stdout,
            r"\[ok\]\s+check 6 root command \(a service login without a grant.*"
            r"rule default:service.root_shell",
        )
        for rule in ("default:service.caution_write", "floor:destructive_waits"):
            with self.subTest(rule=rule):
                self.answers["c6-root-row"] = row("c6-root", rule, "service_agent")
                self.assert_one_fail(
                    self.run_script(),
                    "check 6 root command",
                    "expected rule default:service.root_shell",
                )


class ReusedRequestTest(DryRunTestCase):
    """A request that MEHO gives back from before the run is never this run's."""

    def test_a_reused_request_fails_and_nobody_is_asked_to_approve_it(self) -> None:
        self.answers["c2-call"] = call("c2", "awaiting_approval", request="req-old", reused=True)
        result = self.run_script()
        fails = self.fails(result)
        self.assertEqual(len(fails), 1, fails)
        self.assertIn("MEHO gave back request req-old, which waited before this run", fails[0])
        self.assertNotIn(
            "req-old", "".join(line for line in result.stdout.splitlines() if "[person]" in line)
        )
        self.assertIn(
            "[skip] check 2 (a second Operator, a person, web console): not run: no request of this run",
            result.stdout,
        )

    def test_the_agent_never_tries_to_approve_a_reused_request(self) -> None:
        self.answers["c6-harmless"] = call(
            "c6-harmless", "awaiting_approval", request="req-old", reused=True
        )
        self.assert_one_fail(self.run_script(), "check 6 agent approve", "not tried", "req-old")


class Check10CommandLineTest(DryRunTestCase):
    """The command line refuses to approve the person's own agent's request."""

    def test_passes_on_the_403_row_and_the_refusal(self) -> None:
        self.assertRegex(
            self.run_script().stdout,
            r"\[ok\]\s+check 10 \(the Operator, a person, command line\): request req-c10-cli is pending",
        )

    def test_fails_when_the_command_line_approved_or_refused_for_another_reason(self) -> None:
        cases = {
            "approved": ("c10-cli-request", request("approved", "operator-1", "operator-1")),
            "no 403 row": ("c10-cli-rows", rows(post_row("x", "/api/v1/approvals/req-c10-cli/approve", "operator-1", 200))),
            "another refusal": ("p10-cli-refusal", {"code": 0, "body": "n"}),
        }  # fmt: skip
        for name, (step, answer) in cases.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers[step] = answer
                self.assert_one_fail(
                    self.run_script(), "check 10 (the Operator, a person, command line)"
                )

    def test_needs_a_person(self) -> None:
        del self.answers["p10-cli"]
        result = self.run_script()
        self.assertIn(
            "[skip] check 10 (the Operator, a person, command line): needs a person", result.stdout
        )


class Check11Test(DryRunTestCase):
    """Check 11 covers the logins of MEHO's own code by their caller kind."""

    def sensor_row(self, kind: str | None, ts: str = "2026-01-01T00:00:06Z") -> dict[str, Any]:
        body = row("c11", "default:system", kind or "", "ok")["body"]
        if kind is None:
            del body["caller_kind"]
        body.update(target_name="web-a", principal_sub="__sensor__", ts=ts)
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

    def test_a_row_of_an_earlier_run_does_not_count(self) -> None:
        self.answers["c11-row"] = self.sensor_row("system", ts="2026-01-01T00:00:04Z")
        self.assert_one_fail(self.run_script(), "check 11 (the check runner", "no audit row")

    def test_reads_the_sensor_from_the_list_and_fails_at_once_without_it(self) -> None:
        self.answers["c11-sensors"]["body"]["items"].pop()
        result = self.run_script()
        self.assert_one_fail(result, "check 11 (the check runner", "could not read the sensor")

    def test_the_web_console_reader_needs_the_kind_person(self) -> None:
        self.assertIn("[ok]   check 11 (the web console reader", self.run_script().stdout)
        self.answers["c1-row"] = rows(run_row("c1", "override:ov-1", "system"))
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
        self.assertEqual(result.stdout.count("not run: waits for the person step 6"), 7)
        self.assertNotRegex(result.stdout, r"\[(ok|FAIL)\]\s+check 6 (delete|destructive|root)")

    def test_the_console_approvals_need_a_person(self) -> None:
        for step in ("p2-admin", "p5-with", "p10-web", "p10-cli"):
            del self.answers[step]
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        for line in (
            "[skip] check 2 (an Admin, a person, web console): needs a person",
            "[skip] check 5 (a person with the custom role, web console): needs a person",
            "[skip] check 10 (the Operator, a person, web console): needs a person",
            "[skip] check 10 (the Operator, a person, command line): needs a person",
        ):
            self.assertIn(line, result.stdout)


class TakeBackTest(DryRunTestCase):
    """The run takes back only what it made, and names what it could not."""

    def left_lines(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return [line for line in result.stdout.splitlines() if line.startswith("[left]")]

    def test_what_could_not_be_taken_back_is_named(self) -> None:
        self.answers["withdraw-req-3"] = {"code": 403, "body": {"detail": "not_request_sender"}}
        self.answers["takeback-overrides"] = self.answers["c1-overrides"]
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        left = self.left_lines(result)
        self.assertEqual(len(left), 4, left)
        self.assertIn("request req-3 (sent with PERSON_TOKEN): not withdrawn, HTTP 403", left[0])
        self.assertIn("the lowering of k8s.scale (check 1): not as before the run", left[1])
        self.assertIn("the personal agent rule of check 4", left[2])
        self.assertIn("the custom role Approvers-VMW of check 5", left[3])

    def test_a_request_whose_state_cannot_be_read_is_named(self) -> None:
        self.answers["withdraw-req-4-status"] = {"code": 500, "body": {"detail": "boom"}}
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        self.assertEqual(
            self.left_lines(result),
            [
                "[left] request req-4 (sent with SERVICE_TOKEN): its state could not be read, HTTP 500",
                *REMOVAL_NOT_CONFIRMED,
            ],
        )
        self.assertIn("3 not taken back", result.stdout)

    def test_asks_only_for_the_person_steps_that_were_done(self) -> None:
        for step in ("p1", "p5-role", "p6-lower", "p8", "p9"):
            del self.answers[step]
        result = self.run_script()
        question = next(
            x for x in result.stdout.splitlines() if x.startswith("[person] Take back.")
        )
        self.assertIn("remove the personal agent rule of check 4", question)
        for other in ("custom role", "k8s.scale", "k8s.pod.delete", "k8s.annotate"):
            self.assertNotIn(other, question)

    def test_no_question_when_no_person_step_was_done(self) -> None:
        for step in ("p1", "p4", "p5-role", "p6-delete", "p8", "p9"):
            del self.answers[step]
        result = self.run_script()
        self.assertNotIn("Take back.", result.stdout)
        self.assertEqual(self.left_lines(result), [])

    def test_the_override_of_check_9_is_read_back(self) -> None:
        del self.answers["p8"]
        override = {
            "connector_id": "k8s", "op_id": "k8s.annotate", "scope": "tenant",
            "safety_level": "dangerous", "requires_approval": None,
        }  # fmt: skip
        self.answers["takeback-overrides"] = {"code": 200, "body": {"overrides": [override]}}
        result = self.run_script()
        self.assertEqual(
            self.left_lines(result),
            [*REMOVAL_NOT_CONFIRMED,
             "[left] the override of k8s.annotate (check 9): not as before the run: tenant "
             "override: added (risk level dangerous, category not set, labels none, always wait "
             "not set)"],
        )  # fmt: skip

    def test_an_override_from_before_the_run_is_set_back_not_removed(self) -> None:
        before = {
            "connector_id": "k8s", "op_id": "k8s.scale", "scope": "tenant",
            "safety_level": "dangerous", "requires_approval": True, "category": "write",
            "labels": {"team": "web", "area": "apps"}, "always_wait": True,
        }  # fmt: skip
        self.answers["before-overrides"] = {"code": 200, "body": {"overrides": [before]}}
        self.answers["takeback-overrides"] = {"code": 200, "body": {"overrides": [before]}}
        result = self.run_script()
        self.assertIn(
            "set the overrides of k8s.scale back to how they were before the run "
            '(tenant override: risk level dangerous, category write, labels {"area":"apps",'
            '"team":"web"}, always wait true; category, labels and the "always wait" mark are '
            "the values that apply to the operation, which may come from the policy file)",
            result.stdout,
        )
        self.assertEqual(
            self.left_lines(result),
            ["[left] the lowering of k8s.scale (check 1): approval setting not confirmed",
             *REMOVAL_NOT_CONFIRMED],
        )  # fmt: skip

    def lost_setting(self, field: str, value: Any) -> list[str]:
        """An override from before the run that has lost one stored setting at the end."""
        before = {
            "connector_id": "k8s", "op_id": "k8s.scale", "scope": "tenant",
            "safety_level": "dangerous", "requires_approval": None, "category": "write",
            "labels": {"team": "web"}, "always_wait": False,
        }  # fmt: skip
        self.answers["before-overrides"] = {"code": 200, "body": {"overrides": [before]}}
        after = dict(before, **{field: value})
        self.answers["takeback-overrides"] = {"code": 200, "body": {"overrides": [after]}}
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        left = self.left_lines(result)
        self.assertEqual(
            left[-3:],
            ["[left] the lowering of k8s.scale (check 1): approval setting not confirmed",
             *REMOVAL_NOT_CONFIRMED],
        )  # fmt: skip
        return left[:-3]

    def test_an_override_from_before_the_run_that_lost_its_labels_is_named(self) -> None:
        self.assertEqual(
            self.lost_setting("labels", {}),
            ["[left] the lowering of k8s.scale (check 1): not as before the run: tenant "
             'override: labels is none, before the run {"team":"web"}'],
        )  # fmt: skip

    def test_an_override_from_before_the_run_that_lost_its_category_is_named(self) -> None:
        self.assertEqual(
            self.lost_setting("category", None),
            ["[left] the lowering of k8s.scale (check 1): not as before the run: tenant "
             "override: category is not set, before the run write"],
        )  # fmt: skip

    def test_an_always_wait_mark_left_changed_is_named(self) -> None:
        self.assertEqual(
            self.lost_setting("always_wait", True),
            ["[left] the lowering of k8s.scale (check 1): not as before the run: tenant "
             "override: always wait is true, before the run false"],
        )  # fmt: skip

    def test_check_9_does_not_run_with_an_override_from_before_the_run(self) -> None:
        before = {
            "connector_id": "k8s", "op_id": "k8s.annotate", "scope": "install",
            "safety_level": "caution", "requires_approval": None, "labels": {"team": "web"},
        }  # fmt: skip
        self.answers["before-overrides"] = {"code": 200, "body": {"overrides": [before]}}
        self.answers["takeback-overrides"] = {"code": 200, "body": {"overrides": [before]}}
        result = self.run_script()
        self.assertIn(
            "[skip] check 9 (an Admin, a person, web console): not run: an override from "
            "before the run",
            result_lines(result.stdout),
        )
        self.assertNotIn("[person] Check 9.", result.stdout)
        self.assertNotIn("(check 9)", result.stdout)
        self.assertEqual(
            self.left_lines(result),
            [*REMOVAL_NOT_CONFIRMED,
             "[left] an override of k8s.annotate (check 8): approval setting not confirmed"],
        )  # fmt: skip

    def test_a_tenant_lowering_left_next_to_an_install_override_is_named(self) -> None:
        install = {
            "connector_id": "k8s", "op_id": "k8s.scale", "scope": "install",
            "safety_level": "dangerous", "requires_approval": None,
        }  # fmt: skip
        tenant = dict(install, scope="tenant", safety_level="caution")
        self.answers["before-overrides"] = {"code": 200, "body": {"overrides": [install]}}
        self.answers["takeback-overrides"] = {"code": 200, "body": {"overrides": [install, tenant]}}
        result = self.run_script()
        self.assertEqual(
            self.left_lines(result),
            ["[left] the lowering of k8s.scale (check 1): not as before the run: tenant "
             "override: added (risk level caution, category not set, labels none, always wait "
             "not set)",
             "[left] the lowering of k8s.scale (check 1): approval setting not confirmed",
             *REMOVAL_NOT_CONFIRMED],
        )  # fmt: skip
        self.assertIn("4 not taken back", result.stdout)

    def test_an_approval_setting_left_changed_is_named(self) -> None:
        """The list shows the approval that applies, so the stored one is never confirmed."""
        before = {
            "connector_id": "k8s", "op_id": "k8s.scale", "scope": "tenant",
            "safety_level": "dangerous", "requires_approval": True,
        }  # fmt: skip
        self.answers["before-overrides"] = {"code": 200, "body": {"overrides": [before]}}
        for after in (dict(before, requires_approval=False), before):
            with self.subTest(after=after):
                self.answers["takeback-overrides"] = {"code": 200, "body": {"overrides": [after]}}
                result = self.run_script()
                self.assertEqual(
                    self.left_lines(result),
                    ["[left] the lowering of k8s.scale (check 1): approval setting not confirmed",
                     *REMOVAL_NOT_CONFIRMED],
                )  # fmt: skip

    def test_an_override_read_that_fails_at_the_start_skips_the_four_edit_steps(self) -> None:
        self.answers["before-overrides"] = {"code": 500, "body": {"detail": "boom"}}
        result = self.run_script()
        reason = "not run: the overrides could not be read at the start"
        for line in (
            f"[skip] check 1 (an Admin and an Operator, persons, web console): {reason}",
            "[skip] check 6 delete after a lowering (an Operator's token, a personal agent, "
            f"REST): {reason}",
            f"[skip] check 8 (an Admin, a person, web console): {reason}",
            f"[skip] check 9 (an Admin, a person, web console): {reason}",
        ):
            self.assertIn(line, result.stdout)
        for asked in ("[person] Check 1.", "[person] Check 6. As an Admin", "[person] Check 8.",
                      "[person] Check 9."):  # fmt: skip
            self.assertNotIn(asked, result.stdout)
        question = next(
            x for x in result.stdout.splitlines() if x.startswith("[person] Take back.")
        )
        self.assertNotIn("override", question)
        for op in ("k8s.scale", "k8s.pod.delete", "k8s.annotate"):
            self.assertNotIn(op, question)
        self.assertEqual(self.left_lines(result), REMOVAL_NOT_CONFIRMED)

    def test_a_rule_left_in_the_policy_file_is_named_although_explain_is_the_same(self) -> None:
        del self.answers["p5-role"]
        result = self.run_script()
        self.assertEqual(
            self.left_lines(result),
            ["[left] the personal agent rule of check 4: removal not confirmed"],
        )
        self.assertEqual(result.returncode, NOT_COMPLETE)

    def test_a_role_that_cannot_be_read_is_not_confirmed_as_removed(self) -> None:
        del self.answers["p4"]
        result = self.run_script()
        self.assertEqual(
            self.left_lines(result),
            ["[left] the custom role Approvers-VMW of check 5: removal not confirmed"],
        )

    def test_a_policy_file_that_cannot_be_read_at_the_start_is_not_confirmed(self) -> None:
        result = self.run_script()
        left = self.left_lines(result)
        self.assertEqual(len(left), 2, left)
        for line in left:
            self.assertIn("removal not confirmed", line)

    def test_withdraws_a_request_of_its_own_logins_that_the_list_does_not_hold(self) -> None:
        self.answers["withdraw-queue"] = {
            "code": 200,
            "body": [
                {"id": "req-cut", "principal_sub": "service-1", "created_at": "2026-01-01T00:00:01Z",
                 "work_ref": f"{WORK_REF}-c6-root"},
                {"id": "req-other", "principal_sub": "someone-else", "created_at": "2026-01-01T00:00:01Z"},
                {"id": "req-before", "principal_sub": "service-1", "created_at": "2025-12-31T23:59:59Z"},
            ],
        }  # fmt: skip
        self.answers["withdraw-req-cut-status"] = request("pending", "service-1")
        self.answers["withdraw-req-cut"] = {"code": 403, "body": {"detail": "x"}}
        result = self.run_script()
        self.assertEqual(
            self.left_lines(result),
            ["[left] request req-cut (sent with SERVICE_TOKEN): not withdrawn, HTTP 403",
             *REMOVAL_NOT_CONFIRMED],
        )  # fmt: skip

    def test_a_request_of_its_logins_without_this_runs_work_reference_stays(self) -> None:
        self.answers["withdraw-queue"] = {
            "code": 200,
            "body": [
                {"id": "req-session", "principal_sub": "operator-1", "created_at": FAR_FUTURE},
                {"id": "req-ticket", "principal_sub": "operator-1", "created_at": FAR_FUTURE,
                 "work_ref": "CHG0012345"},
            ],
        }  # fmt: skip
        result = self.run_script()
        self.assertIn(
            "[note] request req-session of PERSON_TOKEN waits, but it does not carry this run's "
            "work reference (it has none); it stays",
            result.stdout,
        )
        self.assertIn("[note] request req-ticket of PERSON_TOKEN waits", result.stdout)
        self.assertEqual(self.left_lines(result), REMOVAL_NOT_CONFIRMED)

    def test_a_request_of_another_run_of_the_script_stays(self) -> None:
        other = "custom-policy-check-0123456789abcdef-c6-rights"
        self.answers["withdraw-queue"] = {
            "code": 200,
            "body": [
                {"id": "req-run", "principal_sub": "operator-1", "created_at": FAR_FUTURE,
                 "work_ref": other},
                {"id": "req-prefix", "principal_sub": "operator-1", "created_at": FAR_FUTURE,
                 "work_ref": WORK_REF + "x-c6-rights"},
            ],
        }  # fmt: skip
        result = self.run_script()
        self.assertIn(f"[note] request req-run of PERSON_TOKEN waits, but it does not carry "
                      f"this run's work reference (it has {other}); it stays", result.stdout)  # fmt: skip
        self.assertIn("[note] request req-prefix of PERSON_TOKEN waits", result.stdout)
        self.assertNotIn("withdraw-req-run", result.stdout)
        self.assertEqual(self.left_lines(result), REMOVAL_NOT_CONFIRMED)

    def test_a_queue_answer_that_is_not_a_list_is_never_read_as_empty(self) -> None:
        for body in ({"items": []}, "<html>busy</html>", None, [1, 2]):
            with self.subTest(body=body):
                self.answers["withdraw-queue"] = {"code": 200, "body": body}
                result = self.run_script()
                self.assertEqual(result.returncode, NOT_COMPLETE, result.stdout + result.stderr)
                left = self.left_lines(result)
                self.assertEqual(left[1:], REMOVAL_NOT_CONFIRMED)
                self.assertTrue(
                    left[0].startswith(
                        "[left] the waiting requests of this run: could not read the queue (HTTP 200"
                    ),
                    left[0],
                )
                self.assertIn("3 not taken back", result.stdout)

    def test_a_queue_that_does_not_answer_200_is_named(self) -> None:
        self.answers["withdraw-queue"] = {"code": 502, "body": {"detail": "bad gateway"}}
        result = self.run_script()
        self.assertEqual(result.returncode, NOT_COMPLETE)
        self.assertEqual(
            self.left_lines(result),
            ["[left] the waiting requests of this run: could not read the queue (HTTP 502)",
             *REMOVAL_NOT_CONFIRMED],
        )  # fmt: skip


class RemovalReadBackTest(DryRunTestCase):
    """The agent rule and the custom role are read back only from where MEHO keeps them."""

    def test_an_unchanged_policy_file_is_no_proof_that_they_are_gone(self) -> None:
        self.answers["before-policy-file"] = {"code": 200, "body": "policy: v1\n"}
        self.answers["takeback-policy-file"] = {"code": 200, "body": "policy: v1\n"}
        result = self.run_script()
        left = [line for line in result.stdout.splitlines() if line.startswith("[left]")]
        self.assertEqual(left, REMOVAL_NOT_CONFIRMED)
        self.assertNotIn("policy/export", SCRIPT.read_text())


class RefusedClickTest(DryRunTestCase):
    """A person who may not approve first looks at the Approve button."""

    CASES = (("p2-operator", "c2-operator", "req-2", "check 2 (a second Operator, a person, web console)"),
             ("p5-without", "c5-without", "req-5", "check 5 (a person without the role, web console)"))  # fmt: skip
    CLICKS = {"p2-operator": "Check 2. As a second Operator (not the one of CPC_PERSON_TOKEN), in "
              "the web console, click Approve", "p5-without": "Check 5. As the person WITHOUT the "
              "custom role, in the web console, click Approve"}  # fmt: skip

    def test_a_button_that_is_off_passes_without_a_click_row(self) -> None:
        for person, step, req, check in self.CASES:
            with self.subTest(step=step):
                self.answers = good_answers()
                self.answers[f"{person}-button"] = {"code": 0, "body": "n"}
                self.answers[f"{step}-off"] = {"code": 0, "body": "y"}
                self.answers[f"{step}-rows"] = rows()
                self.answers[f"{step}-request"] = request("pending")
                result = self.run_script()
                self.assertEqual(self.fails(result), [])
                self.assertIn(
                    f"[ok]   {check}: the Approve button is off, request {req} is pending",
                    result.stdout,
                )
                self.assertNotIn(self.CLICKS[person], result.stdout)
                self.assertIn(self.CLICKS[person], self.run_script_with_button_on(person))

    def run_script_with_button_on(self, person: str) -> str:
        self.answers[f"{person}-button"] = {"code": 0, "body": "y"}
        return self.run_script().stdout

    def test_a_button_that_is_off_needs_the_reason_and_a_request_that_waits(self) -> None:
        for person, step, _req, check in self.CASES:
            for name, off, status in (("no reason", "n", "pending"), ("approved", "y", "approved")):
                with self.subTest(step=step, case=name):
                    self.answers = good_answers()
                    self.answers[f"{person}-button"] = {"code": 0, "body": "n"}
                    self.answers[f"{step}-off"] = {"code": 0, "body": off}
                    self.answers[f"{step}-request"] = request(status)
                    self.assert_one_fail(self.run_script(), check)

    def test_the_questions_name_the_sign_in_again_banner_as_a_no(self) -> None:
        result = self.run_script()
        self.assertIn('A "Sign in again" banner, a sign-in page or an error is a no.', result.stdout)
        self.answers["p2-operator-button"] = {"code": 0, "body": "n"}
        self.assertIn('A "Sign in again" banner is a no.', self.run_script().stdout)


class Check8ConsoleRowTest(DryRunTestCase):
    """Check 8 in the console counts only the Admin's refused edit of the named write."""

    def test_counts_only_a_400_row_of_the_admin_of_the_token(self) -> None:
        path = "/ui/connectors/registry/k8s/operations/k8s.annotate"
        cases = {
            "another Admin": post_row("c8-console", path, "admin-1", 400, "PATCH"),
            "not found": post_row("c8-console", path, "admin-0", 404, "PATCH"),
            "a login refusal": post_row("c8-console", path, "admin-0", 403, "PATCH"),
        }
        for name, entry in cases.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c8-console-rows"] = rows(entry)
                self.assert_one_fail(
                    self.run_script(), "check 8 (an Admin, a person, web console)", "HTTP 400"
                )

    def test_the_admins_400_row_with_a_yes_passes_and_skips_the_write_rule_reason(self) -> None:
        result = self.run_script()
        lines = result_lines(result.stdout)
        passed = [line for line in lines if line.startswith("[ok]   check 8 (an Admin, a person")]
        self.assertEqual(len(passed), 1, lines)
        self.assertIn(f"refused edit rows {audit_id('c8-console')}", passed[0])
        self.assertIn(
            "[skip] check 8 (an Admin, a person, web console, the reason of the write rule): "
            "not run: waits for #6966",
            lines,
        )

    def test_the_question_counts_the_refusal_below_the_classification_as_a_yes(self) -> None:
        text = " ".join(SCRIPT.read_text().split())
        self.assertIn(
            'Did MEHO show \\"Edit rejected\\" and keep the risk level? The refusal that the '
            "level is lower than the classification is a yes.",
            text,
        )

    def test_a_no_fails(self) -> None:
        self.answers["c8-refusal"] = {"code": 0, "body": "n"}
        self.assert_one_fail(
            self.run_script(),
            "check 8 (an Admin, a person, web console)",
            '"Edit rejected" with the risk level kept',
        )


class Check9EditRowsTest(DryRunTestCase):
    """Check 9 counts a raise to dangerous by the Admin, then the removal."""

    def test_needs_the_admins_raise_to_dangerous_then_its_removal(self) -> None:
        remove, raise_ = check_9_rows()
        cases = {
            "another Admin": rows(*check_9_rows(sub="admin-1")),
            "a raise to caution": rows(remove, edit_row("c9-raise", "k8s.annotate", "admin-0",
                                                        level="caution", ts=raise_["ts"])),
            "the removal first": rows(dict(remove, ts="2026-01-01T00:00:31.000000Z"), raise_),
            "another connector": rows(*(dict(r, payload=dict(r["payload"], connector_id="k9s"))
                                        for r in (remove, raise_))),
        }  # fmt: skip
        for name, answer in cases.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c9-rows"] = answer
                self.assert_one_fail(self.run_script(), "check 9 (an Admin")


class Check6PolicyChangeTest(DryRunTestCase):
    """An Operator's policy.apply is refused; the Admin's token waits."""

    def test_the_operator_is_refused_and_the_admin_waits(self) -> None:
        result = self.run_script()
        self.assertRegex(
            result.stdout,
            r"\[ok\]\s+check 6 change of the policy \(an Operator's AI tool, a personal agent, "
            r"MCP\): refused",
        )
        self.assertRegex(
            result.stdout,
            r"\[ok\]\s+check 6 change of the policy \(an Admin's AI tool, a personal agent, "
            r"MCP\): waits",
        )

    def test_an_operators_policy_change_that_waits_fails(self) -> None:
        self.answers["c6-policy-operator"] = call("c6-po", "awaiting_approval", request="req-po")
        self.answers["c6-policy-operator-row"] = row("c6-po", "floor:policy_change_waits", "personal_agent")
        self.answers["withdraw-req-po-status"] = request("pending")
        self.answers["withdraw-req-po"] = {"code": 200, "body": {"status": "withdrawn"}}
        self.assert_one_fail(
            self.run_script(),
            "check 6 change of the policy (an Operator's AI tool",
            "expected refused",
            'expected the reason "needs the tenant_admin role"',
        )


class PreviewErrorProbeTest(DryRunTestCase):
    """A preview that answers status error is no probe answer."""

    def error_preview(self) -> dict[str, Any]:
        return {
            "code": 200,
            "body": {
                "status": "error",
                "policy_verdict": "needs-approval",
                "extras": {"error_code": "invalid_params"},
            },
        }

    def test_no_baseline_is_saved_and_the_earlier_file_stays(self) -> None:
        self.answers["ready"] = ready(False)
        i = PROBE_KEYS.index("6 PERSON_TOKEN CREDENTIAL_WRITE token") + 1
        self.answers[f"baseline-{i}-preview"] = self.error_preview()
        out = self.dir / "kept-baseline.tsv"
        out.write_text("the earlier baseline\n")
        result = self.run_script("--record-baseline", str(out))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("status error (invalid_params)", result.stdout)
        self.assertEqual(out.read_text(), "the earlier baseline\n")

    def test_check_7_fails_on_it(self) -> None:
        i = PROBE_KEYS.index("6 PERSON_TOKEN CREDENTIAL_WRITE token") + 1
        self.answers[f"c7-{i}-preview"] = self.error_preview()
        self.assert_one_fail(
            self.run_script(), "check 7 (as check 6: PERSON_TOKEN CREDENTIAL_WRITE", "status error"
        )


class Check1RunRowTest(DryRunTestCase):
    """Check 1 takes only the Operator's run of the operation after the Admin's edit."""

    def test_takes_only_the_run_of_the_operator_on_the_target_after_the_edit(self) -> None:
        cases = {
            "another login": run_row("c1", "override:ov-1", "person", sub="operator-2"),
            "another target": run_row("c1", "override:ov-1", "person", target="web-b"),
            "another connector": run_row("c1", "override:ov-1", "person", impl="k9s"),
            "before the edit": run_row("c1", "override:ov-1", "person", ts=C1_START),
        }
        for name, entry in cases.items():
            with self.subTest(case=name):
                self.answers = good_answers()
                self.answers["c1-row"] = rows(entry)
                result = self.run_script()
                self.assertRegex(
                    result.stdout, r"\[FAIL\] check 1 \(the Operator.*no run row of the Operator"
                )

    def test_fails_without_the_servers_time_and_asks_nobody(self) -> None:
        self.answers["c1-start-row"] = rows()
        result = self.run_script()
        self.assertIn(
            "[FAIL] check 1 (an Admin and an Operator, persons, web console): could not read "
            "the server's time at the step start; nobody was asked",
            result.stdout,
        )
        self.assertNotIn("[person] Check 1.", result.stdout)


class Check11TimeTest(DryRunTestCase):
    """Check 11 compares full timestamps and needs a fresh result of the named sensor."""

    def sensor_row(self, ts: str) -> dict[str, Any]:
        body = row("c11", "default:system", "system", "ok")["body"]
        body.update(target_name="web-a", principal_sub="__sensor__", ts=ts)
        return rows(body)

    def test_a_row_in_the_same_second_before_the_run_call_does_not_count(self) -> None:
        self.answers["c11-row"] = self.sensor_row("2026-01-01T00:00:05.000001Z")
        self.assert_one_fail(self.run_script(), "check 11 (the check runner", "no audit row")
        self.answers["c11-row"] = self.sensor_row("2026-01-01T00:00:05.123456+00:00")
        self.assertIn("[ok]   check 11 (the check runner", self.run_script().stdout)

    def test_needs_a_fresh_result_of_the_named_sensor(self) -> None:
        old = {"sensor_id": SENSOR_ID, "evaluated_at": "2026-01-01T00:00:05.1Z", "state": "ok"}
        other = {"sensor_id": "x", "evaluated_at": "2026-01-01T00:00:09Z", "state": "ok"}
        for items in ([], [old], [other]):
            with self.subTest(items=items):
                self.answers["c11-results"] = {"code": 200, "body": {"items": items}}
                self.assert_one_fail(
                    self.run_script(), "check 11 (the check runner", "no fresh result"
                )

    def test_follows_the_offset_of_the_sensor_list(self) -> None:
        sensor = self.answers["c11-sensors"]["body"]["items"][1]
        page = [{"id": f"00000000-0000-4000-8000-{n:012d}", "op_id": "k8s.list"} for n in range(500)]
        self.answers["c11-sensors"] = {"code": 200, "body": {"items": page, "next_cursor": None}}
        self.answers["c11-sensors-500"] = {"code": 200, "body": {"items": [sensor]}}
        self.assertIn("[ok]   check 11 (the check runner", self.run_script().stdout)


class SameRunRequestTest(DryRunTestCase):
    """A request MEHO gives back from this run's own list is this run's."""

    def test_it_is_not_reported_as_waiting_before_the_run(self) -> None:
        self.answers["c6-delete-agent"] = call(
            "c6-delete-agent", "awaiting_approval", request="req-c6-delete-person", reused=True
        )
        result = self.run_script()
        self.assertEqual(self.fails(result), [])
        self.assertNotIn("waited before this run", result.stdout)

    def test_the_second_call_of_check_10_waits_for_the_first_request(self) -> None:
        self.answers["c10-first-request"] = request("pending")
        result = self.run_script()
        for line in (
            "[skip] check 10 (the Operator's AI tool, a personal agent, MCP): not run: the "
            "first request req-c10-web still waits",
            "[skip] check 10 (the Operator, a person, command line): not run: the first "
            "request req-c10-web still waits",
        ):
            self.assertIn(line, result.stdout)
        self.assertNotIn("req-c10-cli", result.stdout)


class Check10DecideRowTest(DryRunTestCase):
    def test_an_approve_row_is_not_the_route_meho_approvals_approve_calls(self) -> None:
        self.answers["c10-cli-rows"] = rows(
            post_row("c10-cli-approve", "/api/v1/approvals/req-c10-cli/approve", "operator-1", 403)
        )
        self.assert_one_fail(
            self.run_script(), "check 10 (the Operator, a person, command line)", "decide"
        )


class OverrideReadBackTest(DryRunTestCase):
    """The override read-back compares what the list shows, as stored values."""

    def override(self, **extra: Any) -> dict[str, Any]:
        entry = {"connector_id": "k8s", "op_id": "k8s.scale", "scope": "tenant",
                 "safety_level": "dangerous", "requires_approval": True}  # fmt: skip
        entry.update(extra)
        return {"code": 200, "body": {"overrides": [entry]}}

    def test_labels_compare_as_key_and_value_pairs(self) -> None:
        self.answers["before-overrides"] = self.override(labels={"a": "b c=d"})
        self.answers["takeback-overrides"] = self.override(labels={"a": "b", "c": "d"})
        left = [x for x in self.run_script().stdout.splitlines() if x.startswith("[left]")]
        self.assertIn(
            "[left] the lowering of k8s.scale (check 1): not as before the run: tenant override: "
            'labels is {"a":"b","c":"d"}, before the run {"a":"b c=d"}',
            left,
        )

    def test_the_person_is_not_asked_to_set_an_approval_back(self) -> None:
        self.answers["before-overrides"] = self.override()
        self.answers["takeback-overrides"] = self.override()
        result = self.run_script()
        question = next(x for x in result.stdout.splitlines() if x.startswith("[person] Take back."))
        self.assertNotIn("approval", question)
        self.assertIn(
            "[left] the lowering of k8s.scale (check 1): approval setting not confirmed",
            result.stdout,
        )


class UnknownSenderTest(DryRunTestCase):
    def test_the_queue_is_read_also_when_no_login_was_read(self) -> None:
        self.answers["login-rows"] = rows()
        self.answers["withdraw-queue"] = {
            "code": 200,
            "body": [{"id": "req-cut", "principal_sub": "operator-1",
                      "created_at": FAR_FUTURE, "work_ref": f"{WORK_REF}-c6-rights"}],
        }  # fmt: skip
        result = self.run_script()
        self.assertIn(
            "[left] request req-cut (work reference of this run): not withdrawn, its sender is "
            "not known",
            result.stdout,
        )


class RuleIdTest(unittest.TestCase):
    def test_each_expected_floor_rule_is_named_by_the_rule_table_or_floors_py(self) -> None:
        table = (ROOT / "docs/codebase/policy-evaluator.md").read_text()
        floors = (ROOT / "backend/src/meho_backplane/policy/floors.py").read_text()
        named = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M))
        named |= set(re.findall(r'"([a-z_]+)"', floors))
        expected = set(re.findall(r'"floor:([a-z_]+)"', SCRIPT.read_text()))
        self.assertTrue(expected)
        self.assertEqual(expected - named, set())


class DocsPageTest(unittest.TestCase):
    """The docs page says what the run ends with."""

    def test_the_docs_page_names_the_exit_codes_and_the_work_references(self) -> None:
        text = " ".join((ROOT / "docs/acceptance/custom-policy-check.md").read_text().split())
        for sentence in (
            "Exit code 0 needs the three open parts of check 11",
            "#7073",
            "the two parts of check 8 that need the write rule's own reason (#6966)",
            "the owner's own approval in check 7 (#7132)",
            "Check 7 does not compare whether the owner may approve their own request until "
            "#7132 is merged",
            "Check 10 tests it while the switch is on.",
            "MEHO never gives the first request of check 10 back for its second call.",
            "Check 10 makes its second call only after its first request is decided.",
            "An agent's policy import (`policy.apply`) is refused before anything waits",
            "An agent's policy import (`policy.apply`, check 6) passes only when MEHO refuses it",
            "exit code 3, or 1 when a check also failed",
            "When a check failed and something was not taken back, the run ends with exit code 1",
            "Each request of a run has its own work reference.",
            "it makes no request",
            "APPROVAL_SAME_CALL_REUSE",
        ):
            self.assertIn(sentence, text)


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
        self.assertNotRegex(text, r"rest [\w-]+ POST \"/api/v1/approvals/[^\"]*/decide\"")
        self.assertNotRegex(text, r"meho_cli\s+\S+\s+approvals")
        self.assertNotRegex(text, r"(?m)^\s*meho\s+approvals")
        self.assertEqual(
            len(re.findall(r"rest [\w-]+ POST \"/api/v1/approvals/[^\"]*/approve\"", text)), 1
        )


if __name__ == "__main__":
    unittest.main()
