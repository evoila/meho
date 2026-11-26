# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded answers and stand-in programs for the custom policy check tests.

``good_answers`` are the answers of an install where every check passes.
The same answer files serve the script's dry mode (``--dry-run``) and a
live run against the stand-in ``curl`` and ``meho`` programs, which read
the step name from ``CPC_STEP`` and answer from the same folder. The
stand-ins write one JSON line per start to a log: their command line, the
address, the body, and a short hash of the token they got.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import subprocess
import tempfile
import termios
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/acceptance/custom-policy-check.sh"
BACKPLANE = "https://meho.sandbox.example.test"

#: CPC_OP_* settings of the tests. Two have no target, on purpose.
OPS = {
    "CPC_OP_LOWERED": "k8s k8s.scale web-a",
    "CPC_OP_RELEVELLED": "k8s k8s.restart",
    "CPC_OP_ALWAYS_WAIT": "k8s k8s.drain web-a",
    "CPC_OP_AGENT_RULE": "k8s k8s.cordon web-a",
    "CPC_OP_APPROVER": "vmw vmw.vm.reset vc-a",
    "CPC_OP_CREDENTIAL_READ": "vault vault.kv.read vault-a",
    "CPC_OP_CREDENTIAL_WRITE": "vault vault.kv.write",
    "CPC_OP_RIGHTS_CHANGE": "kc kc.role.assign",
    "CPC_OP_POLICY_CHANGE": "policy policy.apply",
    "CPC_OP_DESTRUCTIVE": "k8s k8s.namespace.delete web-a",
    "CPC_OP_ROOT_SHELL": "linux linux.service.control host-a",
    "CPC_OP_DELETE": "k8s k8s.pod.delete web-a",
    "CPC_OP_HARMLESS": "k8s k8s.label web-a",
    "CPC_OP_CALLER_ADDRESS": "http http.get",
    "CPC_OP_WRITE": "k8s k8s.annotate web-a",
    "CPC_OP_OWN_AGENT": "k8s k8s.uncordon web-a",
}

PROBE_KEYS = [
    "1 PERSON_TOKEN LOWERED web",
    "2 PERSON_TOKEN RELEVELLED token",
    "3 PERSON_TOKEN ALWAYS_WAIT token",
    "4 PERSON_TOKEN AGENT_RULE token",
    "4 SERVICE_TOKEN AGENT_RULE token",
    "5 SERVICE_TOKEN APPROVER token",
    "6 AGENT_TOKEN CREDENTIAL_READ token",
    "6 PERSON_TOKEN CREDENTIAL_WRITE token",
    "6 PERSON_TOKEN RIGHTS_CHANGE token",
    "6 AGENT_TOKEN POLICY_CHANGE token",
    "6 PERSON_TOKEN POLICY_CHANGE token",
    "6 SERVICE_TOKEN DESTRUCTIVE token",
    "6 SERVICE_TOKEN ROOT_SHELL token",
    "6 PERSON_TOKEN DELETE token",
    "6 AGENT_TOKEN DELETE token",
    "6 SERVICE_TOKEN DELETE token",
    "6 DELETE_GRANT_TOKEN DELETE token",
    "6 PERSON_TOKEN HARMLESS token",
    "6 PERSON_TOKEN CALLER_ADDRESS token",
    "8 ADMIN_TOKEN WRITE web",
    "8 ADMIN_TOKEN WRITE token",
    "10 PERSON_TOKEN OWN_AGENT token",
]

PERSON_STEPS = [
    "p1", "p2-operator", "p2-admin", "p4", "p5-role", "p5-without",
    "p5-with", "p6-delete", "p6-lower", "p8", "p9", "c9-fresh",
    "p10-web", "p10-cli", "p10-cli-refusal", "p-takeback", "p7",
]  # fmt: skip

#: The [skip] lines of a run where every check passes: they never count as passed.
ALWAYS_SKIPPED = [
    "check 8 (an Admin's token, REST, a policy file preview): not run: waits for #6966",
    "check 11 (the event intake): not run: the script cannot send an event to the install",
    "check 11 (the findings investigator): not run: "
    + "it needs a sensor change and an AI model on the install",
    "check 11 (the runner executor): not run: it needs a satellite runner",
]

