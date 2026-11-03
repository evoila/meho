// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// countServer answers POST /api/v1/audit/class-counts with status and
// body, and hands the request body it got to gotBody.
func countServer(t *testing.T, status int, body string, gotBody *string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/class-counts", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		b, _ := io.ReadAll(r.Body)
		if gotBody != nil {
			*gotBody = string(b)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv
}

const countsBody = `{"counts": {"operation_execution": 40, "policy_decision": 2,` +
	` "approval_decision": 0, "security_event": 1, "legacy_request": 7},` +
	` "total": 50, "since": "2026-10-28T06:00:00Z"}`

// TestCountPrintsTotalPerClassAndWindow — a normal answer prints the
// window, the total and one line per event class; without --since the
// output says only the last 24 hours were counted.
func TestCountPrintsTotalPerClassAndWindow(t *testing.T) {
	var got string
	srv := countServer(t, http.StatusOK, countsBody, &got)
	cmd, stdout, stderr := newRunCmd(t)
	if err := runCount(cmd, queryOptions{Target: "vc-1", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runCount: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(got, `"target":"vc-1"`) {
		t.Errorf("body missing target filter: %s", got)
	}
	out := stdout.String()
	for _, want := range []string{
		"Counted from: 2026-10-28T06:00:00Z (the last 24 hours; set --since for a longer window)",
		"Counted until: now",
		"Total rows: 50",
		"operation_execution    40",
		"legacy_request         7",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in:\n%s", want, out)
		}
	}
}

// TestCountWithSinceDoesNotClaimTheDefaultWindow — with --since the
// window line has no "last 24 hours" note, and every filter lands on
// the wire under the backend's key (no limit, no cursor).
func TestCountWithSinceDoesNotClaimTheDefaultWindow(t *testing.T) {
	var got string
	srv := countServer(t, http.StatusOK, countsBody, &got)
	cmd, stdout, stderr := newRunCmd(t)
	opts := queryOptions{
		Target:            "vc-1",
		Principal:         "someone",
		OpID:              "vmware.vm.*",
		OpClass:           "write",
		ResultStatus:      "ok",
		Since:             "7d",
		Until:             "1h",
		AuditID:           "00000000-0000-0000-0000-000000000001",
		ParentAuditID:     "00000000-0000-0000-0000-000000000002",
		SessionID:         "00000000-0000-0000-0000-000000000003",
		WorkRef:           "gh:evoila/meho#1",
		Limit:             50,
		Cursor:            "c",
		BackplaneOverride: srv.URL,
	}
	if err := runCount(cmd, opts); err != nil {
		t.Fatalf("runCount: %v; stderr=%s", err, stderr.String())
	}
	if strings.Contains(stdout.String(), "last 24 hours") {
		t.Errorf("window line should not claim the default window:\n%s", stdout.String())
	}
	if !strings.Contains(stdout.String(), "Counted until: 1h (as given to --until)") {
		t.Errorf("until line missing:\n%s", stdout.String())
	}
	var wire map[string]any
	if err := json.Unmarshal([]byte(got), &wire); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	want := map[string]string{
		"target": "vc-1", "principal": "someone", "op_id": "vmware.vm.*",
		"op_class": "write", "result_status": "ok", "since": "7d", "until": "1h",
		"audit_id":         "00000000-0000-0000-0000-000000000001",
		"parent_audit_id":  "00000000-0000-0000-0000-000000000002",
		"agent_session_id": "00000000-0000-0000-0000-000000000003",
		"work_ref":         "gh:evoila/meho#1",
	}
	for k, v := range want {
		if wire[k] != v {
			t.Errorf("wire %s: got %v; want %q", k, wire[k], v)
		}
	}
	for _, k := range []string{"limit", "cursor", "event_class"} {
		if _, ok := wire[k]; ok {
			t.Errorf("wire must not carry %q (the route refuses it): %s", k, got)
		}
	}
}

// TestCountJSONPrintsAnswerAsItCame — --json writes the server bytes.
func TestCountJSONPrintsAnswerAsItCame(t *testing.T) {
	srv := countServer(t, http.StatusOK, countsBody, nil)
	cmd, stdout, stderr := newRunCmd(t)
	if err := runCount(cmd, queryOptions{JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runCount: %v; stderr=%s", err, stderr.String())
	}
	if stdout.String() != countsBody+"\n" {
		t.Fatalf("--json should print the body as it came; got %q", stdout.String())
	}
}

// TestCountRefusedSinceSurfacesTheReason — the backplane refuses a bad
// --since with 400; the command fails and shows the reason.
func TestCountRefusedSinceSurfacesTheReason(t *testing.T) {
	srv := countServer(t, http.StatusBadRequest, `{"detail": "unrecognised duration 'yesterday'"}`, nil)
	cmd, stdout, stderr := newRunCmd(t)
	err := runCount(cmd, queryOptions{Since: "yesterday", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected an error for a refused --since")
	}
	if !strings.Contains(stderr.String(), "unrecognised duration 'yesterday'") {
		t.Errorf("stderr missing the reason: %s", stderr.String())
	}
	if stdout.String() != "" {
		t.Errorf("stdout should be empty; got %q", stdout.String())
	}
}

// TestCountBadUUIDShortCircuits — a malformed UUID filter is refused
// before any request.
func TestCountBadUUIDShortCircuits(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCount(cmd, queryOptions{SessionID: "not-a-uuid", BackplaneOverride: "http://127.0.0.1:1"})
	if err == nil || !strings.Contains(stderr.String(), "--session-id must be a valid UUID") {
		t.Fatalf("expected a UUID refusal; err=%v stderr=%s", err, stderr.String())
	}
}

// TestCountFlagNames — the work-ref filter is --filter-work-ref, so the
// command does not hide the global --work-ref flag; there is no
// --limit or --cursor.
func TestCountFlagNames(t *testing.T) {
	cmd := newCountCmd()
	for _, name := range []string{
		"target", "principal", "op-id", "op-class", "result-status", "since", "until",
		"audit-id", "parent-audit-id", "session-id", "filter-work-ref", "json", "backplane",
	} {
		if cmd.Flags().Lookup(name) == nil {
			t.Errorf("missing flag --%s", name)
		}
	}
	for _, name := range []string{"work-ref", "limit", "cursor"} {
		if cmd.Flags().Lookup(name) != nil {
			t.Errorf("unexpected flag --%s", name)
		}
	}
}
