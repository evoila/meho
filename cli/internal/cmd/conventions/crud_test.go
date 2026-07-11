// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

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

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

// stubID / stubTenantID are the fixed UUIDs every fixture uses so a
// test failure reads "11111111-..." in the request URL or response
// body — easier to chase than a uuid.New() that rotates per run.
// Mirrors the same const pair in cli/internal/cmd/agent-principal/
// agent_principal_test.go (T4 #1262).
const (
	stubID       = "11111111-1111-1111-1111-111111111111"
	stubTenantID = "22222222-2222-2222-2222-222222222222"
)

func mustUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	id, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("mustUUID(%q): %v", s, err)
	}
	return id
}

// writeJSON wraps the common httptest.Server handler pattern so every
// mock response sets `Content-Type: application/json` before writing.
// The generated client's Parse* helpers only populate JSON200 / JSON201
// when the response Content-Type contains "json"; a bare Encode against
// http.ResponseWriter omits the header and the response body bytes
// land in `.Body` but the typed `JSONxxx` pointer stays nil. Centralise
// the header-then-encode so a test failure can't be a misset header.
func writeJSON(t *testing.T, w http.ResponseWriter, status int, body any) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		t.Errorf("writeJSON encode: %v", err)
	}
}

// writeJSONErr is the equivalent of writeJSON for error bodies — the
// status code is the *first* arg (mirrors writeJSON's signature)
// and the body is the raw JSON envelope the FastAPI route would emit
// for that status. Returns a (status, body) shape into the typed
// response's `.Body` field so renderHTTPStatus can pick up the
// backend's `detail` field.
func writeJSONErr(t *testing.T, w http.ResponseWriter, status int, body string) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write([]byte(body)); err != nil {
		t.Errorf("writeJSONErr write: %v", err)
	}
}

// sampleConvention returns a fully-populated api.Convention (the
// generated type) for happy-path round-trips. Same field semantics
// as the pre-migration consumer-side duplicate; only the type moved.
func sampleConvention(t *testing.T) api.Convention {
	t.Helper()
	ts := time.Date(2026, 5, 24, 0, 0, 0, 0, time.UTC)
	return api.Convention{
		Id:        mustUUID(t, stubID),
		TenantId:  mustUUID(t, stubTenantID),
		Slug:      "vault-canonical",
		Title:     "Vault is canonical",
		Body:      "Vault is the canonical secret store.\nNever paste secrets into chat.",
		Kind:      "operational",
		Priority:  10,
		CreatedAt: ts,
		UpdatedAt: ts,
	}
}

func sampleSummary(t *testing.T) api.ConventionSummary {
	t.Helper()
	ts := time.Date(2026, 5, 24, 0, 0, 0, 0, time.UTC)
	return api.ConventionSummary{
		Id:        mustUUID(t, stubID),
		TenantId:  mustUUID(t, stubTenantID),
		Slug:      "vault-canonical",
		Title:     "Vault is canonical",
		Kind:      "operational",
		Priority:  10,
		CreatedAt: ts,
		UpdatedAt: ts,
	}
}

// --- list ---

// TestListQueryParamsOmitsKindWhenUnset confirms the default flag
// state sends no `kind` query param. The backplane's own default
// (returning all kinds) then applies; sending an explicit empty
// `kind` would trip pydantic's enum validation.
func TestListQueryParamsOmitsKindWhenUnset(t *testing.T) {
	params := listQueryParams(listOptions{})
	if params.Kind != nil {
		t.Errorf("unset --kind should leave params.Kind nil; got %+v", params.Kind)
	}
}

// TestListQueryParamsPassesKindWhenSet pins that the validated
// string is forwarded as the generated typed enum.
func TestListQueryParamsPassesKindWhenSet(t *testing.T) {
	params := listQueryParams(listOptions{Kind: "operational"})
	if params.Kind == nil || *params.Kind != api.ConventionKind("operational") {
		t.Errorf("expected params.Kind == operational; got %+v", params.Kind)
	}
}