#: CPC_PARAMS_* of the tests: the credential write has params, so its check 7
#: probe also previews the call with them.
PARAMS = {"CPC_PARAMS_CREDENTIAL_WRITE": json.dumps({"path": "app/db", "value": "x"})}

#: The login of each token.
SUBS = {
    "CPC_ADMIN_TOKEN": "admin-0",
    "CPC_PERSON_TOKEN": "operator-1",
    "CPC_SERVICE_TOKEN": "service-1",
    "CPC_DELETE_GRANT_TOKEN": "service-2",
    "CPC_AGENT_TOKEN": "agent-1",
}
APPROVERS = {"groups": ["Admins"], "rule": None}

PERSON_SUB = "operator-1"
SENSOR_ID = "11111111-1111-4111-8111-111111111111"
SENSOR_DUE = "2026-01-01T00:00:05.123456Z"
FAR_FUTURE = "2999-01-01T00:00:00Z"
#: The run's work reference of a dry run. Each call sends this reference and
#: its step ("@work-ref-c6-rights"). The stand-in curl puts the run's part of
#: the reference that a live run sent in its place.
WORK_REF = "@work-ref"
#: The tenant's policy file before and after a run.
POLICY_FILE = "policy: v1\nclassifications: []\n"
WRITE_FLOOR_MISTAKE = (
    "`classifications[0]`: `k8s.annotate` changes something, so it stays at least caution."
)


def audit_id(step: str) -> str:
    """A fixed audit row id per step, so the output can be checked."""
    number = sum(ord(c) * (i + 1) for i, c in enumerate(step))
    return f"00000000-0000-4000-8000-{number:012d}"


def call(
    step: str, status: str, *, request: str = "", code: str = "", reused: bool = False
) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if request:
        extras["approval_request_id"] = request
    if reused:
        extras["approval_request_reused"] = True
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


def rows(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"code": 200, "body": {"rows": list(entries)}}


def post_row(
    step: str, path: str, sub: str = "someone", status_code: int = 200, method: str = "POST"
) -> dict[str, Any]:
    return {
        "id": audit_id(step),
        "method": method,
        "path": path,
        "principal_sub": sub,
        "status_code": status_code,
    }


def edit_row(step: str, op: str, sub: str = "admin-1", *, removed: bool = False) -> dict[str, Any]:
    """The audit row of an Admin's edit of one operation's risk level."""
    payload: dict[str, Any] = {"op_id": op}
    if removed:
        payload["override_removed"] = True
    return {
        "id": audit_id(step),
        "method": "SERVICE",
        "path": "meho_connector_edit_op",
        "principal_sub": sub,
        "status_code": 200,
        "payload": payload,
    }


def request(status: str, sub: str = PERSON_SUB, reviewer: str | None = None) -> dict[str, Any]:
    return {"code": 200, "body": {"status": status, "principal_sub": sub, "reviewed_by": reviewer}}


def explained(answer: str = "wait") -> dict[str, Any]:
    return {"code": 200, "body": {"status": "ok", "answer": answer, "approvers": APPROVERS}}


def preview(level: str, hash_: str) -> dict[str, Any]:
    return {"code": 200, "body": {"status": "ok", "preview_hash": hash_, "safety_level": level}}


def ready(switch: bool | None, *, code: int = 200, status: str = "ready") -> dict[str, Any]:
    body: dict[str, Any] = {"status": status, "checks": [], "sandbox_mode": "on"}
    if switch is not None:
        body["features"] = {"policy": {"custom_policy": switch}}
    return {"code": code, "body": body}


