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

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunDraftTemplateHappyPath — happy path: YAML loads, pre-flight
// passes, POST to /api/v1/runbooks/templates carries the right
// payload, 201 response renders the 2-line summary.
func TestRunDraftTemplateHappyPath(t *testing.T) {
	var sawBody api.DraftTemplateRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &sawBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(api.DraftTemplateResponse{
			Slug: sawBody.Slug, Version: 1, Status: "draft",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, stdout, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "vcenter-cert-rotation", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDraftTemplate: %v; stderr=%s", err, stderr.String())
	}
	if sawBody.Slug != "vcenter-cert-rotation" {
		t.Errorf("wire slug: %q", sawBody.Slug)
	}
	if sawBody.Body.Title == "" || len(sawBody.Body.Steps) != 2 {
		t.Errorf("wire body steps: %d title=%q", len(sawBody.Body.Steps), sawBody.Body.Title)
	}
	out := stdout.String()
	if !strings.Contains(out, "Created draft vcenter-cert-rotation@1") {
		t.Errorf("expected created-draft line; got %q", out)
	}
	if !strings.Contains(out, "Status: draft") {
		t.Errorf("expected status line; got %q", out)
	}
}

// TestRunDraftTemplateJSONHappyPath — --json emits the raw envelope.
func TestRunDraftTemplateJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(api.DraftTemplateResponse{
			Slug: "x", Version: 1, Status: "draft",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, stdout, _ := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "vcenter-cert-rotation", FromPath: path, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDraftTemplate --json: %v", err)
	}
	var decoded api.DraftTemplateResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Slug != "x" || decoded.Status != "draft" {
		t.Errorf("envelope: %+v", decoded)
	}
}

// TestRunDraftTemplateRequiresFromFlag — missing --from short-circuits
// before any HTTP call.
func TestRunDraftTemplateRequiresFromFlag(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{Slug: "x"})
	if err == nil {
		t.Fatal("expected error for missing --from")
	}
	if !strings.Contains(stderr.String(), "--from") {
		t.Errorf("expected --from hint; got %q", stderr.String())
	}
}

// TestRunDraftTemplateRejectsBadSlugBeforeHTTP — invalid slug fails
// pre-flight; no HTTP call made (AC test 6).
func TestRunDraftTemplateRejectsBadSlugBeforeHTTP(t *testing.T) {
	httpCalls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		httpCalls++
		w.WriteHeader(http.StatusInternalServerError)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "BAD_SLUG", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error for bad slug")
	}
	if httpCalls != 0 {
		t.Errorf("expected zero HTTP calls; got %d", httpCalls)
	}
	if !strings.Contains(stderr.String(), "does not match") {
		t.Errorf("expected slug-regex hint; got %q", stderr.String())
	}
}

// TestRunDraftTemplateRejectsDuplicateStepIDBeforeHTTP — duplicate
// step ids fail pre-flight; no HTTP call (AC test 7).
func TestRunDraftTemplateRejectsDuplicateStepIDBeforeHTTP(t *testing.T) {
	httpCalls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(_ http.ResponseWriter, _ *http.Request) {
		httpCalls++
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	bad := strings.Replace(validYAML, "issue-new-cert", "revoke-old-cert", 1)
	path := writeYAML(t, bad)
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "ok-slug", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error for duplicate step id")
	}
	if httpCalls != 0 {
		t.Errorf("expected zero HTTP calls; got %d", httpCalls)
	}
	if !strings.Contains(stderr.String(), "duplicate step id") {
		t.Errorf("expected dup-id hint; got %q", stderr.String())
	}
}

// TestRunDraftTemplateRejectsBadSubstitutionBeforeHTTP — disallowed
// `${...}` patterns fail pre-flight; no HTTP call (AC test 8).
func TestRunDraftTemplateRejectsBadSubstitutionBeforeHTTP(t *testing.T) {
	httpCalls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(_ http.ResponseWriter, _ *http.Request) {
		httpCalls++
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	bad := strings.Replace(validYAML, "${run.target}", "${run.bad.path}", 1)
	path := writeYAML(t, bad)
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "ok-slug", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error for disallowed substitution")
	}
	if httpCalls != 0 {
		t.Errorf("expected zero HTTP calls; got %d", httpCalls)
	}
	if !strings.Contains(stderr.String(), "disallowed substitution") {
		t.Errorf("expected substitution-allowlist hint; got %q", stderr.String())
	}
}

// TestRunDraftTemplateRejectsBadYAMLBeforeHTTP — malformed YAML fails
// at parse time with a `line N:` hint; no HTTP call (AC test 9).
func TestRunDraftTemplateRejectsBadYAMLBeforeHTTP(t *testing.T) {
	httpCalls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(_ http.ResponseWriter, _ *http.Request) {
		httpCalls++
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	// Make the YAML decoder unhappy — start a flow scalar with `{`
	// that never closes. yaml.v3 surfaces this with a clear
	// `line N:` parse error.
	bad := "title: { unterminated flow\n"
	path := writeYAML(t, bad)
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "ok-slug", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error for bad YAML")
	}
	if httpCalls != 0 {
		t.Errorf("expected zero HTTP calls; got %d", httpCalls)
	}
	if !strings.Contains(stderr.String(), "line") {
		t.Errorf("expected line: hint in stderr; got %q", stderr.String())
	}
}

// TestRunDraftTemplate403SurfacesInsufficientRole — operator-role
// JWT lands 403; CLI exits 5.
func TestRunDraftTemplate403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "vcenter-cert-rotation", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "tenant_admin required") {
		t.Errorf("expected role hint; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunDraftTemplate409SurfacesDraftAlreadyExists — POST against
// an existing draft surfaces the backend's 409 detail.
func TestRunDraftTemplate409SurfacesDraftAlreadyExists(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"detail":"draft_already_exists"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "vcenter-cert-rotation", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "draft_already_exists") {
		t.Errorf("expected detail; got %q", stderr.String())
	}
}

// TestRunDraftTemplate422SurfacesValidationDetail — backend's 422
// envelope (with `loc` path) survives the round-trip.
func TestRunDraftTemplate422SurfacesValidationDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":[{"loc":["body","slug"],"msg":"value does not match SLUG_PATTERN"}]}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, _, stderr := newRunCmd(t)
	err := runDraftTemplate(cmd, draftTemplateOptions{
		Slug: "vcenter-cert-rotation", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "SLUG_PATTERN") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}
