// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

func newRunSummary(t *testing.T, id, slug, assignedTo string, state api.RunSummaryState, n, total int) api.RunSummary {
	t.Helper()
	u, err := uuid.Parse(id)
	if err != nil {
		t.Fatalf("uuid.Parse: %v", err)
	}
	var pos *api.StepPosition
	if state == api.RunSummaryState("in_progress") {
		pos = &api.StepPosition{N: n, Total: total}
	}
	return api.RunSummary{
		RunId:           u,
		TemplateSlug:    slug,
		TemplateVersion: 1,
		AssignedTo:      assignedTo,
		Target:          "host-1",
		State:           state,
		Position:        pos,
		StartedAt:       time.Date(2026, 5, 30, 12, 0, 0, 0, time.UTC),
	}
}

// TestRunListRunsHappyPath — issue test #16. Default 7-column table.
func TestRunListRunsHappyPath(t *testing.T) {
	var lastQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET; got %s", r.Method)
		}
		lastQuery = r.URL.RawQuery
		resp := api.RunbookListRunsResponse{Runs: []api.RunSummary{
			newRunSummary(t, "11111111-1111-4111-8111-111111111111",
				"vmware-host-quiesce", "alice", api.RunSummaryState("in_progress"), 2, 5),
			newRunSummary(t, "22222222-2222-4222-8222-222222222222",
				"vault-rotate", "bob", api.RunSummaryState("completed"), 0, 0),
		}}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(resp)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{
		Status:            "in_progress",
		Assignee:          "alice",
		TemplateSlug:      "vmware-host-quiesce",
		Limit:             50,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runListRuns: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{
		"status=in_progress", "assignee=alice",
		"template_slug=vmware-host-quiesce", "limit=50",
	} {
		if !strings.Contains(lastQuery, want) {
			t.Errorf("expected %q in query; got %q", want, lastQuery)
		}
	}
	out := stdout.String()
	for _, want := range []string{
		"RUN_ID", "TEMPLATE_SLUG", "VERSION", "ASSIGNED_TO", "STATE", "STEP", "STARTED_AT",
		"11111111", "vmware-host-quiesce", "alice", "in_progress", "2/5",
		"22222222", "vault-rotate", "bob", "completed", "-",
		"2026-05-30T12:00:00Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in output; got:\n%s", want, out)
		}
	}
}

// TestRunListRunsEmpty — empty list emits a one-liner.
func TestRunListRunsEmpty(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.RunbookListRunsResponse{Runs: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runListRuns empty: %v", err)
	}
	if !strings.Contains(stdout.String(), "no runbook runs in this tenant") {
		t.Errorf("expected empty-list hint; got %q", stdout.String())
	}
}

// TestRunListRunsJSON — issue test #17.
func TestRunListRunsJSON(t *testing.T) {
	expected := api.RunbookListRunsResponse{Runs: []api.RunSummary{
		newRunSummary(t, "11111111-1111-4111-8111-111111111111",
			"x", "alice", api.RunSummaryState("in_progress"), 1, 3),
	}}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(expected)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runListRuns --json: %v", err)
	}
	var decoded api.RunbookListRunsResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if len(decoded.Runs) != 1 || decoded.Runs[0].TemplateSlug != "x" {
		t.Errorf("envelope: %+v", decoded)
	}
	// Confirm full UUID is preserved in --json (not the 8-char
	// truncation the table renders).
	if decoded.Runs[0].RunId.String() != "11111111-1111-4111-8111-111111111111" {
		t.Errorf("expected full UUID in --json; got %q", decoded.Runs[0].RunId.String())
	}
}

// TestRunListRunsOperatorOmitsAssigneeQueryParam — issue test #18.
// An OPERATOR caller without --assignee → the CLI sends no
// `assignee` query param; the backend handles role-based scoping.
func TestRunListRunsOperatorOmitsAssigneeQueryParam(t *testing.T) {
	var lastQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, r *http.Request) {
		lastQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.RunbookListRunsResponse{Runs: []api.RunSummary{
			newRunSummary(t, "11111111-1111-4111-8111-111111111111",
				"x", "operator-self", api.RunSummaryState("in_progress"), 1, 2),
		}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runListRuns: %v", err)
	}
	if strings.Contains(lastQuery, "assignee=") {
		t.Errorf("OPERATOR call without --assignee should omit the query param; got %q", lastQuery)
	}
}

