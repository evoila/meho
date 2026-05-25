// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func sampleConvention() Convention {
	return Convention{
		ID:        "11111111-1111-1111-1111-111111111111",
		TenantID:  "22222222-2222-2222-2222-222222222222",
		Slug:      "vault-canonical",
		Title:     "Vault is canonical",
		Body:      "Vault is the canonical secret store.\nNever paste secrets into chat.",
		Kind:      "operational",
		Priority:  10,
		CreatedAt: "2026-05-24T00:00:00Z",
		UpdatedAt: "2026-05-24T00:00:00Z",
	}
}

func sampleSummary() Summary {
	return Summary{
		ID:        "11111111-1111-1111-1111-111111111111",
		TenantID:  "22222222-2222-2222-2222-222222222222",
		Slug:      "vault-canonical",
		Title:     "Vault is canonical",
		Kind:      "operational",
		Priority:  10,
		CreatedAt: "2026-05-24T00:00:00Z",
		UpdatedAt: "2026-05-24T00:00:00Z",
	}
}

// --- list ---

func TestBuildListPath(t *testing.T) {
	if got := buildListPath(listOptions{}); got != "/api/v1/conventions" {
		t.Fatalf("empty opts: got %q", got)
	}
	got := buildListPath(listOptions{Kind: "operational"})
	if !strings.Contains(got, "kind=operational") {
		t.Errorf("buildListPath kind: got %q", got)
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
		_ = json.NewEncoder(w).Encode(ListResponse{Entries: []Summary{sampleSummary()}})
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
		_ = json.NewEncoder(w).Encode(ListResponse{Entries: []Summary{sampleSummary()}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL, JSONOut: true}); err != nil {
		t.Fatalf("runList --json: %v", err)
	}
	var got ListResponse
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode stdout JSON: %v; raw=%s", err, stdout.String())
	}
	if len(got.Entries) != 1 || got.Entries[0].Slug != "vault-canonical" {
		t.Errorf("decoded JSON unexpected: %+v", got)
	}
}

func TestPrintListTableEmpty(t *testing.T) {
	var sb strings.Builder
	printListTable(&sb, &ListResponse{})
	if !strings.Contains(sb.String(), "no conventions registered") {
		t.Errorf("empty render missing hint; got %q", sb.String())
	}
}

// --- show ---

func TestBuildShowPath(t *testing.T) {
	if got := buildShowPath("vault-canonical"); got != "/api/v1/conventions/vault-canonical" {
		t.Fatalf("buildShowPath: got %q", got)
	}
}

func TestRunShowHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(sampleConvention())
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
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"convention_not_found"}`)
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
		var body createRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body.Slug != "vault-canonical" || body.Kind != "operational" || body.Title != "Vault is canonical" {
			t.Errorf("unexpected request body: %+v", body)
		}
		if body.Body == "" {
			t.Errorf("body missing")
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(sampleConvention())
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
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"detail":"convention_already_exists"}`)
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
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
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
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":"convention body exceeds preamble budget (estimated=1200, budget=800)"}`)
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

func TestRunCreatePriorityOmittedWhenNotSet(t *testing.T) {
	var got createRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		// Confirm "priority" key is not in the JSON when not set.
		if strings.Contains(string(raw), `"priority"`) {
			t.Errorf("priority key present when --priority not set: %s", raw)
		}
		_ = json.Unmarshal(raw, &got)
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(sampleConvention())
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
		conv := sampleConvention()
		conv.Priority = 20
		_ = json.NewEncoder(w).Encode(conv)
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
			_ = json.NewEncoder(w).Encode(sampleConvention())
		case http.MethodPatch:
			raw, _ := io.ReadAll(r.Body)
			var body updateRequest
			_ = json.Unmarshal(raw, &body)
			if body.Body == nil {
				t.Errorf("PATCH body missing body field: %s", raw)
			} else if !strings.Contains(*body.Body, "Additional rule line") {
				t.Errorf("PATCH body lost editor edit: %s", *body.Body)
			}
			if body.Title != nil || body.Priority != nil {
				t.Errorf("PATCH body should only have body field: %+v", body)
			}
			conv := sampleConvention()
			conv.Body = *body.Body
			_ = json.NewEncoder(w).Encode(conv)
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
			_ = json.NewEncoder(w).Encode(sampleConvention())
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
			_ = json.NewEncoder(w).Encode(sampleConvention())
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
			_ = json.NewEncoder(w).Encode(sampleConvention())
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
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"convention_not_found"}`)
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
			_ = json.NewEncoder(w).Encode(sampleConvention())
		case http.MethodPatch:
			w.WriteHeader(http.StatusUnprocessableEntity)
			fmt.Fprint(w, `{"detail":"convention body exceeds preamble budget (estimated=900, budget=800)"}`)
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
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"convention_not_found"}`)
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

func TestBuildHistoryPath(t *testing.T) {
	if got := buildHistoryPath("vault-canonical"); got != "/api/v1/conventions/vault-canonical/history" {
		t.Fatalf("buildHistoryPath: got %q", got)
	}
}

func TestRunHistoryHappyPathRendersDiffs(t *testing.T) {
	bodyBefore := "Vault is canonical."
	entries := []HistoryEntry{
		{
			ID:           "h2",
			ConventionID: "11111111-1111-1111-1111-111111111111",
			BodyBefore:   &bodyBefore,
			BodyAfter:    "Vault is canonical.\nAdded rule.",
			ActorSub:     "ops-admin",
			Ts:           "2026-05-25T00:00:00Z",
		},
		{
			ID:           "h1",
			ConventionID: "11111111-1111-1111-1111-111111111111",
			BodyBefore:   nil,
			BodyAfter:    "Vault is canonical.",
			ActorSub:     "ops-admin",
			Ts:           "2026-05-24T00:00:00Z",
		},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/vault-canonical/history", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(entries)
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
	mkEntry := func(id string, ts string) HistoryEntry {
		return HistoryEntry{
			ID: id, BodyBefore: &bodyBefore, BodyAfter: "y", Ts: ts, ActorSub: "ops",
		}
	}
	entries := []HistoryEntry{
		mkEntry("h3", "2026-05-26T00:00:00Z"),
		mkEntry("h2", "2026-05-25T00:00:00Z"),
		mkEntry("h1", "2026-05-24T00:00:00Z"),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/x/history", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(entries)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runHistory(cmd, historyOptions{Slug: "x", Limit: 2, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runHistory --limit: %v; stderr=%s", err, stderr.String())
	}
	if strings.Contains(stdout.String(), "h1") {
		t.Errorf("--limit 2 included beyond-limit row: %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), "h2") {
		t.Errorf("--limit 2 dropped on-limit row: %q", stdout.String())
	}
}

func TestRunHistoryEmpty(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/x/history", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode([]HistoryEntry{})
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
	entries := []HistoryEntry{
		{ID: "h1", BodyBefore: &bodyBefore, BodyAfter: "y", Ts: "2026-05-24T00:00:00Z", ActorSub: "ops"},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/conventions/x/history", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(entries)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runHistory(cmd, historyOptions{Slug: "x", JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runHistory --json: %v", err)
	}
	var got []HistoryEntry
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("decode stdout JSON: %v; raw=%s", err, stdout.String())
	}
	if len(got) != 1 || got[0].ID != "h1" {
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