def _calls(a: dict[str, dict[str, Any]]) -> None:
    """The operation calls of checks 1 to 6 and 10, and their audit rows."""
    a["c2-level"] = preview("dangerous", "h-c2")
    a["c2-call"] = call("c2", "awaiting_approval", request="req-2")
    a["c2-call-row"] = row("c2", "floor:token_runs_only_safe", "personal_agent")
    a["c3-nopreview"] = call("c3-np", "error", code="preview_binding_required")
    a["c3-nopreview-row"] = row("c3-np", "", "personal_agent", "error")
    a["c3-call-preview"] = preview("destructive", "h-c3-call")
    a["c3-call"] = call("c3", "awaiting_approval", request="req-3")
    a["c3-call-row"] = row("c3", "floor:destructive_waits", "personal_agent")
    a["c4-agent"] = call("c4a", "ok")
    a["c4-agent-row"] = row("c4a", "tenant_rule:personal_agent.caution", "personal_agent", "ok")
    a["c4-service"] = call("c4s", "awaiting_approval", request="req-4")
    a["c4-service-row"] = row("c4s", "default:service.caution_write", "service_agent")
    a["c5-call"] = call("c5", "awaiting_approval", request="req-5")
    a["c5-call-row"] = row("c5", "default:service.dangerous", "service_agent")
    waiting = {
        "c6-cred-read": ("floor:credential_waits", "service_agent"),
        "c6-cred-write": ("floor:credential_waits", "personal_agent"),
        "c6-rights": ("floor:rights_change_waits", "personal_agent"),
        "c6-policy": ("floor:policy_change_waits", "personal_agent"),
        "c6-destructive": ("floor:destructive_waits", "service_agent"),
        "c6-root": ("default:service.root_shell", "service_agent"),
        "c6-delete-person": ("floor:delete_waits", "personal_agent"),
        "c6-delete-agent": ("floor:delete_waits", "service_agent"),
        "c6-delete-service": ("floor:delete_waits", "service_agent"),
        "c6-delete-lowered": ("floor:delete_waits", "personal_agent"),
        "c6-address": ("floor:caller_address_waits", "personal_agent"),
        "c10-web": ("floor:token_runs_only_safe", "personal_agent"),
        "c10-cli": ("floor:token_runs_only_safe", "personal_agent"),
    }
    for step, (rule, kind) in waiting.items():
        a[step] = call(step, "awaiting_approval", request=f"req-{step}")
        a[f"{step}-row"] = row(step, rule, kind)
    for step in ("c6-destructive", "c6-delete-person", "c6-delete-agent", "c6-delete-service"):
        a[f"{step}-preview"] = preview("destructive", f"h-{step}")
    a["c6-delete-grant-preview"] = preview("destructive", "h-c6-delete-grant")
    a["c6-delete-grant"] = call("c6-delete-grant", "ok")
    a["c6-delete-grant-row"] = row(
        "c6-delete-grant", "standing_grant:sandbox_delete_grant", "service_agent", "ok"
    )
    a["c6-delete-lowered-preview"] = preview("dangerous", "h-c6-delete-lowered")
    a["c6-policy-agent"] = {
        "code": 200,
        "body": {
            "status": "denied",
            "audit_id": audit_id("c6-policy-agent"),
            "extras": {
                "error_code": "denied",
                "reason_code": "policy_denied",
                "reason": "Only a person can ask for a policy import. Use `meho policy import` "
                "or POST /api/v1/policy/import with your own login.",
            },
        },
    }
    a["c6-policy-agent-row"] = row("c6-policy-agent", "", "service_agent", "denied")
    a["c6-harmless"] = call("c6-harmless", "awaiting_approval", request="req-harmless")


