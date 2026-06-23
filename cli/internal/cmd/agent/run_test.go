// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

// --- run ---

func TestRunSyncHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/triage/run", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		// Assert the typed request body decodes into the generated
		// AgentRunRequest shape, with the operator's --input on the
		// wire and the --async flag absent (defaults to false).
		var body api.AgentRunRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		if body.Input != "what happened?" {
			t.Errorf("unexpected input: %q", body.Input)
		}
		if body.Async != nil && *body.Async {
			t.Errorf("expected async=false on the wire for sync run")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(runResponse{
			RunID:  "11111111-1111-1111-1111-111111111111",
			Status: "succeeded",
			Output: map[string]any{"text": "triaged: ok"},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runRun(cmd, runOptions{Name: "triage", Input: "what happened?", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runRun: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"succeeded", "triaged: ok"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunAsyncPrintsHandleHint(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/triage/run", func(w http.ResponseWriter, r *http.Request) {
		var body api.AgentRunRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body.Async == nil || !*body.Async {
			t.Errorf("expected async=true on the wire")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(runResponse{
			RunID:  "22222222-2222-2222-2222-222222222222",
			Status: "running",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runRun(cmd, runOptions{Name: "triage", Input: "go", Async: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runRun async: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "run-status") {
		t.Errorf("async output missing poll hint; got %q", stdout.String())
	}
}

func TestRunRequiresInput(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runRun(cmd, runOptions{Name: "triage", Input: "", BackplaneOverride: "http://x"})
	if err == nil {
		t.Fatalf("expected error for empty --input")
	}
	if !strings.Contains(stderr.String(), "input") {
		t.Errorf("error should mention input; got %q", stderr.String())
	}
}

// --- run-status ---

func TestRunStatusRejectsInvalidUUID(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runRunStatus(cmd, runStatusOptions{Handle: "abc", BackplaneOverride: "http://x"})
	if err == nil {
		t.Fatalf("expected error for non-UUID handle")
	}
	if !strings.Contains(stderr.String(), "invalid <handle>") {
		t.Errorf("stderr missing parse-error hint; got %q", stderr.String())
	}
}

func TestRunStatusHappyPath(t *testing.T) {
	handle := "11111111-1111-1111-1111-111111111111"
	provider := "anthropic"
	model := "claude-sonnet-4-6"
	output := map[string]any{"text": "done"}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs/"+handle, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AgentRunStatusResponse{
			RunId:    uuid.MustParse(handle),
			Status:   api.AgentRunStatusSucceeded,
			Turns:    2,
			Provider: &provider,
			Model:    &model,
			Output:   &output,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runRunStatus(cmd, runStatusOptions{Handle: handle, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRunStatus: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"succeeded", "anthropic", "done"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunStatusNotFoundRendersError(t *testing.T) {
	handle := "33333333-3333-3333-3333-333333333333"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs/"+handle, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]any{"detail": "agent_run_not_found"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runRunStatus(cmd, runStatusOptions{Handle: handle, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "agent_run_not_found") {
		t.Errorf("error should carry the detail; got %q", stderr.String())
	}
}

// --- run-cancel ---

func TestRunCancelRejectsInvalidUUID(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runRunCancel(cmd, runCancelOptions{Handle: "abc", BackplaneOverride: "http://x"})
	if err == nil {
		t.Fatalf("expected error for non-UUID handle")
	}
	if !strings.Contains(stderr.String(), "invalid <handle>") {
		t.Errorf("stderr missing parse-error hint; got %q", stderr.String())
	}
}

func TestRunCancelHappyPath(t *testing.T) {
	handle := "11111111-1111-1111-1111-111111111111"
	provider := "anthropic"
	model := "claude-sonnet-4-6"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs/"+handle+"/cancel", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AgentRunSummaryResponse{
			RunId:     uuid.MustParse(handle),
			Status:    api.AgentRunStatusCancelled,
			Trigger:   "direct",
			ModelTier: "standard",
			Provider:  &provider,
			Model:     &model,
			Turns:     1,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runRunCancel(cmd, runCancelOptions{Handle: handle, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRunCancel: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"cancelled", handle, "anthropic"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunCancelNotFoundRendersError(t *testing.T) {
	handle := "33333333-3333-3333-3333-333333333333"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs/"+handle+"/cancel", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]any{"detail": "agent_run_not_found"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runRunCancel(cmd, runCancelOptions{Handle: handle, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "agent_run_not_found") {
		t.Errorf("error should carry the detail; got %q", stderr.String())
	}
}

func TestRunCancelAlreadyTerminalRendersError(t *testing.T) {
	handle := "44444444-4444-4444-4444-444444444444"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs/"+handle+"/cancel", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(w).Encode(map[string]any{"detail": "agent_run_not_cancellable"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runRunCancel(cmd, runCancelOptions{Handle: handle, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 409")
	}
	if !strings.Contains(stderr.String(), "agent_run_not_cancellable") {
		t.Errorf("error should carry the 409 detail; got %q", stderr.String())
	}
}

// --- run-events (SSE) ---

func TestRunEventsStreamsFrames(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/triage/run/events", func(w http.ResponseWriter, r *http.Request) {
		// Verify the SSE-specific Accept override the verb sets via
		// the per-call RequestEditorFn so the backend's negotiation
		// reads the right content-type intent.
		if got := r.Header.Get("Accept"); got != "text/event-stream" {
			t.Errorf("Accept header: got %q; want text/event-stream", got)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "event: turn\ndata: {\"run_id\":\"r1\"}\n\n")
		fmt.Fprint(w, ": heartbeat\n\n")
		fmt.Fprint(w, "event: final\ndata: {\"run_id\":\"r1\",\"output\":\"answer\"}\n\n")
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := streamRunEvents(cmd, srv.URL, runEventsOptions{Name: "triage", Input: "go"})
	if err != nil {
		t.Fatalf("streamRunEvents: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"turn", "final", "answer"} {
		if !strings.Contains(out, want) {
			t.Errorf("stream output missing %q in %q", want, out)
		}
	}
	if strings.Contains(out, "heartbeat") {
		t.Errorf("heartbeat comment should be skipped; got %q", out)
	}
}

func TestPrintSSEEventJSON(t *testing.T) {
	var sb strings.Builder
	printSSEEvent(&sb, "tool_call", `{"tool_name":"call_operation"}`, true)
	if !strings.Contains(sb.String(), `"event":"tool_call"`) {
		t.Errorf("json event missing merged event kind; got %q", sb.String())
	}
}

// TestRunEvents403SingleErrorLine — regression for B1 on #1277. A non-2xx
// SSE handshake is rendered by streamRunEvents via renderHTTPStatus, which
// returns an already-rendered *silentError. runRunEvents must NOT re-route
// that error through renderRequestError (which would fall through to
// output.Unreachable and emit a second `meho:` stderr line — a direct
// regression of AC #6 "byte-identical to main output" for the 401/403/
// 404/422 paths on this streaming verb).
func TestRunEvents403SingleErrorLine(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/triage/run/events", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runRunEvents(cmd, runEventsOptions{
		Name: "triage", Input: "go", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 403")
	}
	got := stderr.String()
	if c := strings.Count(got, "meho:"); c != 1 {
		t.Errorf("expected exactly one 'meho:' stderr line, got %d: %q", c, got)
	}
	if !strings.Contains(got, "insufficient_role") {
		t.Errorf("expected insufficient_role category in stderr; got %q", got)
	}
	if strings.Contains(got, "unreachable") {
		t.Errorf("403 must not surface as 'unreachable' (B1 regression); got %q", got)
	}
}

// --- run-list ---

func TestRunListHappyPathRendersTable(t *testing.T) {
	workRef := "gh:evoila/meho#11"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]api.AgentRunSummaryResponse{
			{
				RunId:     uuid.MustParse("11111111-1111-1111-1111-111111111111"),
				Status:    api.AgentRunStatusSucceeded,
				Trigger:   "direct",
				ModelTier: "standard",
				Turns:     2,
				WorkRef:   &workRef,
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runRunList(cmd, runListOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRunList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"succeeded", "direct", workRef} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunListPassesWorkRefFilter(t *testing.T) {
	var gotQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs", func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode([]api.AgentRunSummaryResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runRunList(cmd, runListOptions{
		WorkRef:           "gh:evoila/meho#11",
		Status:            "succeeded",
		Limit:             25,
		Offset:            50,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRunList: %v; stderr=%s", err, stderr.String())
	}
	q, perr := url.ParseQuery(gotQuery)
	if perr != nil {
		t.Fatalf("parse query %q: %v", gotQuery, perr)
	}
	if got := q.Get("work_ref"); got != "gh:evoila/meho#11" {
		t.Errorf("work_ref filter: got %q, want %q", got, "gh:evoila/meho#11")
	}
	if got := q.Get("status"); got != "succeeded" {
		t.Errorf("status filter: got %q, want %q", got, "succeeded")
	}
	if got := q.Get("limit"); got != "25" {
		t.Errorf("limit filter: got %q, want %q", got, "25")
	}
	if got := q.Get("offset"); got != "50" {
		t.Errorf("offset filter: got %q, want %q", got, "50")
	}
	if !strings.Contains(stdout.String(), "no agent runs") {
		t.Errorf("empty list should print 'no agent runs'; got %q", stdout.String())
	}
}

func TestListRunsParamsOmitsEmptyFilters(t *testing.T) {
	params := listRunsParams(runListOptions{})
	if params.WorkRef != nil || params.Status != nil || params.Limit != nil || params.Offset != nil {
		t.Errorf("empty options must leave filters nil; got %+v", params)
	}
}
