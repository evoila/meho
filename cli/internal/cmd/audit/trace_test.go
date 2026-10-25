// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// TestNewTraceCmdRegistersListAndShow — the group has exactly the two
// verbs the task names.
func TestNewTraceCmdRegistersListAndShow(t *testing.T) {
	root := newTraceCmd()
	want := map[string]bool{"list": false, "show": false}
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho audit trace`", name)
		}
	}
}

// traceListPage is the fixture page two tests share: two operation
// runs with a trace and a continuation cursor.
func traceListPage(t *testing.T) api.AuditQueryResult {
	t.Helper()
	target := "lab-vcenter"
	next := "opaque-cursor-2"
	return api.AuditQueryResult{
		Rows: []api.AuditEntry{
			{
				Id:           mustUUID(t, "11111111-1111-1111-1111-111111111111"),
				Ts:           mustTS(t, "2026-10-20T10:00:00Z"),
				PrincipalSub: "op-1",
				TargetName:   &target,
				Method:       "MCP",
				Path:         "/mcp",
				StatusCode:   200,
				OpId:         "vmware.vm.list",
				OpClass:      "read",
				ResultStatus: "ok",
				EventClass:   "operation_execution",
				Payload:      map[string]any{},
			},
			{
				Id:           mustUUID(t, "22222222-2222-2222-2222-222222222222"),
				Ts:           mustTS(t, "2026-10-19T09:00:00Z"),
				PrincipalSub: "op-1",
				Method:       "MCP",
				Path:         "/mcp",
				StatusCode:   500,
				OpId:         "vmware.vm.power_on",
				OpClass:      "write",
				ResultStatus: "error",
				EventClass:   "operation_execution",
				Payload:      map[string]any{},
			},
		},
		NextCursor: &next,
	}
}

// TestRunTraceListSendsHasTraceAndFlags — the request body fixes
// has_trace=true and carries the flags with the same names as
// `meho audit query`; the table puts the audit id first and ends with
// the NEXT line.
func TestRunTraceListSendsHasTraceAndFlags(t *testing.T) {
	var got api.AuditQueryRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(traceListPage(t))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runTraceList(cmd, traceListOptions{
		OpID:              "vmware.*",
		Target:            "lab-vcenter",
		Since:             "30d",
		Until:             "1h",
		Limit:             50,
		Cursor:            "opaque-cursor-1",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runTraceList: %v; stderr=%s", err, stderr.String())
	}
	if got.HasTrace == nil || !*got.HasTrace {
		t.Errorf("has_trace: got %v; want true", got.HasTrace)
	}
	if got.OpId == nil || *got.OpId != "vmware.*" {
		t.Errorf("op_id: got %v", got.OpId)
	}
	if got.Target == nil || *got.Target != "lab-vcenter" {
		t.Errorf("target: got %v", got.Target)
	}
	if got.Since == nil || *got.Since != "30d" {
		t.Errorf("since: got %v", got.Since)
	}
	if got.Until == nil || *got.Until != "1h" {
		t.Errorf("until: got %v", got.Until)
	}
	if got.Limit == nil || *got.Limit != 50 {
		t.Errorf("limit: got %v", got.Limit)
	}
	if got.Cursor == nil || *got.Cursor != "opaque-cursor-1" {
		t.Errorf("cursor: got %v", got.Cursor)
	}

	out := stdout.String()
	lines := strings.Split(strings.TrimRight(out, "\n"), "\n")
	if len(lines) != 4 {
		t.Fatalf("want header + 2 rows + NEXT, got %d lines:\n%s", len(lines), out)
	}
	if !strings.HasPrefix(lines[0], "AUDIT ID") {
		t.Errorf("header does not start with AUDIT ID: %q", lines[0])
	}
	for _, col := range []string{"TIME", "TARGET", "OP_ID", "STATUS"} {
		if !strings.Contains(lines[0], col) {
			t.Errorf("header missing %s: %q", col, lines[0])
		}
	}
	if !strings.HasPrefix(lines[1], "11111111-1111-1111-1111-111111111111") {
		t.Errorf("row 1 does not start with its audit id: %q", lines[1])
	}
	for _, want := range []string{"lab-vcenter", "vmware.vm.list", "ok"} {
		if !strings.Contains(lines[1], want) {
			t.Errorf("row 1 missing %q: %q", want, lines[1])
		}
	}
	if !strings.HasPrefix(lines[2], "22222222-2222-2222-2222-222222222222") {
		t.Errorf("row 2 does not start with its audit id: %q", lines[2])
	}
	if !strings.Contains(lines[2], "error") {
		t.Errorf("row 2 missing status: %q", lines[2])
	}
	if !strings.Contains(lines[3], "NEXT: --cursor=opaque-cursor-2") {
		t.Errorf("missing NEXT line: %q", lines[3])
	}
}

// TestRunTraceListJSONPassesRouteAnswerUnchanged — --json writes the
// server bytes verbatim.
func TestRunTraceListJSONPassesRouteAnswerUnchanged(t *testing.T) {
	raw := `{"rows":[],"next_cursor":null,"big":12345678901234567890}`
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(raw))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runTraceList(cmd, traceListOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runTraceList: %v; stderr=%s", err, stderr.String())
	}
	if stdout.String() != raw+"\n" {
		t.Errorf("stdout is not the raw body:\n%s", stdout.String())
	}
}

// TestRunTraceListEmptyPage — zero rows is a clean exit with a plain
// message, not an error.
func TestRunTraceListEmptyPage(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runTraceList(cmd, traceListOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runTraceList: %v", err)
	}
	if !strings.Contains(stdout.String(), "no stored traces matched the filter") {
		t.Errorf("stdout missing empty message: %s", stdout.String())
	}
}

// TestRunTraceListRejectsBadLimit — the flag is checked before any
// network round-trip, like `meho audit query`.
func TestRunTraceListRejectsBadLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runTraceList(cmd, traceListOptions{Limit: 1001})
	if err == nil {
		t.Fatalf("expected error for --limit 1001")
	}
	if !strings.Contains(stderr.String(), "--limit must be between 1 and 1000") {
		t.Errorf("stderr missing limit hint: %s", stderr.String())
	}
}

// TestRunTraceList403IsInsufficientRole — a read_only login gets the
// CLI's role message and exit 5.
func TestRunTraceList403IsInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"operator role required"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runTraceList(cmd, traceListOptions{BackplaneOverride: srv.URL})
	assertExitCode(t, err, output.ExitInsufficientRole)
	if !strings.Contains(stderr.String(), "operator role required") {
		t.Errorf("stderr missing the role detail: %s", stderr.String())
	}
}

// assertExitCode checks that err carries the given process exit code.
func assertExitCode(t *testing.T, err error, want int) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected an error with exit code %d", want)
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("error %T does not carry an exit code", err)
	}
	if coder.ExitCode() != want {
		t.Errorf("exit code: got %d; want %d", coder.ExitCode(), want)
	}
}