def _person_steps(a: dict[str, dict[str, Any]]) -> None:
    """The person steps, the clicks in the web console and their audit rows."""
    for step in PERSON_STEPS:
        a[step] = {"code": 0, "body": "y"}
    a["c1-edits"] = rows(edit_row("c1-edit", "k8s.scale"))
    a["c1-overrides"] = {
        "code": 200,
        "body": {
            "overrides": [
                {
                    "connector_id": "k8s",
                    "op_id": "k8s.scale",
                    "scope": "tenant",
                    "safety_level": "caution",
                    "requires_approval": None,
                    "set_at": FAR_FUTURE,
                    "set_by_sub": "admin-1",
                    "rule_ref": "",
                }
            ]
        },
    }
    a["c1-row"] = rows(row("c1", "override:ov-1", "person", "ok")["body"])
    # step: (request, its state, its sender, who decided, who clicked)
    clicks = {
        "c2-operator": ("req-2", "pending", PERSON_SUB, None, "operator-2"),
        "c2-admin": ("req-2", "approved", PERSON_SUB, "admin-1", "admin-1"),
        "c5-without": ("req-5", "pending", "service-1", None, "person-without-role"),
        "c5-with": ("req-5", "approved", "service-1", "person-with-role", "person-with-role"),
        "c10-web": ("req-c10-web", "approved", PERSON_SUB, PERSON_SUB, PERSON_SUB),
    }
    for step, (req, status, sender, reviewer, clicker) in clicks.items():
        a[f"{step}-request"] = request(status, sender, reviewer)
        a[f"{step}-rows"] = rows(post_row(step, f"/ui/approvals/{req}/approve", clicker))
    for step in ("c2-operator", "c5-without"):
        a[f"{step}-refusal"] = {"code": 0, "body": "y"}
    a["c6-agent-approve"] = {
        "code": 403,
        "body": {"detail": "human_principal_required: only a person approves."},
    }
    a["c6-agent-approve-request"] = request("pending")
    a["c6-agent-approve-rows"] = rows(
        post_row("c6-agent-approve", "/api/v1/approvals/req-harmless/approve", "agent-1", 403)
    )
    a["c8-console-rows"] = rows(
        post_row(
            "c8-console", "/ui/connectors/registry/k8s/operations/k8s.annotate", "admin-1", 400,
            "PATCH",
        )
    )  # fmt: skip
    a["c8-edits"] = rows()
    a["c8-level"] = preview("caution", "h-c8")
    a["c8-token"] = {
        "code": 200,
        "body": {"preview": True, "ok": False, "mistakes": [WRITE_FLOOR_MISTAKE], "diff": None},
    }
    a["c8-token-rows"] = rows(post_row("c8-token", "/api/v1/policy/import"))
    a["c9-rows"] = rows(
        edit_row("c9-raise", "k8s.annotate"), edit_row("c9-remove", "k8s.annotate", removed=True)
    )
    a["c10-cli-request"] = request("pending")
    a["c10-cli-rows"] = rows(
        post_row("c10-cli-approve", "/api/v1/approvals/req-c10-cli/approve", PERSON_SUB, 403)
    )


def _end(a: dict[str, dict[str, Any]]) -> None:
    """Check 11, the take-back, the probes of check 7 and the baseline."""
    a["c11-sensors"] = {
        "code": 200,
        "body": {
            "items": [
                {"id": "00000000-0000-4000-8000-0000000000aa", "op_id": "k8s.list"},
                {
                    "id": SENSOR_ID,
                    "op_id": "k8s.get",
                    "target": {"name": "web-a"},
                    "identity_sub": "__sensor__",
                },
            ],
            "next_cursor": None,
        },
    }
    a["c11-run"] = {"code": 202, "body": {"sensor_id": SENSOR_ID, "next_fire_at": SENSOR_DUE}}
    sensor_row = row("c11", "default:system", "system", "ok")["body"]
    sensor_row.update(target_name="web-a", principal_sub="__sensor__", ts="2026-01-01T00:00:06Z")
    a["c11-row"] = rows(sensor_row)
    approved = {"req-2", "req-5", "req-c10-web"}
    for req in waiting_requests() + sorted(approved):
        a[f"withdraw-{req}-status"] = request("approved" if req in approved else "pending")
        a[f"withdraw-{req}"] = {"code": 200, "body": {"status": "withdrawn"}}
    a["withdraw-queue"] = {"code": 200, "body": []}
    for name in SUBS:
        a[f"login-{name}"] = {"code": 200, "body": []}
    a["login-rows"] = rows(
        *({"id": audit_id(f"login-{n}"), "request_id": f"@request-id:login-{n}", "principal_sub": s}
          for n, s in SUBS.items())
    )  # fmt: skip
    a["before-overrides"] = {"code": 200, "body": {"overrides": []}}
    a["before-policy-file"] = {"code": 200, "body": POLICY_FILE}
    a["takeback-overrides"] = {"code": 200, "body": {"overrides": []}}
    a["takeback-policy-file"] = {"code": 200, "body": POLICY_FILE}
    a["c7-ready"] = ready(False)
    with_params = PROBE_KEYS.index("6 PERSON_TOKEN CREDENTIAL_WRITE token") + 1
    for i in range(1, len(PROBE_KEYS) + 1):
        for prefix in ("c7", "baseline"):
            step = f"{prefix}-{i}"
            a[step] = explained()
            probe_rows = [{"id": audit_id(step), "request_id": f"@request-id:{step}"}]
            if i == with_params:
                a[f"{step}-preview"] = {
                    "code": 200,
                    "body": {"status": "ok", "policy_verdict": "needs-approval"},
                }
                probe_rows.append(
                    {"id": audit_id(f"{step}-p"), "request_id": f"@request-id:{step}-preview"}
                )
            a[f"{step}-row"] = rows(*probe_rows)


