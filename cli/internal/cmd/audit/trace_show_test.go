// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/output"
)

const traceShowAuditID = "11111111-1111-1111-1111-111111111111"

// traceWithTwoSpans is the route's answer for a stored trace: a vendor
// call whose redacted headers and body hold a redaction marker, then
// the result reduction. The marker must reach --json unchanged and
// must never reach the human output.
const traceWithTwoSpans = `{"audit_id":"11111111-1111-1111-1111-111111111111",` +
	`"trace_present":true,"event_class":"operation_execution","trace":{` +
	`"trace_id":"33333333-3333-3333-3333-333333333333",` +
	`"audit_id":"11111111-1111-1111-1111-111111111111",` +
	`"tenant_id":"44444444-4444-4444-4444-444444444444",` +
	`"created_at":"2026-10-20T10:00:00Z","expires_at":"2026-10-27T10:00:00Z",` +
	`"redaction_uncertain":true,"spans":[` +
	`{"seq":0,"span_kind":"vendor_call","name":"GET https://vc.example.test/api/vcenter/vm",` +
	`"started_at":"2026-10-20T10:00:00Z","duration_ms":"12.5","status":"200",` +
	`"attributes":{"method":"GET","url":"https://vc.example.test/api/vcenter/vm",` +
	`"request_headers":{"authorization":"[REDACTED:token]"},` +
	`"response_headers":{"content-type":"application/json"},` +
	`"request_body":null,"response_body":"{\"password\":\"[REDACTED:secret]\"}"}},` +
	`{"seq":1,"span_kind":"jsonflux","name":"reduce","started_at":"2026-10-20T10:00:01Z",` +
	`"duration_ms":"0.4","status":null,"attributes":{"input_bytes":4096,"kept_rows":3}}]}}`

func serveTrace(t *testing.T, status int, body string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		want := "/api/v1/audit/" + traceShowAuditID + "/trace"
		if r.URL.Path != want {
			t.Errorf("path: got %q; want %q", r.URL.Path, want)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv
}

// TestRunTraceShowPrintsSpansInOrderWithoutHeadersOrBodies — the human
// output has the header lines and one line per span in order, with the
// vendor call's method and URL, and no header or body value.
func TestRunTraceShowPrintsSpansInOrderWithoutHeadersOrBodies(t *testing.T) {
	srv := serveTrace(t, http.StatusOK, traceWithTwoSpans)

	cmd, stdout, stderr := newRunCmd(t)
	err := runTraceShow(cmd, traceShowOptions{AuditID: traceShowAuditID, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runTraceShow: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"recorded_at:", "2026-10-20T10:00:00Z",
		"expires_at:", "2026-10-27T10:00:00Z",
		"withheld_from_agents:", "yes",
		"spans:", "2",
		"vendor_call", "GET https://vc.example.test/api/vcenter/vm", "200", "12.5",
		"jsonflux", "reduce",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
	first := strings.Index(out, "vendor_call")
	second := strings.Index(out, "jsonflux")
	if first < 0 || second < 0 || first > second {
		t.Errorf("spans are not printed in seq order:\n%s", out)
	}
	for _, leak := range []string{"REDACTED", "authorization", "content-type", "password", "response_body"} {
		if strings.Contains(out, leak) {
			t.Errorf("human output must not print header or body values; found %q:\n%s", leak, out)
		}
	}
}

// TestRunTraceShowJSONPrintsRouteAnswerUnchanged — --json is the
// route's body byte for byte, redaction marker included.
func TestRunTraceShowJSONPrintsRouteAnswerUnchanged(t *testing.T) {
	srv := serveTrace(t, http.StatusOK, traceWithTwoSpans)

	cmd, stdout, stderr := newRunCmd(t)
	err := runTraceShow(cmd, traceShowOptions{
		AuditID: traceShowAuditID, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runTraceShow: %v; stderr=%s", err, stderr.String())
	}
	if stdout.String() != traceWithTwoSpans+"\n" {
		t.Errorf("stdout is not the raw body:\n%s", stdout.String())
	}
	if !strings.Contains(stdout.String(), "[REDACTED:secret]") {
		t.Errorf("--json lost the redaction marker")
	}
}

// TestRunTraceShowNoTraceExitsZero — an audit row without a stored
// trace is a normal state: plain message, no error.
func TestRunTraceShowNoTraceExitsZero(t *testing.T) {
	srv := serveTrace(t, http.StatusOK, `{"audit_id":"`+traceShowAuditID+`",`+
		`"trace_present":false,"trace":null,"event_class":"policy_decision"}`)

	cmd, stdout, stderr := newRunCmd(t)
	err := runTraceShow(cmd, traceShowOptions{AuditID: traceShowAuditID, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("expected exit 0 for trace_present=false; got %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"No trace is stored for this audit row.",
		"capture is off", "already deleted", "not an operation run",
		"policy_decision",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
}

// TestRunTraceShow404MatchesAuditShow — unknown or other-tenant row:
// the `meho audit show` text and exit 4.
func TestRunTraceShow404MatchesAuditShow(t *testing.T) {
	srv := serveTrace(t, http.StatusNotFound, `{"detail":"audit row not found"}`)

	cmd, _, stderr := newRunCmd(t)
	err := runTraceShow(cmd, traceShowOptions{AuditID: traceShowAuditID, BackplaneOverride: srv.URL})
	assertExitCode(t, err, output.ExitUnexpected)
	if !strings.Contains(stderr.String(), "audit row not found") {
		t.Errorf("stderr missing not-found text: %s", stderr.String())
	}
}

// TestRunTraceShow403IsInsufficientRole — a read_only login gets the
// CLI's role message and exit 5.
func TestRunTraceShow403IsInsufficientRole(t *testing.T) {
	srv := serveTrace(t, http.StatusForbidden, `{"detail":"operator role required"}`)

	cmd, _, stderr := newRunCmd(t)
	err := runTraceShow(cmd, traceShowOptions{AuditID: traceShowAuditID, BackplaneOverride: srv.URL})
	assertExitCode(t, err, output.ExitInsufficientRole)
	if !strings.Contains(stderr.String(), "operator role required") {
		t.Errorf("stderr missing the role detail: %s", stderr.String())
	}
}

// TestRunTraceShowRejectsNonUUIDArg — checked before any round-trip.
func TestRunTraceShowRejectsNonUUIDArg(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runTraceShow(cmd, traceShowOptions{AuditID: "not-a-uuid"})
	assertExitCode(t, err, output.ExitUnexpected)
	if !strings.Contains(stderr.String(), "audit-id is not a valid UUID") {
		t.Errorf("stderr missing UUID hint: %s", stderr.String())
	}
}