// TestRunListRunsAdminWithAssignee — issue test #19. Admin can
// narrow to a junior's assignee; the CLI passes the flag verbatim.
func TestRunListRunsAdminWithAssignee(t *testing.T) {
	var lastQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, r *http.Request) {
		lastQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.RunbookListRunsResponse{Runs: []api.RunSummary{
			newRunSummary(t, "11111111-1111-4111-8111-111111111111",
				"x", "junior-bob", api.RunSummaryState("in_progress"), 1, 2),
		}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{
		Assignee:          "junior-bob",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runListRuns: %v", err)
	}
	if !strings.Contains(lastQuery, "assignee=junior-bob") {
		t.Errorf("expected assignee in query; got %q", lastQuery)
	}
}

// TestRunListRunsForwardsWorkRef — issue #1661. The --work-ref filter
// is forwarded verbatim as the work_ref query param.
func TestRunListRunsForwardsWorkRef(t *testing.T) {
	var lastQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, r *http.Request) {
		lastQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.RunbookListRunsResponse{Runs: []api.RunSummary{
			newRunSummary(t, "11111111-1111-4111-8111-111111111111",
				"x", "operator-self", api.RunSummaryState("in_progress"), 1, 2),
		}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{
		WorkRef:           "gh:evoila/meho#9",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runListRuns: %v", err)
	}
	if !strings.Contains(lastQuery, "work_ref=") ||
		!strings.Contains(lastQuery, url.QueryEscape("gh:evoila/meho#9")) {
		t.Errorf("expected work_ref filter in query; got %q", lastQuery)
	}
}

// TestRunListRunsRejectsBadStatus — typo fails fast.
func TestRunListRunsRejectsBadStatus(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{Status: "pending"})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "in_progress, completed, abandoned") {
		t.Errorf("expected enum hint; got %q", stderr.String())
	}
}

// TestRunListRunsRejectsOutOfRangeLimit — > 500 fails fast.
func TestRunListRunsRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{Limit: 501})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "between 1 and 500") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

// TestRunListRuns403SurfacesInsufficientRole — operator-role 403
// surfaces as exit 5.
func TestRunListRuns403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: operator required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "operator required") {
		t.Errorf("expected role detail in stderr; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunListRunsOpacityNoStepBodyLeak — RunSummary's contract says
// no step contents leak in the list (per #1313 _LIST_DESCRIPTION).
// Backend that put a Body field on a summary row would be a contract
// violation; the CLI's table renderer reads only RunSummary fields,
// so the test asserts the rendered output is bounded to the 7
// table columns.
func TestRunListRunsOpacityNoStepBodyLeak(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		// Inject a leaked step body field at the top of the JSON.
		// The generated RunSummary type doesn't have a `body`
		// field; encoding/json will drop it on decode, and the
		// table renderer's column list explicitly enumerates the
		// 7 visible columns.
		raw := `{
			"runs": [
				{
					"run_id": "11111111-1111-4111-8111-111111111111",
					"template_slug": "x",
					"template_version": 1,
					"assigned_to": "alice",
					"target": "h",
					"state": "in_progress",
					"position": {"n": 1, "total": 2},
					"started_at": "2026-05-30T12:00:00Z",
					"body": "LEAKED_STEP_BODY",
					"current_step": {"body": "LEAKED_STEP_BODY_NESTED"}
				}
			]
		}`
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = fmt.Fprint(w, raw)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runListRuns(cmd, listRunsOptions{BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runListRuns: %v", err)
	}
	if strings.Contains(stdout.String(), "LEAKED_STEP_BODY") {
		t.Errorf("OPACITY VIOLATION: leaked step body in table; got:\n%s", stdout.String())
	}
}

// TestListRunsParamsOmitsZeroValues — pointer fields stay nil when
// the operator didn't supply the corresponding flag.
func TestListRunsParamsOmitsZeroValues(t *testing.T) {
	got := listRunsParams(listRunsOptions{})
	if got.Assignee != nil {
		t.Errorf("expected nil Assignee; got %v", *got.Assignee)
	}
	if got.Status != nil {
		t.Errorf("expected nil Status; got %v", *got.Status)
	}
	if got.TemplateSlug != nil {
		t.Errorf("expected nil TemplateSlug; got %v", *got.TemplateSlug)
	}
	if got.Limit != nil {
		t.Errorf("expected nil Limit; got %v", *got.Limit)
	}
}

// TestListRunsParamsSetsAllFilters — supplied flags reach the typed
// params shape with the right discriminator.
func TestListRunsParamsSetsAllFilters(t *testing.T) {
	got := listRunsParams(listRunsOptions{
		Assignee: "alice", Status: "in_progress", TemplateSlug: "x", Limit: 100,
	})
	if got.Assignee == nil || *got.Assignee != "alice" {
		t.Errorf("Assignee: %+v", got.Assignee)
	}
	if got.Status == nil || string(*got.Status) != "in_progress" {
		t.Errorf("Status: %+v", got.Status)
	}
	if got.TemplateSlug == nil || *got.TemplateSlug != "x" {
		t.Errorf("TemplateSlug: %+v", got.TemplateSlug)
	}
	if got.Limit == nil || *got.Limit != 100 {
		t.Errorf("Limit: %+v", got.Limit)
	}
}

// TestTruncateRunIDShortInput — UUIDs shorter than 8 chars pass
// through (defensive; real UUIDs are 36).
func TestTruncateRunIDShortInput(t *testing.T) {
	if got := truncateRunID("abc"); got != "abc" {
		t.Errorf("got %q", got)
	}
	if got := truncateRunID("abcdef12-..."); got != "abcdef12" {
		t.Errorf("got %q", got)
	}
}