def waiting_requests() -> list[str]:
    """The requests of a full run that still wait at its end."""
    steps = [
        "c6-cred-read", "c6-cred-write", "c6-rights", "c6-policy", "c6-destructive", "c6-root",
        "c6-delete-person", "c6-delete-agent", "c6-delete-service", "c6-delete-lowered",
        "c6-address", "c10-cli",
    ]  # fmt: skip
    return ["req-3", "req-4", "req-harmless"] + [f"req-{s}" for s in steps]


def good_answers() -> dict[str, dict[str, Any]]:
    """The answers of an install where every check passes."""
    a: dict[str, dict[str, Any]] = {
        "ready": ready(True),
        "version": {"code": 200, "body": {"git_sha": "abc1234", "chart_version": "1.2.3"}},
    }
    _calls(a)
    _person_steps(a)
    _end(a)
    return a


def baseline_text(answer: str = "wait") -> str:
    approvers = json.dumps(APPROVERS, sort_keys=True, separators=(",", ":"))
    lines = []
    for key in PROBE_KEYS:
        verdict = "needs-approval" if key == "6 PERSON_TOKEN CREDENTIAL_WRITE token" else "-"
        lines.append(f"{key}\t{answer}\t{approvers}\t{verdict}\n")
    return "".join(lines)


def write_answers(folder: Path, answers: dict[str, dict[str, Any]]) -> None:
    for old in folder.glob("*.json"):
        old.unlink()
    for step, answer in answers.items():
        (folder / f"{step}.json").write_text(json.dumps(answer))


def result_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith(("[ok]", "[FAIL]", "[skip]"))]


def base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("CPC_", "MEHO_", "XDG_"))}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


STUB_CURL = r"""#!/usr/bin/env python3
import hashlib, json, os, re, signal, sys

args = sys.argv[1:]
values = {"-o": None, "-D": None, "-w": None, "-X": "GET"}
headers, data, url = [], None, None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-D", "-w", "-X", "-H", "--data-binary"):
        v = args[i + 1]
        i += 2
        if a == "-H":
            if v.startswith("@"):
                with open(v[1:]) as f:
                    headers += [line.strip() for line in f if line.strip()]
            else:
                headers.append(v)
        elif a == "--data-binary":
            data = open(v[1:]).read() if v.startswith("@") else v
        else:
            values[a] = v
        continue
    if not a.startswith("-"):
        url = a
    i += 1
auth = ""
folder = os.environ["CPC_STUB_DIR"]
for h in headers:
    if h.lower().startswith("authorization: bearer "):
        auth = hashlib.sha256(h.split(" ", 2)[2].encode()).hexdigest()[:12]
step = os.environ.get("CPC_STEP", "")
for h in headers:
    if h.lower().startswith("x-request-id: "):
        rid = h.split(" ", 1)[1]
        rid = "-".join([rid[:8], rid[8:12], rid[12:16], rid[16:20], rid[20:]])
        open(os.path.join(folder, ".request-id-" + step), "w").write(rid)
entry = {"prog": "curl", "argv": sys.argv[1:], "step": step, "method": values["-X"],
         "url": url, "auth": auth, "body": data}
with open(os.environ["CPC_STUB_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
if os.environ.get("CPC_STUB_STOP_AT") == step:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgid(0), signal.SIGTERM)
if values["-D"] and not values["-w"]:
    open(values["-D"], "w").write("HTTP/1.1 200 OK\r\nMcp-Session-Id: s-1\r\n\r\n")
    sys.exit(0)
path = os.path.join(folder, step + ".json")
if not os.path.exists(path):
    if values["-w"]:
        sys.stdout.write("000")
    sys.exit(7)
answer = json.load(open(path))
body = json.dumps(answer.get("body"))
def request_id(match):
    path = os.path.join(folder, ".request-id-" + match.group(1))
    return open(path).read() if os.path.exists(path) else match.group(0)
body = re.sub(r"@request-id:([A-Za-z0-9_.-]+)", request_id, body)
for h in headers:
    if h.lower().startswith("meho-work-ref: "):
        ref = h.split(" ", 1)[1]
        if step and ref.endswith("-" + step):
            ref = ref[: -len(step) - 1]
        body = body.replace("@work-ref", ref)
if values["-o"]:
    open(values["-o"], "w").write(body)
else:
    sys.stdout.write(body)
if values["-w"]:
    sys.stdout.write(str(answer.get("code", 0)))
"""

