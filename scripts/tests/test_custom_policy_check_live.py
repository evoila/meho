# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test the live paths of the custom policy check script, with stand-in programs.

The script runs without ``--dry-run``. A stand-in ``curl`` and a stand-in
``meho`` on ``PATH`` answer from recorded files and write down their own
command lines (``custom_policy_check_fixture``). No install is contacted.
One run has a terminal of its own where a person answers "y" to each
question; the other runs have no terminal.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

from custom_policy_check_fixture import (
    BACKPLANE,
    C1_START,
    FAR_FUTURE,
    OPS,
    PROBE_KEYS,
    REMOVAL_NOT_CONFIRMED,
    WORK_REF,
    LiveRun,
    good_answers,
    ready,
    result_lines,
    token_hash,
    waiting_requests,
)

NOT_COMPLETE = 3
STOPPED = 143

#: The token each request of a full run was sent with.
MAKERS = {
    "req-2": "CPC_PERSON_TOKEN", "req-3": "CPC_PERSON_TOKEN", "req-4": "CPC_SERVICE_TOKEN",
    "req-5": "CPC_SERVICE_TOKEN", "req-harmless": "CPC_PERSON_TOKEN",
    "req-c6-cred-read": "CPC_AGENT_TOKEN", "req-c6-cred-write": "CPC_PERSON_TOKEN",
    "req-c6-rights": "CPC_PERSON_TOKEN", "req-c6-policy": "CPC_ADMIN_TOKEN",
    "req-c6-destructive": "CPC_SERVICE_TOKEN", "req-c6-root": "CPC_SERVICE_TOKEN", "req-c6-delete-person": "CPC_PERSON_TOKEN",
    "req-c6-delete-agent": "CPC_AGENT_TOKEN", "req-c6-delete-service": "CPC_SERVICE_TOKEN",
    "req-c6-delete-lowered": "CPC_PERSON_TOKEN", "req-c6-address": "CPC_PERSON_TOKEN",
    "req-c10-web": "CPC_PERSON_TOKEN", "req-c10-cli": "CPC_PERSON_TOKEN",
}  # fmt: skip


@unittest.skipUnless(
    shutil.which("jq") and shutil.which("bash") and shutil.which("python3"),
    "needs bash, jq and python3",
)
class LiveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.live = LiveRun(Path(tmp.name))

    def steps(self, calls: list[dict[str, Any]], prog: str = "curl") -> list[str]:
        return [c["step"] for c in calls if c["prog"] == prog]


