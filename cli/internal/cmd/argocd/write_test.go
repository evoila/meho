// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package argocd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// TestAppHasWriteVerbs — the five approval-gated app write verbs attach to
// the `app` parent alongside the read verbs.
func TestAppHasWriteVerbs(t *testing.T) {
	c := newAppCmd()
	subs := make(map[string]bool)
	for _, s := range c.Commands() {
		subs[s.Name()] = true
	}
	for _, name := range []string{"sync", "rollback", "set", "refresh", "delete"} {
		if !subs[name] {
			t.Errorf("app is missing write sub-verb %q", name)
		}
	}
}

// TestAppProjectHasWriteVerbs — create + update attach to the appproject parent.
func TestAppProjectHasWriteVerbs(t *testing.T) {
	c := newAppProjectCmd()
	subs := make(map[string]bool)
	for _, s := range c.Commands() {
		subs[s.Name()] = true
	}
	for _, name := range []string{"create", "update"} {
		if !subs[name] {
			t.Errorf("appproject is missing write sub-verb %q", name)
		}
	}
}

// TestWriteVerbsDispatchCanonicalOpIDs — every write verb dispatches its
// canonical op_id with the connector_id pre-baked.
func TestWriteVerbsDispatchCanonicalOpIDs(t *testing.T) {
	expected := []string{
		"argocd.app.sync",
		"argocd.app.rollback",
		"argocd.app.set",
		"argocd.app.refresh",
		"argocd.app.delete",
		"argocd.appproject.create",
		"argocd.appproject.update",
	}
	dispatched := make(map[string]bool)
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "argocd-api-3.x" {
				t.Errorf("connector_id: got %q", body.ConnectorID)
			}
			dispatched[body.OpID] = true
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID, Result: json.RawMessage(`{}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	for _, opID := range expected {
		if _, err := dispatchOp(context.Background(), srv.URL, opID, "rdc-argocd", map[string]any{"name": "x"}); err != nil {
			t.Fatalf("dispatchOp %s: %v", opID, err)
		}
	}
	for _, opID := range expected {
		if !dispatched[opID] {
			t.Errorf("op_id %q was not dispatched", opID)
		}
	}
}

// TestSyncForwardsSyncParams — `app sync --prune` forwards prune + name.
func TestSyncForwardsSyncParams(t *testing.T) {
	var got callRequestBody
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewDecoder(r.Body).Decode(&got)
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "argocd.app.sync",
				Result: json.RawMessage(`{"name":"guestbook","phase":"Succeeded","timed_out":false}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newAppSyncCmd()
	cmd.SetArgs([]string{"--target", "rdc-argocd", "--name", "guestbook", "--prune", "--backplane", srv.URL})
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("sync execute: %v", err)
	}
	if got.OpID != "argocd.app.sync" {
		t.Errorf("op_id: got %q", got.OpID)
	}
	if got.Params["name"] != "guestbook" {
		t.Errorf("name param: got %v", got.Params["name"])
	}
	if got.Params["prune"] != true {
		t.Errorf("prune param: got %v", got.Params["prune"])
	}
}