STUB_MEHO = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
params = None
if "--params" in args:
    value = args[args.index("--params") + 1]
    params = open(value[1:]).read() if value.startswith("@") else value
step = os.environ.get("CPC_STEP", "")
with open(os.environ["CPC_STUB_LOG"], "a") as log:
    log.write(json.dumps({"prog": "meho", "argv": args, "step": step, "params": params}) + "\n")
path = os.path.join(os.environ["CPC_STUB_DIR"], step + ".json")
if not os.path.exists(path):
    sys.exit(3)
answer = json.load(open(path))
sys.stdout.write(json.dumps(answer.get("body")))
sys.exit(answer.get("exit", 0))
"""


class LiveRun:
    """A live run of the script against the stand-in ``curl`` and ``meho``."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.answers_dir = folder / "answers"
        self.answers_dir.mkdir()
        self.bin = folder / "bin"
        self.bin.mkdir()
        for name, text in (("curl", STUB_CURL), ("meho", STUB_MEHO)):
            stub = self.bin / name
            stub.write_text(text)
            stub.chmod(0o755)
        self.log = folder / "calls.jsonl"
        self.config = folder / "config"
        self.baseline = folder / "baseline.tsv"
        self.baseline.write_text(baseline_text())
        self.tokens = {
            name: "tok-" + os.urandom(16).hex()
            for name in (
                "CPC_ADMIN_TOKEN", "CPC_PERSON_TOKEN", "CPC_SERVICE_TOKEN",
                "CPC_DELETE_GRANT_TOKEN", "CPC_AGENT_TOKEN",
            )
        }  # fmt: skip
        self.secret = "pw-" + os.urandom(16).hex()
        self.stored_install(BACKPLANE)

    def stored_install(self, url: str | None) -> None:
        folder = self.config / "meho"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "config.json"
        if url is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"backplane_url": url}))

    def env(self, **extra: str) -> dict[str, str]:
        env = base_env()
        env.update(self.tokens)
        env.update(OPS)
        env.update(
            PATH=f"{self.bin}:{env.get('PATH', '')}",
            MEHO_BACKPLANE=BACKPLANE,
            XDG_CONFIG_HOME=str(self.config),
            CPC_BASELINE=str(self.baseline),
            CPC_CUSTOM_ROLE="Approvers-VMW",
            CPC_SENSOR_ID=SENSOR_ID,
            CPC_PARAMS_CREDENTIAL_WRITE=json.dumps({"path": "app/db", "password": self.secret}),
            CPC_STUB_DIR=str(self.answers_dir),
            CPC_STUB_LOG=str(self.log),
        )
        env.update(extra)
        return env

    def run(
        self,
        answers: dict[str, dict[str, Any]],
        *args: str,
        terminal: bool = False,
        bash_args: tuple[str, ...] = (),
        **extra: str,
    ) -> subprocess.CompletedProcess[str]:
        write_answers(self.answers_dir, answers)
        self.log.write_text("")
        cmd = ["bash", *bash_args, str(SCRIPT), *args]
        if not terminal:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self.env(**extra),
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                timeout=300,
                check=False,
            )
        return _run_on_a_terminal(cmd, self.env(**extra))

    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]


def _run_on_a_terminal(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with a terminal of its own, where a person answers "y" to each question."""
    import fcntl

    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)

    def take_terminal() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=env,
            text=True,
            preexec_fn=take_terminal,
            pass_fds=(slave,),
        )
        os.write(master, b"y\n" * 64)
        try:
            code = proc.wait(timeout=300)
        finally:
            os.close(slave)
            os.close(master)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(cmd, code, out.read(), err.read())