class FullLiveRunTest(LiveTestCase):
    """One full run on a terminal of its own, where a person answers "y" to each question."""

    full_run: ClassVar[Any] = None
    full_calls: ClassVar[list[dict[str, Any]]] = []
    full_tokens: ClassVar[dict[str, str]] = {}
    full_secret: ClassVar[str] = ""

    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        run = LiveRun(Path(tmp.name))
        cls.full_run = run.run(good_answers(), terminal=True)
        cls.full_calls = run.calls()
        cls.full_tokens = dict(run.tokens)
        cls.full_secret = run.secret

    def by_step(self, step: str) -> list[dict[str, Any]]:
        return [c for c in self.full_calls if c["step"] == step]

    def body(self, step: str) -> dict[str, Any]:
        entry = self.by_step(step)[-1]
        return json.loads(entry["body"])

    def test_a_full_run_passes_every_check(self) -> None:
        result = self.full_run
        self.assertEqual(result.returncode, NOT_COMPLETE, result.stdout + result.stderr)
        self.assertNotIn("[FAIL]", result.stdout)
        left = [line for line in result.stdout.splitlines() if line.startswith("[left]")]
        self.assertEqual(left, REMOVAL_NOT_CONFIRMED)
        self.assertIn("passed, 0 failed, 4 not run, 2 not taken back", result.stdout)

    def test_every_call_goes_to_the_checked_install(self) -> None:
        self.assertTrue(self.full_calls)
        for entry in self.full_calls:
            if entry["prog"] == "curl":
                self.assertTrue(entry["url"].startswith(BACKPLANE + "/"), entry)
            else:
                argv = entry["argv"]
                self.assertIn("--backplane", argv, entry)
                self.assertEqual(argv[argv.index("--backplane") + 1], BACKPLANE)
        self.assertIn("c6-cred-write", self.steps(self.full_calls, "meho"))
        self.assertIn(f"[note] the install: {BACKPLANE}", self.full_run.stdout)
        self.assertIn("[note] the install's version: abc1234 (chart 1.2.3)", self.full_run.stdout)

    def test_no_token_or_param_value_is_on_a_command_line(self) -> None:
        secrets = [*self.full_tokens.values(), self.full_secret]
        for entry in self.full_calls:
            line = " ".join(entry["argv"])
            for secret in secrets:
                self.assertNotIn(secret, line, entry["step"])
        cred_write = self.by_step("c6-cred-write")[-1]
        self.assertIn(self.full_secret, cred_write["params"])
        params_arg = cred_write["argv"][cred_write["argv"].index("--params") + 1]
        self.assertTrue(params_arg.startswith("@"), params_arg)
        for secret in secrets:
            self.assertNotIn(secret, self.full_run.stdout + self.full_run.stderr)

    def test_a_call_without_a_target_has_no_target(self) -> None:
        for step in ("c6-rights", "c2-level", "c2-call", "c7-2", "c6-policy-agent"):
            body = self.body(step)
            if "params" in body and "arguments" in body.get("params", {}):
                body = body["params"]["arguments"]
            self.assertNotIn("target", body, step)
        self.assertEqual(self.body("c6-delete-person")["target"], "web-a")
        argv = self.by_step("c6-cred-write")[-1]["argv"]
        self.assertNotIn("--target", argv)

    def test_destructive_calls_and_deletes_send_the_hash_of_their_own_preview(self) -> None:
        steps = [
            "c3-call", "c6-destructive", "c6-delete-person", "c6-delete-service",
            "c6-delete-grant", "c6-delete-lowered", "c6-delete-agent",
        ]  # fmt: skip
        for step in steps:
            with self.subTest(step=step):
                call = self.by_step(step)[-1]
                seen = self.by_step(f"{step}-preview")[-1]
                call_body, preview_body = json.loads(call["body"]), json.loads(seen["body"])
                if step == "c6-delete-agent":
                    call_body = call_body["params"]["arguments"]
                    preview_body = preview_body["params"]["arguments"]
                    self.assertTrue(seen["url"].endswith("/mcp"))
                self.assertEqual(call_body.pop("preview_hash"), f"h-{step}")
                self.assertEqual(call_body, preview_body)
                self.assertEqual(call["auth"], seen["auth"])

    def test_the_agent_approve_names_only_the_harmless_request(self) -> None:
        approves = [c for c in self.full_calls if (c.get("url") or "").endswith("/approve")]
        self.assertEqual(len(approves), 1, approves)
        self.assertEqual(approves[0]["url"], f"{BACKPLANE}/api/v1/approvals/req-harmless/approve")
        self.assertEqual(approves[0]["auth"], token_hash(self.full_tokens["CPC_AGENT_TOKEN"]))
        self.assertEqual(json.loads(approves[0]["body"]), {"params": {}})
        self.assertFalse([c for c in self.full_calls if "/decide" in (c.get("url") or "")])
        for entry in self.full_calls:
            if entry["prog"] == "meho":
                self.assertNotIn("approvals", entry["argv"])

    def test_no_two_calls_send_the_same_work_reference(self) -> None:
        refs: list[str] = []
        runs = set()
        for entry in self.full_calls:
            argv = entry["argv"]
            if entry["prog"] == "curl":
                headers = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-H"]
                found = [h.split(" ", 1)[1] for h in headers if h.startswith("Meho-Work-Ref: ")]
            else:
                found = [argv[argv.index("--work-ref") + 1]] if "--work-ref" in argv else []
            self.assertEqual(len(found), 1, entry)
            ref = found[0]
            # The MCP start of a step has a reference of its own too.
            step = entry["step"] + ("-init" if ref.endswith(entry["step"] + "-init") else "")
            self.assertTrue(ref.endswith("-" + step), (ref, step))
            runs.add(ref[: -len(step) - 1])
            refs.append(ref)
        duplicates = sorted({r for r in refs if refs.count(r) > 1})
        self.assertEqual(duplicates, [])
        self.assertEqual(len(runs), 1, runs)
        run = runs.pop()
        self.assertRegex(run, r"^custom-policy-check-[0-9a-f]{16}$")
        self.assertIn(f"[note] this run's work reference: {run}", self.full_run.stdout)
        for step in ("c2-call", "c6-delete-person", "c6-delete-agent", "c6-cred-write", "c10-cli"):
            self.assertIn(f"{run}-{step}", refs, step)

    def test_the_policy_change_waits_for_the_admins_token_and_refuses_the_operators(self) -> None:
        tokens = {"c6-policy": "CPC_ADMIN_TOKEN", "c6-policy-operator": "CPC_PERSON_TOKEN"}
        for step, token in tokens.items():
            calls = self.by_step(step)
            self.assertTrue(calls, step)
            self.assertEqual(calls[-1]["auth"], token_hash(self.full_tokens[token]), step)
        probe = PROBE_KEYS.index("6 ADMIN_TOKEN POLICY_CHANGE token") + 1
        self.assertEqual(
            self.by_step(f"c7-{probe}")[-1]["auth"],
            token_hash(self.full_tokens["CPC_ADMIN_TOKEN"]),
        )

    def test_check_1_searches_from_the_servers_time_of_the_person_step(self) -> None:
        for step in ("c1-edits", "c1-row"):
            self.assertEqual(self.body(step)["since"], C1_START, step)

    def test_withdraws_each_waiting_request_with_the_login_that_made_it(self) -> None:
        withdrawn = {}
        for entry in self.full_calls:
            url = entry.get("url") or ""
            if url.endswith("/withdraw"):
                withdrawn[url.split("/")[-2]] = entry["auth"]
        self.assertEqual(sorted(withdrawn), sorted(waiting_requests()))
        for req, auth in withdrawn.items():
            self.assertEqual(auth, token_hash(self.full_tokens[MAKERS[req]]), req)


