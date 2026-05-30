// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/google/uuid"
)

func newReassignResponse(t *testing.T, runID, assignedTo string, at time.Time) api.ReassignRunResponse {
	t.Helper()
	u, err := uuid.Parse(runID)
	if err != nil {
		t.Fatalf("uuid.Parse(%q): %v", runID, err)
	}
	return api.ReassignRunResponse{
		RunId:        u,
		AssignedTo:   assignedTo,
		ReassignedAt: at,
	}
}

// TestRunReassignHappyPath — POST hits the route with the new
// assignee; success message printed.
func TestRunReassignHappyPath(t *testing.T) {
	var seen api.ReassignRunRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/11111111-1111-4111-8111-111111111111/reassign",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			raw, _ := io.ReadAll(r.Body)
			readJSONBodyOf(t, raw, &seen)
			resp := newReassignResponse(t, "11111111-1111-4111-8111-111111111111",
				"new-operator-sub", time.Date(2026, 5, 30, 12, 0, 0, 0, time.UTC))
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(resp)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runReassignRun(cmd, reassignRunOptions{
		RunID:             "11111111-1111-4111-8111-111111111111",
		NewAssignee:       "new-operator-sub",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runReassignRun: %v; stderr=%s", err, stderr.String())
	}
	if seen.NewAssignee != "new-operator-sub" {
		t.Errorf("new_assignee on wire: got %q", seen.NewAssignee)
	}
	out := stdout.String()
	for _, want := range []string{
		"Reassigned run 11111111-1111-4111-8111-111111111111 to new-operator-sub",
		"reassigned_at=2026-05-30T12:00:00Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in output; got:\n%s", want, out)
		}
	}
}

// TestRunReassignRejectsEmptyTo — --to is required.
func TestRunReassignRejectsEmptyTo(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runReassignRun(cmd, reassignRunOptions{
		RunID:       "11111111-1111-4111-8111-111111111111",
		NewAssignee: "",
	})
	if err == nil {
		t.Fatal("expected error for empty --to")
	}
	if !strings.Contains(stderr.String(), "--to") {
		t.Errorf("expected --to hint; got %q", stderr.String())
	}
}

// TestRunReassignRejectsBadUUID — bad arg fails fast.
func TestRunReassignRejectsBadUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runReassignRun(cmd, reassignRunOptions{
		RunID:       "not-a-uuid",
		NewAssignee: "x",
	})
	if err == nil {
		t.Fatal("expected error for bad uuid")
	}
	if !strings.Contains(stderr.String(), "invalid run_id") {
		t.Errorf("expected uuid hint; got %q", stderr.String())
	}
}

// TestRunReassign403SurfacesInsufficientRole — operator-role JWT
// trips the route's tenant_admin gate; CLI surfaces the role detail
// and exits 5.
func TestRunReassign403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/22222222-2222-4222-8222-222222222222/reassign",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runReassignRun(cmd, reassignRunOptions{
		RunID:             "22222222-2222-4222-8222-222222222222",
		NewAssignee:       "x",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected 403 error")
	}
	if !strings.Contains(stderr.String(), "tenant_admin required") {
		t.Errorf("expected role hint; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunReassign400SurfacesTerminalRun — re-assigning a terminal
// run lands as 400.
func TestRunReassign400SurfacesTerminalRun(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/33333333-3333-4333-8333-333333333333/reassign",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, `{"detail":"RunAlreadyTerminalError"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runReassignRun(cmd, reassignRunOptions{
		RunID:             "33333333-3333-4333-8333-333333333333",
		NewAssignee:       "x",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected 400 error")
	}
	if !strings.Contains(stderr.String(), "RunAlreadyTerminalError") {
		t.Errorf("expected terminal detail; got %q", stderr.String())
	}
}

// TestRunReassignJSONHappyPath — --json emits the envelope.
func TestRunReassignJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/44444444-4444-4444-8444-444444444444/reassign",
		func(w http.ResponseWriter, _ *http.Request) {
			resp := newReassignResponse(t, "44444444-4444-4444-8444-444444444444",
				"successor", time.Date(2026, 5, 30, 12, 0, 0, 0, time.UTC))
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(resp)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runReassignRun(cmd, reassignRunOptions{
		RunID:             "44444444-4444-4444-8444-444444444444",
		NewAssignee:       "successor",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runReassignRun --json: %v", err)
	}
	var decoded api.ReassignRunResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.AssignedTo != "successor" {
		t.Errorf("envelope: %+v", decoded)
	}
}
