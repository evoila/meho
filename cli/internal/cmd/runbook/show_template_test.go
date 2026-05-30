// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// buildShowResponse synthesises a wire ShowTemplateResponse with two
// steps (manual + operation_call) to mirror the validYAML fixture. The
// generated step union types require explicit From*Step setters; this
// helper hides that ceremony from the test bodies.
func buildShowResponse(t *testing.T, slug string, version int, status api.ShowTemplateResponseStatus) api.ShowTemplateResponse {
	t.Helper()
	var step0 api.ShowTemplateResponse_Steps_Item
	var step0Verify api.ManualStep_Verify
	if err := step0Verify.FromConfirmVerify(api.ConfirmVerify{Type: "confirm", Prompt: "Did it work?"}); err != nil {
		t.Fatalf("step0 verify: %v", err)
	}
	if err := step0.FromManualStep(api.ManualStep{
		Id: "revoke-old-cert", Title: "Revoke", Body: "Run revoke-cert ${run.target}.",
		Type: "manual", Verify: step0Verify,
	}); err != nil {
		t.Fatalf("step0: %v", err)
	}

	var step1 api.ShowTemplateResponse_Steps_Item
	var step1Verify api.OperationCallStep_Verify
	if err := step1Verify.FromOperationCallVerify(api.OperationCallVerify{
		Type: "operation_call", OpId: "vault.pki.cert",
		Params: map[string]interface{}{"serial": "abc"},
		Expect: map[string]interface{}{"revoked": false},
	}); err != nil {
		t.Fatalf("step1 verify: %v", err)
	}
	if err := step1.FromOperationCallStep(api.OperationCallStep{
		Id: "issue-new-cert", Title: "Issue", Body: "Dispatching.",
		Type: "operation_call", OpId: "vault.pki.issue",
		Params: map[string]interface{}{"common_name": "x"},
		Verify: step1Verify,
	}); err != nil {
		t.Fatalf("step1: %v", err)
	}

	tk := "vmware-rest"
	return api.ShowTemplateResponse{
		Slug: slug, Version: version,
		Title: "T", Description: "First line\nSecond line", TargetKind: &tk,
		Status:    status,
		Steps:     []api.ShowTemplateResponse_Steps_Item{step0, step1},
		CreatedBy: "alice", CreatedAt: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		EditedBy: "bob", EditedAt: time.Date(2026, 5, 30, 12, 0, 0, 0, time.UTC),
	}
}

// TestRunShowTemplateHappyPath — GET on the right path; body renders
// title, status, target_kind, description, and a numbered step list
// with verify summary.
func TestRunShowTemplateHappyPath(t *testing.T) {
	var rawQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/vcenter-cert-rotation", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET; got %s", r.Method)
		}
		rawQuery = r.URL.RawQuery
		resp := buildShowResponse(t, "vcenter-cert-rotation", 3, api.ShowTemplateResponseStatusPublished)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(resp)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runShowTemplate(cmd, showTemplateOptions{
		Slug: "vcenter-cert-rotation", Version: 3, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runShowTemplate: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(rawQuery, "version=3") {
		t.Errorf("expected version=3 in query; got %q", rawQuery)
	}
	out := stdout.String()
	for _, want := range []string{
		"Template: vcenter-cert-rotation@3",
		"Status:      published",
		"Target kind: vmware-rest",
		"First line",
		"Second line",
		"[manual]",
		"[operation_call]",
		"verify: confirm",
		"verify: operation_call op_id=vault.pki.cert",
		"revoke-old-cert",
		"issue-new-cert",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected stdout to contain %q; got:\n%s", want, out)
		}
	}
}

// TestRunShowTemplateJSONHappyPath — --json emits the raw envelope.
func TestRunShowTemplateJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x", func(w http.ResponseWriter, _ *http.Request) {
		resp := buildShowResponse(t, "x", 1, api.ShowTemplateResponseStatusDraft)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(resp)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runShowTemplate(cmd, showTemplateOptions{Slug: "x", JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runShowTemplate --json: %v", err)
	}
	var decoded api.ShowTemplateResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Slug != "x" || decoded.Version != 1 {
		t.Errorf("envelope: slug=%q version=%d", decoded.Slug, decoded.Version)
	}
}

// TestRunShowTemplate404SurfacesSlugNotFound — slug_not_found is the
// substrate's shape for both genuine absence and cross-tenant probes.
func TestRunShowTemplate404SurfacesSlugNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/missing", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"slug_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShowTemplate(cmd, showTemplateOptions{Slug: "missing", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatal("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "slug_not_found") {
		t.Errorf("expected slug_not_found in stderr; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}

// TestRunShowTemplate403SurfacesInsufficientRole — opacity_floor /
// no-carve-out paths land as 403; CLI must classify exit 5.
func TestRunShowTemplate403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/locked", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required (post-completion exception not satisfied)"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShowTemplate(cmd, showTemplateOptions{Slug: "locked", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatal("expected error on 403")
	}
	if !strings.Contains(stderr.String(), "post-completion") {
		t.Errorf("expected backend detail in stderr; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunShowTemplateRejectsEmptySlug — empty positional arg.
func TestRunShowTemplateRejectsEmptySlug(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runShowTemplate(cmd, showTemplateOptions{Slug: ""})
	if err == nil {
		t.Fatal("expected error for empty slug")
	}
	if !strings.Contains(stderr.String(), "non-empty <slug>") {
		t.Errorf("expected slug hint; got %q", stderr.String())
	}
}

// TestRunShowTemplateRejectsNegativeVersion — --version must be
// non-negative.
func TestRunShowTemplateRejectsNegativeVersion(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runShowTemplate(cmd, showTemplateOptions{Slug: "x", Version: -1})
	if err == nil {
		t.Fatal("expected error for negative --version")
	}
	if !strings.Contains(stderr.String(), "non-negative") {
		t.Errorf("expected non-negative hint; got %q", stderr.String())
	}
}