class NoTerminalTest(LiveTestCase):
    def test_with_no_terminal_no_delete_is_sent_and_the_run_is_not_complete(self) -> None:
        result = self.live.run(good_answers())
        self.assertEqual(result.returncode, NOT_COMPLETE, result.stdout + result.stderr)
        calls = self.live.calls()
        guarded = [
            OPS[name].split()[1]
            for name in ("CPC_OP_DESTRUCTIVE", "CPC_OP_ROOT_SHELL", "CPC_OP_DELETE")
        ]
        for entry in calls:
            for op_id in guarded:
                self.assertNotIn(f'"{op_id}"', entry.get("body") or "", entry["step"])
        self.assertNotIn("Take back.", result.stdout)
        self.assertNotIn("[left]", result.stdout)
        lines = result_lines(result.stdout)
        self.assertNotIn("[FAIL]", result.stdout)
        for line in (
            "[skip] check 2 (a second Operator, a person, web console): needs a person",
            "[skip] check 2 (an Admin, a person, web console): needs a person",
            "[skip] check 5 (an Admin, a person, web console): needs a person",
            "[skip] check 6 delete (a service login with a delete grant, a service agent, REST): "
            "not run: waits for the person step 6",
            "[skip] check 10 (the Operator, a person, web console): needs a person",
            "[skip] check 10 (the Operator, a person, command line): needs a person",
            "[skip] check 7 (every probe, REST policy explain): not run: waits for the person step 7",
        ):
            self.assertIn(line, lines)

    def test_a_stopped_run_withdraws_its_requests(self) -> None:
        result = self.live.run(good_answers(), CPC_STUB_STOP_AT="c6-rights")
        self.assertEqual(result.returncode, STOPPED, result.stdout + result.stderr)
        calls = self.live.calls()
        stop = next(i for i, c in enumerate(calls) if c["step"] == "c6-rights")
        after = [c["step"] for c in calls[stop + 1 :]]
        self.assertTrue(after and all(s.startswith("withdraw-") for s in after), after)
        withdrawn = {
            (c.get("url") or "").split("/")[-2]
            for c in calls
            if (c.get("url") or "").endswith("/withdraw")
        }
        self.assertTrue({"req-3", "req-c6-cred-read", "req-c6-cred-write"} <= withdrawn, withdrawn)

    def test_a_stop_during_the_clean_up_names_each_request_not_handled_yet(self) -> None:
        result = self.live.run(good_answers(), CPC_STUB_STOP_AT="withdraw-req-c6-rights-status")
        self.assertEqual(result.returncode, STOPPED, result.stdout + result.stderr)
        left = [line for line in result.stdout.splitlines() if line.startswith("[left]")]
        not_handled = ["req-c6-rights", "req-c6-policy", "req-c6-address", "req-harmless",
                       "req-c10-web", "req-c10-cli"]  # fmt: skip
        self.assertEqual(
            left,
            [f"[left] request {req} (sent with {MAKERS[req].removeprefix('CPC_')}): not "
             "withdrawn, the run was stopped" for req in not_handled],
        )  # fmt: skip

    def test_a_stopped_call_is_found_in_the_queue_and_withdrawn(self) -> None:
        answers = good_answers()
        answers["withdraw-queue"] = {
            "code": 200,
            "body": [
                {
                    "id": "req-cut",
                    "principal_sub": "operator-1",
                    "created_at": "2999-01-01T00:00:00Z",
                    "work_ref": f"{WORK_REF}-c6-rights",
                }
            ],
        }
        answers["withdraw-req-cut-status"] = {"code": 200, "body": {"status": "pending"}}
        answers["withdraw-req-cut"] = {"code": 200, "body": {"status": "withdrawn"}}
        result = self.live.run(answers, CPC_STUB_STOP_AT="c6-rights")
        self.assertEqual(result.returncode, STOPPED, result.stdout + result.stderr)
        cut = [c for c in self.live.calls() if (c.get("url") or "").endswith("/req-cut/withdraw")]
        self.assertEqual(len(cut), 1)
        self.assertEqual(cut[0]["auth"], token_hash(self.live.tokens["CPC_PERSON_TOKEN"]))

    def test_a_request_of_the_same_operator_in_another_session_still_waits(self) -> None:
        answers = good_answers()
        answers["withdraw-queue"] = {
            "code": 200,
            "body": [
                {"id": "req-session", "principal_sub": "operator-1", "created_at": FAR_FUTURE},
                {"id": "req-cut", "principal_sub": "operator-1", "created_at": FAR_FUTURE,
                 "work_ref": f"{WORK_REF}-c6-rights"},
            ],
        }  # fmt: skip
        answers["withdraw-req-cut-status"] = {"code": 200, "body": {"status": "pending"}}
        answers["withdraw-req-cut"] = {"code": 200, "body": {"status": "withdrawn"}}
        result = self.live.run(answers)
        urls = [c.get("url") or "" for c in self.live.calls()]
        self.assertFalse([u for u in urls if "req-session" in u], urls)
        withdrawn = {u.split("/")[-2] for u in urls if u.endswith("/withdraw")}
        self.assertIn("req-cut", withdrawn)
        self.assertIn("req-3", withdrawn)
        self.assertLessEqual(withdrawn - {"req-cut"}, set(waiting_requests()))
        self.assertIn("[note] request req-session of PERSON_TOKEN waits", result.stdout)

    def test_a_reused_request_is_never_withdrawn(self) -> None:
        answers = good_answers()
        answers["c2-call"]["body"]["extras"] = {
            "approval_request_id": "req-old",
            "approval_request_reused": True,
        }
        answers["withdraw-req-old-status"] = {"code": 200, "body": {"status": "pending"}}
        result = self.live.run(answers)
        self.assertIn("MEHO gave back request req-old", result.stdout)
        urls = [c.get("url") or "" for c in self.live.calls()]
        self.assertFalse([u for u in urls if "req-old" in u], urls)

    def test_bash_x_stops_before_any_token_is_printed(self) -> None:
        result = self.live.run(good_answers(), bash_args=("-x",))
        self.assertEqual(result.returncode, 2)
        self.assertIn("tracing (bash -x) is on", result.stderr)
        self.assertEqual(self.live.calls(), [])
        for token in self.live.tokens.values():
            self.assertNotIn(token, result.stdout + result.stderr)


