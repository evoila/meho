// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package policy

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

const explainWait = `{
  "status": "ok", "engine": "shadow", "connector_id": "<connector-id>", "op_id": "<op-id>",
  "answer": "wait", "decided_by": "today",
  "approvers": {"groups": ["MEHO-SA-Operators", "MEHO-Admins"], "rule": "install:routing[0], tenant:routing[0]",
    "floor": "install:routing[0]"},
  "today": {"verdict": "wait", "policy_verdict": "needs-approval", "reason": "a token call runs only safe operations"},
  "evaluator": {
    "verdict": "wait",
    "rule": {"source": "floor", "rule_id": "token_runs_only_safe", "text": "a token call runs only safe operations", "verdict": "wait"},
    "floor": {"source": "floor", "rule_id": "token_runs_only_safe", "text": "a token call runs only safe operations", "verdict": "wait"},
    "rules": [
      {"source": "default", "rule_id": "person.token.caution", "text": "risk level caution waits", "verdict": "wait"},
      {"source": "default", "rule_id": "always_wait", "text": "the operation always waits", "verdict": "wait", "path": "file:classifications[1]"}
    ],
    "same_as_today": true
  },
  "classification": {"risk_level": "caution", "requires_approval": true, "category": "network",
    "labels": {}, "entries": [{"path": "file:classifications[1]", "labels": {}}]},
  "not_checked": ["idempotency_key", "connector_resolution"]
}`

func decodeSent(t *testing.T, got seen) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal([]byte(got.body), &body); err != nil {
		t.Fatalf("request body is not JSON: %v; %q", err, got.body)
	}
	return body
}

func TestExplainSendsTheQuestionAndPrintsTheRules(t *testing.T) {
	var got seen
	srv := server(t, 200, explainWait, &got)
	stdout, stderr, err := run(t, "explain", "<connector-id>", "<op-id>",
		"--groups", "Operators,SA-Operators", "--principal", "agent", "--target", "<target-name>",
		"--file", writeFile(t), "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("explain: %v; stderr=%s", err, stderr)
	}
	if got.method != http.MethodPost || got.path != "/api/v1/policy/explain" {
		t.Errorf("unexpected %s %s", got.method, got.path)
	}
	body := decodeSent(t, got)
	if body["connector_id"] != "<connector-id>" || body["op_id"] != "<op-id>" {
		t.Errorf("operation not sent: %v", body)
	}
	if body["principal"] != "agent" || body["target"] != "<target-name>" || body["file"] != testFile {
		t.Errorf("question not sent: %v", body)
	}
	if groups, _ := body["groups"].([]any); len(groups) != 2 || groups[1] != "SA-Operators" {
		t.Errorf("groups not sent: %v", body["groups"])
	}
	for _, want := range []string{
		"Answer: wait",
		"Who may approve: MEHO-SA-Operators, MEHO-Admins (rule: install:routing[0], tenant:routing[0])",
		"Install's floor: install:routing[0] (a tenant's routing can only narrow it)",
		"Today: needs-approval: a token call runs only safe operations",
		"Floor: floor token_runs_only_safe: wait",
		"  2. default always_wait: wait (the operation always waits) at file:classifications[1]",
		"category: network",
		"matched file:classifications[1]",
		"Not checked: idempotency_key, connector_resolution",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout lacks %q:\n%s", want, stdout)
		}
	}
}

func TestExplainWithTheEvaluatorOffSaysSo(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"status":"ok","engine":"off","answer":"run","decided_by":"today",
		"today":{"verdict":"run","policy_verdict":"auto-execute","reason":null},"evaluator":null}`, &got)
	stdout, stderr, err := run(t, "explain", "<connector-id>", "<op-id>", "--me", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("explain: %v; stderr=%s", err, stderr)
	}
	if body := decodeSent(t, got); body["me"] != true {
		t.Errorf("--me not sent: %v", body)
	}
	if !strings.Contains(stdout, "Answer: run") || !strings.Contains(stdout, "Policy evaluator: off") {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestExplainWithoutTheApproverRulePrintsOnlyTheGroups(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"status":"ok","engine":"off","answer":"wait","decided_by":"today",
		"approvers":{"groups":["MEHO-Admins"],"rule":null},
		"today":{"verdict":"wait","policy_verdict":"needs-approval","reason":null},"evaluator":null}`, &got)
	stdout, stderr, err := run(t, "explain", "<connector-id>", "<op-id>", "--me", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("explain: %v; stderr=%s", err, stderr)
	}
	if !strings.Contains(stdout, "Who may approve: MEHO-Admins\n") || strings.Contains(stdout, "rule:") ||
		strings.Contains(stdout, "floor") {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestExplainPrintsWhetherYouMayApproveYourOwnRequest(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"status":"ok","engine":"shadow","answer":"wait","decided_by":"today",
		"approvers":{"groups":["MEHO-Operators","MEHO-Admins"],"rule":null},
		"own_approval":{"web":true,"web_reason":"You may approve it here.",
			"token":false,"token_reason":"You cannot approve your own change."},
		"today":{"verdict":"wait","policy_verdict":"needs-approval","reason":null},"evaluator":null}`, &got)
	stdout, stderr, err := run(t, "explain", "<connector-id>", "<op-id>", "--me", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("explain: %v; stderr=%s", err, stderr)
	}
	for _, want := range []string{
		"May you approve your own request:\n",
		"  in the web console: yes (You may approve it here.)\n",
		"  with a token: no (You cannot approve your own change.)\n",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout lacks %q:\n%s", want, stdout)
		}
	}
}

func TestExplainSaysWhenYourOwnApprovalDependsOnTheValues(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"status":"ok","engine":"shadow","answer":"wait","decided_by":"today",
		"approvers":{"groups":["MEHO-Admins"],"rule":null},
		"own_approval":{"web":null,"web_reason":"This depends on the values of the call. Explain asks without them.",
			"token":null,"token_reason":"This depends on the values of the call. Explain asks without them."},
		"today":{"verdict":"wait","policy_verdict":"needs-approval","reason":null},"evaluator":null}`, &got)
	stdout, stderr, err := run(t, "explain", "<connector-id>", "<op-id>", "--me", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("explain: %v; stderr=%s", err, stderr)
	}
	for _, want := range []string{
		"  in the web console: depends on the values of the call (This depends on the values of the call. Explain asks without them.)\n",
		"  with a token: depends on the values of the call (This depends on the values of the call. Explain asks without them.)\n",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout lacks %q:\n%s", want, stdout)
		}
	}
}