func TestRunListRejectsBadKind(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{Kind: "garbage"})
	if err == nil {
		t.Fatalf("expected error for bad kind")
	}
	if !strings.Contains(stderr.String(), "operational, workflow, reference") {
		t.Errorf("stderr missing kind hint; got %q", stderr.String())
	}
}

func TestRunListHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		writeJSON(t, w, http.StatusOK, api.ConventionListResponse{
			Items: []api.ConventionSummary{sampleSummary(t)},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"SLUG", "vault-canonical", "operational", "Vault is canonical"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestRunListJSONPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, api.ConventionListResponse{
			Items: []api.ConventionSummary{sampleSummary(t)},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL, JSONOut: true}); err != nil {
		t.Fatalf("runList --json: %v", err)
	}
	var got api.ConventionListResponse
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode stdout JSON: %v; raw=%s", err, stdout.String())
	}
	if len(got.Items) != 1 || got.Items[0].Slug != "vault-canonical" {
		t.Errorf("decoded JSON unexpected: %+v", got)
	}
}

func TestPrintListTableEmpty(t *testing.T) {
	var sb strings.Builder
	printListTable(&sb, &api.ConventionListResponse{})
	if !strings.Contains(sb.String(), "no conventions registered") {
		t.Errorf("empty render missing hint; got %q", sb.String())
	}
}

// TestRunListOverBudgetExitsFive — G7.1-T7 (#1094) the deferred AC.
// Table-mode list against an over-budget tenant prints the stderr
// warning naming the dropped slugs, exits with code 5
// (insufficient_budget), and still writes the table to stdout so
// scripted consumers redirecting stdout still see the data.
func TestRunListOverBudgetExitsFive(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, api.ConventionListResponse{
			Items: []api.ConventionSummary{sampleSummary(t)},
			BudgetStatus: api.BudgetStatus{
				MaxTokens:       600,
				EstimatedTokens: 920,
				OverBudget:      true,
				DroppedSlugs:    []string{"low-priority-rule", "lower-priority-rule"},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected non-nil error for over-budget tenant; stdout=%q stderr=%q",
			stdout.String(), stderr.String())
	}
	// Exit code: must be 5 (insufficient_budget).
	exitCoder, ok := err.(interface{ ExitCode() int })
	if !ok {
		t.Fatalf("error does not implement ExitCode(): %T", err)
	}
	if got := exitCoder.ExitCode(); got != 5 {
		t.Errorf("exit code = %d; want 5 (insufficient_budget)", got)
	}
	// Table goes to stdout — even when over-budget, the operator
	// wants to see what's actually registered.
	if !strings.Contains(stdout.String(), "vault-canonical") {
		t.Errorf("stdout missing table content; got %q", stdout.String())
	}
	// Warning + structured error envelope go to stderr together.
	for _, want := range []string{
		"WARNING",
		"max_tokens=600",
		"estimated=920",
		"DROPPED",
		"low-priority-rule",
		"lower-priority-rule",
		"insufficient_budget",
	} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr missing %q in %q", want, stderr.String())
		}
	}
}

// TestRunListOverBudgetJSONExitsZero — `--json` mode is the agent /
// scripting surface; the entire envelope (entries + budget_status)
// goes to stdout and the exit code is 0 regardless of over-budget
// state. JSON consumers parse budget_status themselves.
func TestRunListOverBudgetJSONExitsZero(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, api.ConventionListResponse{
			Items: []api.ConventionSummary{sampleSummary(t)},
			BudgetStatus: api.BudgetStatus{
				MaxTokens:       600,
				EstimatedTokens: 920,
				OverBudget:      true,
				DroppedSlugs:    []string{"low-priority-rule"},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL, JSONOut: true}); err != nil {
		t.Fatalf("runList --json over budget: %v; stderr=%q", err, stderr.String())
	}
	// stderr stays clean — no human warning under --json.
	if stderr.Len() != 0 {
		t.Errorf("expected clean stderr under --json; got %q", stderr.String())
	}
	// stdout carries the full envelope including budget_status.
	var got api.ConventionListResponse
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode stdout JSON: %v; raw=%s", err, stdout.String())
	}
	if !got.BudgetStatus.OverBudget {
		t.Errorf("decoded JSON missing over_budget=true; got %+v", got.BudgetStatus)
	}
	if len(got.BudgetStatus.DroppedSlugs) != 1 || got.BudgetStatus.DroppedSlugs[0] != "low-priority-rule" {
		t.Errorf("decoded JSON dropped_slugs wrong: %+v", got.BudgetStatus.DroppedSlugs)
	}
}