// TestAppSyncAwaitingApprovalRealPath — drives the REAL end-to-end path
// (fake backplane returns status=awaiting_approval → dispatchWrite →
// renderCallResult → conn.Render), not printWriteResult directly. The
// dispatch must NOT classify the park as an exit-4 invalid status: the
// command exits 0 (parked, not failed), stdout carries the parked hint,
// and stderr never carries the invalid-status diagnostic. Replaces the
// pre-#1740 test that called printWriteResult directly and so never
// exercised the conn.Render allowlist that actually rejected the park.
func TestAppSyncAwaitingApprovalRealPath(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "awaiting_approval", OpID: "argocd.app.sync", DurationMs: 5,
				Extras: json.RawMessage(`{"approval_request_id":"ar-123"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newAppSyncCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{"--target", "rdc-argocd", "--name", "guestbook", "--backplane", srv.URL})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("awaiting_approval must not be an error (parked, exit 0); got %v", err)
	}
	if !strings.Contains(out.String(), "parked for human approval") {
		t.Errorf("expected parked hint on stdout; got %q", out.String())
	}
	if strings.Contains(errBuf.String(), "invalid OperationResult") {
		t.Errorf("awaiting_approval was wrongly rejected as invalid status: %s", errBuf.String())
	}
}

// TestAppSyncAwaitingApprovalJSON — with --json the parked envelope
// round-trips as the full OperationResult JSON (incl.
// extras.approval_request_id) and the command exits 0.
func TestAppSyncAwaitingApprovalJSON(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "awaiting_approval", OpID: "argocd.app.sync",
				Extras: json.RawMessage(`{"approval_request_id":"ar-123"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newAppSyncCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"--target", "rdc-argocd", "--name", "guestbook", "--json", "--backplane", srv.URL})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatalf("--json output is not valid JSON: %v\n%s", err, out.String())
	}
	if decoded["status"] != "awaiting_approval" {
		t.Errorf("json status: got %v want awaiting_approval", decoded["status"])
	}
	extras, ok := decoded["extras"].(map[string]any)
	if !ok || extras["approval_request_id"] != "ar-123" {
		t.Errorf("json envelope must carry extras.approval_request_id; got %v", decoded["extras"])
	}
}

// TestPrintWriteResultCascade — the delete result's proposed_effect cascade
// count is surfaced in the text render.
func TestPrintWriteResultCascade(t *testing.T) {
	var buf bytes.Buffer
	r := &CallResult{
		Status: "ok", OpID: "argocd.app.delete", DurationMs: 12,
		Result: json.RawMessage(`{"name":"guestbook","deleted":true,"cascade":true,` +
			`"proposed_effect":{"cascade_resources":[{"kind":"Deployment","name":"a"},{"kind":"Service","name":"b"}]}}`),
	}
	printWriteResult("argocd.app.delete")(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "2 resource(s)") {
		t.Errorf("expected cascade count; got %q", out)
	}
	if !strings.Contains(out, "deleted:") {
		t.Errorf("expected deleted field; got %q", out)
	}
}

// TestSetRequiresSpecFile — `app set` marks --name and --spec-file required.
func TestSetRequiresSpecFile(t *testing.T) {
	cmd := newAppSetCmd()
	for _, flag := range []string{"name", "spec-file"} {
		f := cmd.Flags().Lookup(flag)
		if f == nil {
			t.Fatalf("set missing --%s", flag)
		}
		if ann := f.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
			t.Errorf("set --%s should be required; annotations=%v", flag, f.Annotations)
		}
	}
}

// TestRollbackRequiresID — `app rollback` marks --name and --id required.
func TestRollbackRequiresID(t *testing.T) {
	cmd := newAppRollbackCmd()
	for _, flag := range []string{"name", "id"} {
		f := cmd.Flags().Lookup(flag)
		if f == nil {
			t.Fatalf("rollback missing --%s", flag)
		}
		if ann := f.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
			t.Errorf("rollback --%s should be required; annotations=%v", flag, f.Annotations)
		}
	}
}

// TestLoadJSONObject — reads a JSON object file; rejects non-object/garbage.
func TestLoadJSONObject(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "spec.json")
	if err := os.WriteFile(good, []byte(`{"project":"default"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	obj, se := loadJSONObject(good)
	if se != nil {
		t.Fatalf("loadJSONObject good: %+v", se)
	}
	if obj["project"] != "default" {
		t.Errorf("decoded: %+v", obj)
	}
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte(`[1,2,3]`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, se := loadJSONObject(bad); se == nil {
		t.Errorf("expected error decoding a JSON array as object")
	}
	if _, se := loadJSONObject(filepath.Join(dir, "nope.json")); se == nil {
		t.Errorf("expected error for missing file")
	}
}

// guard: the verb constructors return non-nil commands (cheap smoke).
func TestWriteVerbConstructorsNonNil(t *testing.T) {
	ctors := []func() *cobra.Command{
		newAppSyncCmd, newAppRollbackCmd, newAppSetCmd, newAppRefreshCmd,
		newAppDeleteCmd, newAppProjectCreateCmd, newAppProjectUpdateCmd,
	}
	for i, ctor := range ctors {
		if ctor() == nil {
			t.Errorf("ctor %d returned nil", i)
		}
	}
}