class StartTest(LiveTestCase):
    def test_stops_before_any_call_when_meho_is_signed_in_elsewhere(self) -> None:
        for stored in ("https://meho.other.example.test", None):
            with self.subTest(stored=stored):
                self.live.stored_install(stored)
                result = self.live.run(good_answers())
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("the meho command line is signed in to", result.stderr)
                self.assertEqual(self.live.calls(), [])

    def test_a_stored_install_with_a_slash_at_the_end_is_the_same(self) -> None:
        self.live.stored_install(BACKPLANE + "/")
        result = self.live.run(good_answers())
        self.assertEqual(result.returncode, NOT_COMPLETE, result.stderr)

    def test_a_ready_503_with_sandbox_mode_on_stops_the_run(self) -> None:
        answers = good_answers()
        answers["ready"] = ready(True, code=503, status="not_ready")
        result = self.live.run(answers)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.steps(self.live.calls()), ["ready"])

    def test_a_missing_setting_stops_before_any_call(self) -> None:
        result = self.live.run(good_answers(), CPC_OP_HARMLESS="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("CPC_OP_HARMLESS", result.stderr)
        self.assertEqual(self.live.calls(), [])
        result = self.live.run(good_answers(), CPC_PARAMS_DELETE="[1]")
        self.assertEqual(result.returncode, 2)
        self.assertIn("CPC_PARAMS_DELETE is not a JSON object", result.stderr)

    def test_every_operation_setting_is_named(self) -> None:
        for name in OPS:
            self.assertIn(name, self.live.run(good_answers(), "--help").stdout)


if __name__ == "__main__":
    unittest.main()
