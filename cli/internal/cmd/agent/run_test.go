// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// --- run ---

func TestBuildRunPathEscapes(t *testing.T) {
	if got := buildRunPath("vm.inventory-bot"); got != "/api/v1/agents/vm.inventory-bot/run" {
		t.Fatalf("buildRunPath: got %q", got)
	}
}

func TestRunSyncHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/triage/run", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		var body RunRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body.Input != "what happened?" {
			t.Errorf("unexpected input: %q", body.Input)
		}
		_ = json.NewEncoder(w).Encode(RunResult{
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
		var body RunRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		if !body.Async {
			t.Errorf("expected async=true on the wire")
		}
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(RunResult{
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

func TestBuildRunStatusPathEscapes(t *testing.T) {
	got := buildRunStatusPath("11111111-1111-1111-1111-111111111111")
	if got != "/api/v1/agents/runs/11111111-1111-1111-1111-111111111111" {
		t.Fatalf("buildRunStatusPath: got %q", got)
	}
}

func TestRunStatusHappyPath(t *testing.T) {
	provider := "anthropic"
	model := "claude-sonnet-4-6"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs/abc", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(RunStatus{
			RunID:    "abc",
			Status:   "succeeded",
			Turns:    2,
			Provider: &provider,
			Model:    &model,
			Output:   map[string]any{"text": "done"},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runRunStatus(cmd, runStatusOptions{Handle: "abc", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runRunStatus: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"succeeded", "anthropic", "done"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunStatusNotFoundRendersError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/runs/missing", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]any{"detail": "agent_run_not_found"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runRunStatus(cmd, runStatusOptions{Handle: "missing", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "agent_run_not_found") {
		t.Errorf("error should carry the detail; got %q", stderr.String())
	}
}

// --- run-events (SSE) ---

func TestBuildRunEventsPathEscapes(t *testing.T) {
	if got := buildRunEventsPath("vm.bot"); got != "/api/v1/agents/vm.bot/run/events" {
		t.Fatalf("buildRunEventsPath: got %q", got)
	}
}

func TestRunEventsStreamsFrames(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/triage/run/events", func(w http.ResponseWriter, _ *http.Request) {
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