// TestRunListFittingTenantExitsZero — sanity check that the table
// path is unchanged for the fitting (default) case: stdout carries
// the table, stderr is clean, exit code 0.
func TestRunListFittingTenantExitsZero(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, api.ConventionListResponse{
			Items: []api.ConventionSummary{sampleSummary(t)},
			BudgetStatus: api.BudgetStatus{
				MaxTokens:       600,
				EstimatedTokens: 120,
				OverBudget:      false,
				DroppedSlugs:    []string{},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList fitting tenant: %v; stderr=%q", err, stderr.String())
	}
	if stderr.Len() != 0 {
		t.Errorf("expected clean stderr for fitting tenant; got %q", stderr.String())
	}
	if !strings.Contains(stdout.String(), "vault-canonical") {
		t.Errorf("stdout missing table content; got %q", stdout.String())
	}
}

// TestRunListPassesKindOnWire confirms the typed query-param shape
// surfaces on the wire as `kind=operational`. The generated client
// handles the URL building; this test pins that we feed it the
// right value, not just that the func runs.
func TestRunListPassesKindOnWire(t *testing.T) {
	var seenKind string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, r *http.Request) {
		seenKind = r.URL.Query().Get("kind")
		writeJSON(t, w, http.StatusOK, api.ConventionListResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{Kind: "workflow", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList --kind: %v; stderr=%s", err, stderr.String())
	}
	if seenKind != "workflow" {
		t.Errorf("--kind=workflow should send kind=workflow on wire; got %q", seenKind)
	}
}

// --- show ---

func TestRunShowHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, sampleConvention(t))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{Slug: "vault-canonical", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "Vault is the canonical secret store") {
		t.Errorf("stdout missing body; got %q", stdout.String())
	}
}

func TestRunShow404SurfacesNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/nope", func(w http.ResponseWriter, _ *http.Request) {
		writeJSONErr(t, w, http.StatusNotFound, `{"detail":"convention_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{Slug: "nope", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "convention_not_found") {
		t.Errorf("stderr missing convention_not_found; got %q", stderr.String())
	}
}

func TestRunShowEmptySlugRejected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{Slug: ""})
	if err == nil {
		t.Fatalf("empty slug should be rejected")
	}
	if !strings.Contains(stderr.String(), "non-empty <slug>") {
		t.Errorf("stderr missing hint; got %q", stderr.String())
	}
}

// --- create ---

func TestRunCreateHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		// Decode against the generated request body type so a
		// schema-drift between the CLI's send and the backend's
		// expected shape would fail at unmarshal time.
		var body api.ConventionCreate
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode request body: %v", err)
		}
		if body.Slug != "vault-canonical" || body.Kind != api.ConventionKind("operational") ||
			body.Title != "Vault is canonical" {
			t.Errorf("unexpected request body: %+v", body)
		}
		if body.Body == "" {
			t.Errorf("body missing")
		}
		writeJSON(t, w, http.StatusCreated, sampleConvention(t))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug:              "vault-canonical",
		Kind:              "operational",
		Title:             "Vault is canonical",
		BodyArg:           "Vault is the canonical secret store.",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "created convention") {
		t.Errorf("stdout missing confirmation; got %q", stdout.String())
	}
}

func TestRunCreateRejectsBadKind(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "x", Kind: "garbage", Title: "t", BodyArg: "b",
	})
	if err == nil {
		t.Fatalf("expected error for bad kind")
	}
	if !strings.Contains(stderr.String(), "operational, workflow, reference") {
		t.Errorf("stderr missing kind hint; got %q", stderr.String())
	}
}

func TestRunCreateRejectsOutOfRangePriority(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "x", Kind: "operational", Title: "t", BodyArg: "b",
		Priority: 99999, prioritySet: true,
	})
	if err == nil {
		t.Fatalf("expected error for out-of-range priority")
	}
	if !strings.Contains(stderr.String(), "-32768 and 32767") {
		t.Errorf("stderr missing range hint; got %q", stderr.String())
	}
}

func TestRunCreateRejectsEmptyTitle(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "x", Kind: "operational", Title: "", BodyArg: "b",
	})
	if err == nil {
		t.Fatalf("expected error for empty title")
	}
	if !strings.Contains(stderr.String(), "non-empty --title") {
		t.Errorf("stderr missing title hint; got %q", stderr.String())
	}
}

func TestRunCreate409SurfacesDuplicate(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSONErr(t, w, http.StatusConflict, `{"detail":"convention_already_exists"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "dup", Kind: "operational", Title: "t", BodyArg: "b",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 409")
	}
	if !strings.Contains(stderr.String(), "convention_already_exists") {
		t.Errorf("stderr missing duplicate detail; got %q", stderr.String())
	}
}

