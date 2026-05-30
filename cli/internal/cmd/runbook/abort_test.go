// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"bytes"
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

// withTTY claims a TTY is present for the duration of the test and
// restores the production probe on cleanup. Wired through the
// package-level stdinIsTTY seam so tests don't need an actual file
// descriptor.
func withTTY(t *testing.T, isTTY bool) {
	t.Helper()
	prev := stdinIsTTY
	stdinIsTTY = func() bool { return isTTY }
	t.Cleanup(func() { stdinIsTTY = prev })
}

func newAbortResponse(t *testing.T, runID string, state string, at time.Time) api.AbortRunResponse {
	t.Helper()
	u, err := uuid.Parse(runID)
	if err != nil {
		t.Fatalf("uuid.Parse(%q): %v", runID, err)
	}
	s := state
	return api.AbortRunResponse{
		RunId:       u,
		AbandonedAt: at,
		State:       &s,
	}
}

// TestRunAbortWithReasonFlag — issue test #13. --reason supplied;
// POST is made with the reason body; success message printed.
func TestRunAbortWithReasonFlag(t *testing.T) {
	var seen api.AbortRunRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/11111111-1111-4111-8111-111111111111/abort",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			raw, _ := io.ReadAll(r.Body)
			readJSONBodyOf(t, raw, &seen)
			resp := newAbortResponse(t, "11111111-1111-4111-8111-111111111111",
				"abandoned", time.Date(2026, 5, 30, 12, 0, 0, 0, time.UTC))
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(resp)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	// Reason supplied → the TTY check shouldn't be reached, but
	// pin non-TTY to prove the prompt path isn't entered.
	withTTY(t, false)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAbortRun(cmd, abortRunOptions{
		RunID:             "11111111-1111-4111-8111-111111111111",
		Reason:            "host hardware fault",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAbortRun: %v; stderr=%s", err, stderr.String())
	}
	if seen.Reason != "host hardware fault" {
		t.Errorf("reason on wire: got %q", seen.Reason)
	}
	out := stdout.String()
	for _, want := range []string{
		"Aborted run 11111111-1111-4111-8111-111111111111",
		"state=abandoned",
		"abandoned_at=2026-05-30T12:00:00Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in output; got:\n%s", want, out)
		}
	}
}

// TestRunAbortMissingReasonTTY — issue test #14. TTY mock; --reason
// missing; CLI prompts and reads from stdin; POST is made with the
// prompted reason.
func TestRunAbortMissingReasonTTY(t *testing.T) {
	var seen api.AbortRunRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/22222222-2222-4222-8222-222222222222/abort",
		func(w http.ResponseWriter, r *http.Request) {
			raw, _ := io.ReadAll(r.Body)
			readJSONBodyOf(t, raw, &seen)
			resp := newAbortResponse(t, "22222222-2222-4222-8222-222222222222",
				"abandoned", time.Date(2026, 5, 30, 13, 0, 0, 0, time.UTC))
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(resp)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	withTTY(t, true)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("manual rollback procedure\n"))
	err := runAbortRun(cmd, abortRunOptions{
		RunID:             "22222222-2222-4222-8222-222222222222",
		Reason:            "",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAbortRun: %v; stderr=%s", err, stderr.String())
	}
	if seen.Reason != "manual rollback procedure" {
		t.Errorf("expected prompted reason on wire; got %q", seen.Reason)
	}
	if !strings.Contains(stdout.String(), "Reason (recorded to audit_log)") {
		t.Errorf("expected prompt text on stdout; got:\n%s", stdout.String())
	}
}

// TestRunAbortMissingReasonNonTTY — issue test #15. Non-TTY +
// --reason missing → exit 1 with a useful message; no POST is made.
func TestRunAbortMissingReasonNonTTY(t *testing.T) {
	calls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/", func(_ http.ResponseWriter, _ *http.Request) {
		calls++
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	withTTY(t, false)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAbortRun(cmd, abortRunOptions{
		RunID:             "33333333-3333-4333-8333-333333333333",
		Reason:            "",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on non-TTY + missing --reason")
	}
	if calls != 0 {
		t.Errorf("expected no POST; backend received %d calls", calls)
	}
	if !strings.Contains(stderr.String(), "--reason is required when stdin is not a TTY") {
		t.Errorf("expected useful error message; got:\n%s", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 1 {
		t.Errorf("expected ExitCode 1 (caller error); got %v", err)
	}
}

// TestRunAbortRejectsBadUUID — bad arg fails fast.
func TestRunAbortRejectsBadUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runAbortRun(cmd, abortRunOptions{
		RunID:  "not-a-uuid",
		Reason: "x",
	})
	if err == nil {
		t.Fatal("expected error for bad uuid")
	}
	if !strings.Contains(stderr.String(), "invalid run_id") {
		t.Errorf("expected uuid hint; got %q", stderr.String())
	}
}

// TestRunAbort404SurfacesRunNotFound — substrate emits 404 for
// missing run (and cross-tenant probes).
func TestRunAbort404SurfacesRunNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/44444444-4444-4444-8444-444444444444/abort",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"detail":"RunNotFoundError"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAbortRun(cmd, abortRunOptions{
		RunID:             "44444444-4444-4444-8444-444444444444",
		Reason:            "x",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "RunNotFoundError") {
		t.Errorf("expected detail; got %q", stderr.String())
	}
}

// TestRunAbort403SurfacesNotAssignee — backend's role / assignee gate.
func TestRunAbort403SurfacesNotAssignee(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/55555555-5555-4555-8555-555555555555/abort",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"detail":"NotRunAssigneeError: caller is neither the assignee nor a tenant admin"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAbortRun(cmd, abortRunOptions{
		RunID:             "55555555-5555-4555-8555-555555555555",
		Reason:            "x",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on 403")
	}
	if !strings.Contains(stderr.String(), "NotRunAssigneeError") {
		t.Errorf("expected role hint; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunAbortJSONHappyPath — --json emits the AbortRunResponse
// envelope.
func TestRunAbortJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/66666666-6666-4666-8666-666666666666/abort",
		func(w http.ResponseWriter, _ *http.Request) {
			resp := newAbortResponse(t, "66666666-6666-4666-8666-666666666666",
				"abandoned", time.Date(2026, 5, 30, 14, 0, 0, 0, time.UTC))
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(resp)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	withTTY(t, false)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAbortRun(cmd, abortRunOptions{
		RunID:             "66666666-6666-4666-8666-666666666666",
		Reason:            "x",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAbortRun --json: %v", err)
	}
	var decoded api.AbortRunResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.State == nil || *decoded.State != "abandoned" {
		t.Errorf("envelope state: %+v", decoded.State)
	}
}

// TestRunAbortPromptEmptyAnswer — TTY but operator hits enter on the
// prompt → exit 1 with the same shape as non-TTY + missing reason.
// The backend's Field(min_length=1) would reject an empty reason at
// 422; we fast-fail locally for a clean error.
func TestRunAbortPromptEmptyAnswer(t *testing.T) {
	calls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/", func(_ http.ResponseWriter, _ *http.Request) {
		calls++
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	withTTY(t, true)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("\n")) // operator just pressed enter
	err := runAbortRun(cmd, abortRunOptions{
		RunID:             "77777777-7777-4777-8777-777777777777",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on empty prompted reason")
	}
	if calls != 0 {
		t.Errorf("expected no POST; backend received %d calls", calls)
	}
	if !strings.Contains(stderr.String(), "--reason is required") {
		t.Errorf("expected useful error; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 1 {
		t.Errorf("expected ExitCode 1; got %v", err)
	}
}
