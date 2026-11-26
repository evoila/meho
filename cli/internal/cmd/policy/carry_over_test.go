// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package policy

import (
	"net/http"
	"strings"
	"testing"
)

const carryOverAnswer = `{"scope":"tenant","sandbox_mode":true,"message":"Dry run: nothing was changed.",
"settings":[
 {"source":"override","scope":"tenant","ref":"tenant:override",
  "match":{"connector":"cowapi-1.x","op":"cowapi.vm.update"},
  "operations":[{"connector_id":"cowapi-1.x","op_id":"cowapi.vm.update","step_of":["cowapi.composite.rebuild"]}],
  "warnings":["step warning","sandbox warning","preview warning"]},
 {"source":"policy_file","scope":"tenant","ref":"tenant:classifications[2]",
  "match":{"op":"nothing.*"},"operations":[],
  "warnings":["Today this entry makes no operation wait.","preview warning"]}]}`

func TestCarryOverDryRunListsEachSetting(t *testing.T) {
	var got seen
	srv := server(t, 200, carryOverAnswer, &got)
	stdout, stderr, err := run(t, "carry-over", "--dry-run", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("carry-over: %v; stderr=%s", err, stderr)
	}
	if got.method != http.MethodGet || got.path != "/api/v1/policy/carry-over/dry-run" {
		t.Errorf("unexpected %s %s", got.method, got.path)
	}
	if got.query != "scope=tenant" {
		t.Errorf("query: %q", got.query)
	}
	want := "This install is in sandbox mode.\n" +
		"override tenant:override (tenant): connector=cowapi-1.x op=cowapi.vm.update\n" +
		"  operation cowapi-1.x cowapi.vm.update (a step of cowapi.composite.rebuild)\n" +
		"  warning: step warning\n" +
		"  warning: sandbox warning\n" +
		"  warning: preview warning\n" +
		"policy_file tenant:classifications[2] (tenant): op=nothing.*\n" +
		"  warning: Today this entry makes no operation wait.\n" +
		"  warning: preview warning\n" +
		"Dry run: nothing was changed.\n"
	if stdout != want {
		t.Errorf("stdout:\n%s\nwant:\n%s", stdout, want)
	}
}

func TestCarryOverDryRunJSONAndInstallScope(t *testing.T) {
	var got seen
	srv := server(t, 200, carryOverAnswer, &got)
	stdout, _, err := run(t, "carry-over", "--dry-run", "--scope", "install", "--json",
		"--backplane", srv.URL)
	if err != nil {
		t.Fatalf("carry-over: %v", err)
	}
	if got.query != "scope=install" {
		t.Errorf("query: %q", got.query)
	}
	if !strings.Contains(stdout, `"ref": "tenant:classifications[2]"`) {
		t.Errorf("stdout: %s", stdout)
	}
}

func TestCarryOverEmptyList(t *testing.T) {
	var got seen
	srv := server(t, 200,
		`{"scope":"tenant","sandbox_mode":false,"settings":[],"message":"Dry run: nothing was changed."}`, &got)
	stdout, _, err := run(t, "carry-over", "--dry-run", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("carry-over: %v", err)
	}
	want := "No saved \"always wait\" setting in scope tenant.\nDry run: nothing was changed.\n"
	if stdout != want {
		t.Errorf("stdout: %q", stdout)
	}
}

func TestCarryOverNeedsDryRun(t *testing.T) {
	var got seen
	srv := server(t, 200, carryOverAnswer, &got)
	_, stderr, err := run(t, "carry-over", "--backplane", srv.URL)
	if err == nil {
		t.Fatal("expected an error without --dry-run")
	}
	if got.path != "" {
		t.Errorf("no call expected; got %s", got.path)
	}
	if !strings.Contains(stderr, "add --dry-run") {
		t.Errorf("stderr: %s", stderr)
	}
}

func TestCarryOverRefusedRole(t *testing.T) {
	var got seen
	srv := server(t, 403, `{"detail":"insufficient_role"}`, &got)
	_, stderr, err := run(t, "carry-over", "--dry-run", "--backplane", srv.URL)
	if err == nil {
		t.Fatal("expected an error for a 403")
	}
	if !strings.Contains(stderr, "insufficient_role") {
		t.Errorf("stderr: %s", stderr)
	}
}