func TestRunCreate403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSONErr(t, w, http.StatusForbidden, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "x", Kind: "operational", Title: "t", BodyArg: "b",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 403")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("stderr missing insufficient_role; got %q", stderr.String())
	}
}

func TestRunCreate422SurfacesOverBudget(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSONErr(t, w, http.StatusUnprocessableEntity,
			`{"detail":"convention body exceeds preamble budget (estimated=1200, budget=800)"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "x", Kind: "operational", Title: "t", BodyArg: "b",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 422")
	}
	if !strings.Contains(stderr.String(), "exceeds preamble budget") {
		t.Errorf("stderr missing over-budget detail; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "estimated=1200") {
		t.Errorf("stderr missing estimated count; got %q", stderr.String())
	}
}

// TestRunCreatePriorityOmittedWhenNotSet — load-bearing wire-format
// guarantee: an unset --priority flag must NOT marshal as
// `"priority":0` (which the backend would treat as "operator pinned
// to 0" if the column default ever moves). It also must NOT marshal
// as `"priority":null` (the generated `Priority *int` field has
// `omitempty`, so a nil pointer drops the key entirely).
func TestRunCreatePriorityOmittedWhenNotSet(t *testing.T) {
	var rawBody bytes.Buffer
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, r *http.Request) {
		if _, err := rawBody.ReadFrom(r.Body); err != nil {
			t.Fatalf("read body: %v", err)
		}
		writeJSON(t, w, http.StatusCreated, sampleConvention(t))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "x", Kind: "operational", Title: "t", BodyArg: "b",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	body := rawBody.String()
	if strings.Contains(body, `"priority"`) {
		t.Errorf("priority key present when --priority not set: %s", body)
	}
}

// TestRunCreatePrioritySentOnWireWhenSet pins the inverse: when the
// operator did pass --priority, the value must reach the wire.
func TestRunCreatePrioritySentOnWireWhenSet(t *testing.T) {
	var seen api.ConventionCreate
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&seen); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		writeJSON(t, w, http.StatusCreated, sampleConvention(t))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Slug: "x", Kind: "operational", Title: "t", BodyArg: "b",
		Priority: 42, prioritySet: true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCreate: %v", err)
	}
	if seen.Priority == nil || *seen.Priority != 42 {
		t.Errorf("body Priority: got %+v want pointer to 42", seen.Priority)
	}
}

// --- edit ---

func TestBuildEditRequestFlagDriven(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	req, err := buildEditRequest(cmd, "", editOptions{
		Slug: "x", Title: "new title", titleSet: true,
		Priority: 5, prioritySet: true,
	})
	if err != nil {
		t.Fatalf("buildEditRequest flag-driven: %v", err)
	}
	if req.Title == nil || *req.Title != "new title" {
		t.Errorf("title not set: %+v", req.Title)
	}
	if req.Priority == nil || *req.Priority != 5 {
		t.Errorf("priority not set: %+v", req.Priority)
	}
	if req.Body != nil {
		t.Errorf("body should be nil; got %+v", req.Body)
	}
}

func TestBuildEditRequestRejectsBadPriority(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	_, err := buildEditRequest(cmd, "", editOptions{
		Slug: "x", Priority: 99999, prioritySet: true,
	})
	if err == nil {
		t.Fatalf("expected error for out-of-range priority")
	}
}

func TestRunEditFlagDrivenHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPatch {
			t.Errorf("method: got %s; want PATCH", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(raw), "priority") {
			t.Errorf("PATCH body missing priority: %s", raw)
		}
		conv := sampleConvention(t)
		conv.Priority = 20
		writeJSON(t, w, http.StatusOK, conv)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runEdit(cmd, editOptions{
		Slug: "vault-canonical", Priority: 20, prioritySet: true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEdit: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "updated convention") {
		t.Errorf("stdout missing confirmation; got %q", stdout.String())
	}
}

func TestRunEditEditorModeHappyPath(t *testing.T) {
	// Stub the runEditor seam so the test doesn't spawn a real editor.
	orig := runEditor
	defer func() { runEditor = orig }()
	runEditor = func(_ *cobra.Command, initial string) (string, error) {
		if !strings.Contains(initial, "Vault is the canonical") {
			return "", fmt.Errorf("editor seed missing original body: %q", initial)
		}
		return initial + "\nAdditional rule line.\n", nil
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			writeJSON(t, w, http.StatusOK, sampleConvention(t))
		case http.MethodPatch:
			raw, _ := io.ReadAll(r.Body)
			var body api.ConventionUpdate
			_ = json.Unmarshal(raw, &body)
			if body.Body == nil {
				t.Errorf("PATCH body missing body field: %s", raw)
			} else if !strings.Contains(*body.Body, "Additional rule line") {
				t.Errorf("PATCH body lost editor edit: %s", *body.Body)
			}
			if body.Title != nil || body.Priority != nil {
				t.Errorf("PATCH body should only have body field: %+v", body)
			}
			conv := sampleConvention(t)
			conv.Body = *body.Body
			writeJSON(t, w, http.StatusOK, conv)
		default:
			t.Errorf("unexpected method %s", r.Method)
		}
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runEdit(cmd, editOptions{
		Slug:              "vault-canonical",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEdit editor mode: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "updated convention") {
		t.Errorf("stdout missing confirmation; got %q", stdout.String())
	}
}

func TestRunEditEditorModeAbortsOnEmptyBody(t *testing.T) {
	orig := runEditor
	defer func() { runEditor = orig }()
	runEditor = func(_ *cobra.Command, _ string) (string, error) {
		// Operator opened editor and saved an empty buffer (cleared
		// the content). Treat as abort.
		return "\n\n", nil
	}

	patchCount := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			writeJSON(t, w, http.StatusOK, sampleConvention(t))
			return
		}
		if r.Method == http.MethodPatch {
			patchCount++
			t.Errorf("PATCH should not be called on empty editor save")
		}
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runEdit(cmd, editOptions{
		Slug:              "vault-canonical",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on empty editor body")
	}
	if !strings.Contains(stderr.String(), "empty") {
		t.Errorf("stderr missing empty-body hint; got %q", stderr.String())
	}
	if patchCount != 0 {
		t.Errorf("PATCH called %d times; want 0", patchCount)
	}
}

func TestRunEditEditorModeAbortsOnUnchangedBody(t *testing.T) {
	orig := runEditor
	defer func() { runEditor = orig }()
	runEditor = func(_ *cobra.Command, initial string) (string, error) {
		// Operator opened, saved without changes. Skip the round-trip.
		return initial, nil
	}

	patchCount := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			writeJSON(t, w, http.StatusOK, sampleConvention(t))
			return
		}
		if r.Method == http.MethodPatch {
			patchCount++
			t.Errorf("PATCH should not be called when body unchanged")
		}
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runEdit(cmd, editOptions{
		Slug:              "vault-canonical",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on unchanged editor body")
	}
	if !strings.Contains(stderr.String(), "unchanged") {
		t.Errorf("stderr missing unchanged hint; got %q", stderr.String())
	}
	if patchCount != 0 {
		t.Errorf("PATCH called %d times; want 0", patchCount)
	}
}

func TestRunEditEditorModeAbortsOnEditorFailure(t *testing.T) {
	orig := runEditor
	defer func() { runEditor = orig }()
	runEditor = func(_ *cobra.Command, _ string) (string, error) {
		return "", fmt.Errorf("editor exited 1")
	}

	patchCount := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			writeJSON(t, w, http.StatusOK, sampleConvention(t))
			return
		}
		if r.Method == http.MethodPatch {
			patchCount++
		}
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runEdit(cmd, editOptions{
		Slug:              "vault-canonical",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on editor failure")
	}
	if !strings.Contains(stderr.String(), "editor session aborted") {
		t.Errorf("stderr missing editor abort hint; got %q", stderr.String())
	}
	if patchCount != 0 {
		t.Errorf("PATCH called %d times; want 0", patchCount)
	}
}

func TestRunEditEditorMode404SurfacesNotFoundFromShowFetch(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/nope", func(w http.ResponseWriter, _ *http.Request) {
		writeJSONErr(t, w, http.StatusNotFound, `{"detail":"convention_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runEdit(cmd, editOptions{
		Slug:              "nope",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "convention_not_found") {
		t.Errorf("stderr missing not-found detail; got %q", stderr.String())
	}
}

func TestRunEdit422OverBudgetSurfacedInline(t *testing.T) {
	orig := runEditor
	defer func() { runEditor = orig }()
	runEditor = func(_ *cobra.Command, initial string) (string, error) {
		return initial + "\nOversized line.", nil
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			writeJSON(t, w, http.StatusOK, sampleConvention(t))
		case http.MethodPatch:
			writeJSONErr(t, w, http.StatusUnprocessableEntity,
				`{"detail":"convention body exceeds preamble budget (estimated=900, budget=800)"}`)
		}
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runEdit(cmd, editOptions{
		Slug:              "vault-canonical",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 422")
	}
	if !strings.Contains(stderr.String(), "exceeds preamble budget") {
		t.Errorf("stderr missing over-budget detail; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "estimated=900") {
		t.Errorf("stderr missing estimated token count; got %q", stderr.String())
	}
}

// --- delete ---

func TestRunDeleteConfirmHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("method: got %s; want DELETE", r.Method)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runDelete(cmd, deleteOptions{
		Slug: "vault-canonical", Confirm: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDelete: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "deleted convention") {
		t.Errorf("stdout missing confirmation; got %q", stdout.String())
	}
}

func TestRunDeleteDeclinedExitsZero(t *testing.T) {
	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(strings.NewReader("n\n"))
	err := runDelete(cmd, deleteOptions{Slug: "vault-canonical"})
	if err != nil {
		t.Fatalf("declined delete should exit 0; got %v", err)
	}
	if !strings.Contains(stdout.String(), "declined") {
		t.Errorf("stdout missing declined line; got %q", stdout.String())
	}
}

func TestRunDelete404SurfacesNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/nope", func(w http.ResponseWriter, _ *http.Request) {
		writeJSONErr(t, w, http.StatusNotFound, `{"detail":"convention_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runDelete(cmd, deleteOptions{Slug: "nope", Confirm: true, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "convention_not_found") {
		t.Errorf("stderr missing convention_not_found; got %q", stderr.String())
	}
}

// --- history ---

func TestRunHistoryHappyPathRendersDiffs(t *testing.T) {
	bodyBefore := "Vault is canonical."
	entries := []api.ConventionHistoryEntry{
		{
			Id:           mustUUID(t, "33333333-3333-3333-3333-333333333333"),
			ConventionId: mustUUID(t, stubID),
			BodyBefore:   &bodyBefore,
			BodyAfter:    "Vault is canonical.\nAdded rule.",
			ActorSub:     "ops-admin",
			Ts:           time.Date(2026, 5, 25, 0, 0, 0, 0, time.UTC),
		},
		{
			Id:           mustUUID(t, "44444444-4444-4444-4444-444444444444"),
			ConventionId: mustUUID(t, stubID),
			BodyBefore:   nil,
			BodyAfter:    "Vault is canonical.",
			ActorSub:     "ops-admin",
			Ts:           time.Date(2026, 5, 24, 0, 0, 0, 0, time.UTC),
		},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical/history", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, entries)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runHistory(cmd, historyOptions{Slug: "vault-canonical", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runHistory: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"=== 2026-05-25T00:00:00Z",
		"+ Added rule.",
		"--- /dev/null", // CREATE row
	} {
		if !strings.Contains(out, want) {
			t.Errorf("history output missing %q in %q", want, out)
		}
	}
}

func TestRunHistoryLimit(t *testing.T) {
	bodyBefore := "x"
	idFor := func(i int) uuid.UUID {
		return mustUUID(t, fmt.Sprintf("%08x-0000-0000-0000-000000000000", i))
	}
	mkEntry := func(id uuid.UUID, day int) api.ConventionHistoryEntry {
		return api.ConventionHistoryEntry{
			Id:         id,
			BodyBefore: &bodyBefore,
			BodyAfter:  "y",
			Ts:         time.Date(2026, 5, day, 0, 0, 0, 0, time.UTC),
			ActorSub:   "ops",
		}
	}
	entries := []api.ConventionHistoryEntry{
		mkEntry(idFor(3), 26),
		mkEntry(idFor(2), 25),
		mkEntry(idFor(1), 24),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/x/history", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, entries)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runHistory(cmd, historyOptions{Slug: "x", Limit: 2, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runHistory --limit: %v; stderr=%s", err, stderr.String())
	}
	if strings.Contains(stdout.String(), idFor(1).String()) {
		t.Errorf("--limit 2 included beyond-limit row: %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), idFor(2).String()) {
		t.Errorf("--limit 2 dropped on-limit row: %q", stdout.String())
	}
}

func TestRunHistoryEmpty(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/x/history", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, []api.ConventionHistoryEntry{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runHistory(cmd, historyOptions{Slug: "x", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runHistory empty: %v", err)
	}
	if !strings.Contains(stdout.String(), "no history") {
		t.Errorf("empty render missing hint; got %q", stdout.String())
	}
}

func TestRunHistoryRejectsNegativeLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runHistory(cmd, historyOptions{Slug: "x", Limit: -1})
	if err == nil {
		t.Fatalf("expected error for negative --limit")
	}
	if !strings.Contains(stderr.String(), "non-negative") {
		t.Errorf("stderr missing range hint; got %q", stderr.String())
	}
}

func TestRunHistoryJSON(t *testing.T) {
	bodyBefore := "x"
	entries := []api.ConventionHistoryEntry{
		{
			Id:         mustUUID(t, stubID),
			BodyBefore: &bodyBefore,
			BodyAfter:  "y",
			Ts:         time.Date(2026, 5, 24, 0, 0, 0, 0, time.UTC),
			ActorSub:   "ops",
		},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/x/history", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(t, w, http.StatusOK, entries)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runHistory(cmd, historyOptions{Slug: "x", JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runHistory --json: %v", err)
	}
	var got []api.ConventionHistoryEntry
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode stdout JSON: %v; raw=%s", err, stdout.String())
	}
	if len(got) != 1 || got[0].Id != mustUUID(t, stubID) {
		t.Errorf("decoded JSON unexpected: %+v", got)
	}
}

// --- writeUnifiedDiff ---

func TestWriteUnifiedDiffEmitsAddRemove(t *testing.T) {
	var sb strings.Builder
	writeUnifiedDiff(&sb, "a\nb\nc", "a\nc\nd")
	out := sb.String()
	if !strings.Contains(out, "- b") {
		t.Errorf("missing removed line: %q", out)
	}
	if !strings.Contains(out, "+ d") {
		t.Errorf("missing added line: %q", out)
	}
	if !strings.Contains(out, "  a") {
		t.Errorf("missing context line: %q", out)
	}
}

// Confirm the cobra import isn't unused if no test happens to reference
// it directly — keep the symbol live.
var _ = cobra.Command{}
