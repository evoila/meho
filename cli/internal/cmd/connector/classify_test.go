// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func runCmd(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	root := NewRootCmd()
	var stdout, stderr bytes.Buffer
	root.SetOut(&stdout)
	root.SetErr(&stderr)
	root.SetArgs(args)
	err := root.ExecuteContext(context.Background())
	return stdout.String(), stderr.String(), err
}

func decodeBody(t *testing.T, r *http.Request) map[string]any {
	t.Helper()
	raw, _ := io.ReadAll(r.Body)
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Errorf("decode body: %v", err)
	}
	return got
}

var waitingAnswer = map[string]any{
	"status":              "awaiting_approval",
	"approval_request_id": "<approval-id>",
	"message":             "The change waits until another tenant admin approves it.",
	"matched_operations":  2,
	"changed_operations":  1,
}

// previewAnswer is a dry run with one operation in force and one whose row is gone.
var previewAnswer = map[string]any{
	"match": map[string]any{"op": "k8s.deployment.*"}, "rule_ref": "tenant:classifications[0]",
	"entry": map[string]any{}, "matched_operations": 1, "changed_operations": 1,
	"conflict": nil,
	"operations": []any{map[string]any{
		"connector_id": "k8s-1.x", "group_key": "workload", "op_id": "k8s.deployment.scale",
		"changes": true,
		"before": map[string]any{"category": "other", "labels": map[string]any{}, "risk": nil,
			"always_wait": true, "rule_ref": "taxonomy.default",
			"in_force": map[string]any{"risk": "caution", "waits": true}},
		"after": map[string]any{"category": "compute", "labels": map[string]any{"team": "ops"},
			"risk": nil, "always_wait": false, "rule_ref": "tenant:classifications[0]",
			"in_force": map[string]any{"risk": "caution", "waits": true}},
	}, map[string]any{
		"connector_id": "k8s-1.x", "group_key": "workload", "op_id": "k8s.deployment.gone",
		"changes": true,
		"before": map[string]any{"category": "other", "labels": map[string]any{}, "risk": nil,
			"always_wait": false, "rule_ref": "taxonomy.default", "in_force": nil},
		"after": map[string]any{"category": "compute", "labels": map[string]any{},
			"risk": nil, "always_wait": false, "rule_ref": "tenant:classifications[0]",
			"in_force": nil},
	}},
}

// TestClassifyDryRunIsTheDefault: without --apply the command only asks
// for the dry run and prints every matched operation.
func TestClassifyDryRunIsTheDefault(t *testing.T) {
	var got map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/policy/classification/preview": func(w http.ResponseWriter, r *http.Request) {
			got = decodeBody(t, r)
			writeJSON(t, w, http.StatusOK, previewAnswer)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	stdout, stderr, err := runCmd(t, "classify", "--op", "k8s.deployment.*", "--category", "compute",
		"--label", "team=ops", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("classify: %v\n%s", err, stderr)
	}
	match, _ := got["match"].(map[string]any)
	if match["op"] != "k8s.deployment.*" || got["category"] != "compute" {
		t.Errorf("body = %v", got)
	}
	labels, _ := got["labels"].(map[string]any)
	if labels["team"] != "ops" {
		t.Errorf("labels = %v", got["labels"])
	}
	for _, want := range []string{
		"Dry run: 1 operation(s) matched", "k8s.deployment.scale", "other -> compute", "--apply",
		"IN FORCE", "true -> false", "caution, waits -> caution, waits", "- -> -",
	} {
		if !strings.Contains(stdout, want) {
			t.Errorf("output misses %q:\n%s", want, stdout)
		}
	}
}

// TestClassifyDryRunJSONKeepsNull: with --json, an operation whose row is
// gone prints "in_force": null, never an empty value that reads as "does not wait".
func TestClassifyDryRunJSONKeepsNull(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/policy/classification/preview": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, http.StatusOK, previewAnswer)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	stdout, stderr, err := runCmd(t, "classify", "--op", "k8s.deployment.*", "--category", "compute",
		"--json", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("classify --json: %v\n%s", err, stderr)
	}
	var got struct {
		Operations []struct {
			OpID   string                     `json:"op_id"`
			Before map[string]json.RawMessage `json:"before"`
			After  map[string]json.RawMessage `json:"after"`
		} `json:"operations"`
	}
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatalf("decode stdout: %v\n%s", err, stdout)
	}
	if len(got.Operations) != 2 {
		t.Fatalf("operations = %d, want 2:\n%s", len(got.Operations), stdout)
	}
	inForce := map[string]string{
		"k8s.deployment.scale": `{"risk":"caution","waits":true}`,
		"k8s.deployment.gone":  "null",
	}
	for _, op := range got.Operations {
		want := inForce[op.OpID]
		for side, values := range map[string]map[string]json.RawMessage{"before": op.Before, "after": op.After} {
			raw, ok := values["in_force"]
			if !ok {
				t.Errorf("%s %s: in_force is missing", op.OpID, side)
				continue
			}
			var compact bytes.Buffer
			if err := json.Compact(&compact, raw); err != nil {
				t.Fatalf("compact: %v", err)
			}
			if compact.String() != want {
				t.Errorf("%s %s: in_force = %s, want %s", op.OpID, side, compact.String(), want)
			}
		}
	}
}