func TestExplainWithoutOwnApprovalPrintsNoLine(t *testing.T) {
	var got seen
	srv := server(t, 200, explainWait, &got)
	stdout, stderr, err := run(t, "explain", "<connector-id>", "<op-id>", "--me", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("explain: %v; stderr=%s", err, stderr)
	}
	if strings.Contains(stdout, "approve your own request") {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestExplainRequestShowsTheStoredRule(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"status":"ok","engine":"shadow","request":{
		"request_id":"5b0e8e8e-1111-4c1c-9e9e-000000000001","op_id":"<op-id>","connector_id":"<connector-id>",
		"status":"pending","risk_level":"caution",
		"policy_rule":{"source":"floor","rule_id":"token_runs_only_safe","text":"t","decided":false},
		"policy_version":null,"approvers":{"groups":["MEHO-Admins"],"rule":"default"}}}`, &got)
	stdout, stderr, err := run(t, "explain", "--request", "5b0e8e8e-1111-4c1c-9e9e-000000000001",
		"--backplane", srv.URL)
	if err != nil {
		t.Fatalf("explain: %v; stderr=%s", err, stderr)
	}
	if body := decodeSent(t, got); body["request_id"] != "5b0e8e8e-1111-4c1c-9e9e-000000000001" {
		t.Errorf("request id not sent: %v", body)
	}
	for _, want := range []string{"status pending", `"rule_id":"token_runs_only_safe"`, "MEHO-Admins"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout lacks %q:\n%s", want, stdout)
		}
	}
}

func TestExplainErrorAnswerFails(t *testing.T) {
	var got seen
	srv := server(t, 200, `{"status":"error","engine":"off","error":"no_target: <target-name> not found",
		"extras":{"error_code":"no_target"}}`, &got)
	_, stderr, err := run(t, "explain", "<connector-id>", "<op-id>", "--me", "--target", "<target-name>",
		"--backplane", srv.URL)
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(stderr, "no_target") {
		t.Errorf("stderr: %s", stderr)
	}
}

func TestExplainHTTPErrorsAreShown(t *testing.T) {
	var got seen
	srv := server(t, 403, `{"detail":"insufficient_role"}`, &got)
	_, stderr, err := run(t, "explain", "<connector-id>", "<op-id>", "--groups", "Operators",
		"--backplane", srv.URL)
	if err == nil || !strings.Contains(stderr, "insufficient_role") {
		t.Errorf("expected insufficient_role, got err=%v stderr=%s", err, stderr)
	}
}

func TestExplainRefusesBadFlagsBeforeCalling(t *testing.T) {
	cases := [][]string{
		{"explain"},
		{"explain", "<connector-id>", "<op-id>"},
		{"explain", "<connector-id>", "<op-id>", "--me", "--groups", "Operators"},
		{"explain", "<connector-id>", "<op-id>", "--groups", "Operators", "--principal", "robot"},
		{"explain", "<connector-id>", "<op-id>", "--me", "--via", "phone"},
		{"explain", "<connector-id>", "<op-id>", "--me", "--scope", "world"},
		{"explain", "<connector-id>", "<op-id>", "--me", "--file", "/no/such/file.yaml"},
		{"explain", "--request", "not-a-uuid"},
		{"explain", "<connector-id>", "<op-id>", "--request", "5b0e8e8e-1111-4c1c-9e9e-000000000001"},
	}
	for _, args := range cases {
		var got seen
		srv := server(t, 200, explainWait, &got)
		_, _, err := run(t, append(args, "--backplane", srv.URL)...)
		if err == nil {
			t.Errorf("%v: expected an error", args)
		}
		if got.method != "" {
			t.Errorf("%v: the server was called", args)
		}
	}
}