// TestClassifyApplyAsksForTheChange: --apply sends the request and prints
// the id of the approval request it waits for.
func TestClassifyApplyAsksForTheChange(t *testing.T) {
	var got map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/policy/classification": func(w http.ResponseWriter, r *http.Request) {
			got = decodeBody(t, r)
			writeJSON(t, w, http.StatusAccepted, waitingAnswer)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	stdout, stderr, err := runCmd(t, "classify", "--connector", "vault-1.x", "--always-wait", "--apply",
		"--backplane", srv.URL)
	if err != nil {
		t.Fatalf("classify --apply: %v\n%s", err, stderr)
	}
	if got["always_wait"] != true {
		t.Errorf("always_wait = %v", got["always_wait"])
	}
	if !strings.Contains(stdout, "approval request: <approval-id>") {
		t.Errorf("output misses the approval id:\n%s", stdout)
	}
}

// TestClassifyApplyShowsARefusal: a 400 from the backplane is an error,
// never a success line.
func TestClassifyApplyShowsARefusal(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/policy/classification": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, http.StatusBadRequest, map[string]any{
				"detail": "classification_refused: The match finds no operation your tenant can see.",
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	stdout, _, err := runCmd(t, "classify", "--op", "nothing.*", "--category", "compute", "--apply",
		"--backplane", srv.URL)
	if err == nil {
		t.Fatal("a refused request must fail")
	}
	if strings.Contains(stdout, "waits") {
		t.Errorf("a refusal must not look like a request:\n%s", stdout)
	}
}

// TestClassifyRefusesBadFlags checks the input before any request.
func TestClassifyRefusesBadFlags(t *testing.T) {
	cases := map[string][]string{
		"no match":   {"classify", "--category", "compute"},
		"no value":   {"classify", "--op", "x.*"},
		"bad label":  {"classify", "--op", "x.*", "--label", "no-equals"},
		"both waits": {"classify", "--op", "x.*", "--always-wait", "--no-always-wait"},
	}
	for name, args := range cases {
		t.Run(name, func(t *testing.T) {
			if _, _, err := runCmd(t, append(args, "--backplane", "http://127.0.0.1:1")...); err == nil {
				t.Fatal("expected an error")
			}
		})
	}
}

// TestEditOpCategoryAsksForAWaitingChange: --category on edit-op sends
// only the classification fields and prints the approval id.
func TestEditOpCategoryAsksForAWaitingChange(t *testing.T) {
	var got map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"PATCH /api/v1/connectors/vault-1.x/operations/vault.kv.read": func(w http.ResponseWriter, r *http.Request) {
			got = decodeBody(t, r)
			writeJSON(t, w, http.StatusAccepted, waitingAnswer)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	stdout, stderr, err := runCmd(t, "edit-op", "vault-1.x", "vault.kv.read", "--category", "secrets",
		"--backplane", srv.URL)
	if err != nil {
		t.Fatalf("edit-op --category: %v\n%s", err, stderr)
	}
	if got["category"] != "secrets" || got["safety_level"] != nil || got["requires_approval"] != nil {
		t.Errorf("body = %v", got)
	}
	if !strings.Contains(stdout, "<approval-id>") {
		t.Errorf("output misses the approval id:\n%s", stdout)
	}
}

// TestEditGroupLabelAsksForAWaitingChange: --label on edit-group.
func TestEditGroupLabelAsksForAWaitingChange(t *testing.T) {
	var got map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"PATCH /api/v1/connectors/vault-1.x/groups/kv": func(w http.ResponseWriter, r *http.Request) {
			got = decodeBody(t, r)
			writeJSON(t, w, http.StatusAccepted, waitingAnswer)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, stderr, err := runCmd(t, "edit-group", "vault-1.x", "kv", "--label", "team=ops",
		"--backplane", srv.URL); err != nil {
		t.Fatalf("edit-group --label: %v\n%s", err, stderr)
	}
	labels, _ := got["labels"].(map[string]any)
	if labels["team"] != "ops" || got["name"] != nil {
		t.Errorf("body = %v", got)
	}
}

// TestEditOpRefusesClassificationWithOtherFlags: a classification change
// waits for another admin, so it cannot ride along with a direct edit.
func TestEditOpRefusesClassificationWithOtherFlags(t *testing.T) {
	_, stderr, err := runCmd(t, "edit-op", "vault-1.x", "vault.kv.read", "--category", "secrets",
		"--safety", "dangerous", "--backplane", "http://127.0.0.1:1")
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(stderr, "--safety") {
		t.Errorf("the message must name the other flag:\n%s", stderr)
	}
	if _, _, err := runCmd(t, "edit-group", "vault-1.x", "kv", "--category", "secrets",
		"--name", "KV", "--backplane", "http://127.0.0.1:1"); err == nil {
		t.Fatal("edit-group: expected an error")
	}
}
