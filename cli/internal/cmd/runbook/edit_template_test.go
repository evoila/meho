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

// TestRunEditTemplateHappyPathDraftInPlace — edit in place when only
// a draft exists: response has forked_from == nil and the summary
// is the single-line shape.
func TestRunEditTemplateHappyPathDraftInPlace(t *testing.T) {
	var sawBody api.RunbookTemplateBody
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/vcenter-cert-rotation", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPatch {
			t.Errorf("expected PATCH; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &sawBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.EditTemplateResponse{
			Slug: "vcenter-cert-rotation", Version: 1, Status: "draft", ForkedFrom: nil,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, stdout, _ := newRunCmd(t)
	err := runEditTemplate(cmd, editTemplateOptions{
		Slug: "vcenter-cert-rotation", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEditTemplate: %v", err)
	}
	if sawBody.Title == "" || len(sawBody.Steps) != 2 {
		t.Errorf("wire body: %+v", sawBody)
	}
	out := stdout.String()
	if !strings.Contains(out, "Edited vcenter-cert-rotation@1") {
		t.Errorf("expected edited line; got %q", out)
	}
	if strings.Contains(out, "forked from") {
		t.Errorf("expected no fork notice for in-place edit; got %q", out)
	}
}

// TestRunEditTemplateHappyPathForkOnEdit — editing a published-only
// template forks to a new draft and the summary surfaces
// `forked_from.in_flight_run_count`.
func TestRunEditTemplateHappyPathForkOnEdit(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/vcenter-cert-rotation", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.EditTemplateResponse{
			Slug: "vcenter-cert-rotation", Version: 2, Status: "draft",
			ForkedFrom: &api.ForkInfo{Slug: "vcenter-cert-rotation", Version: 1, InFlightRunCount: 3},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, stdout, _ := newRunCmd(t)
	err := runEditTemplate(cmd, editTemplateOptions{
		Slug: "vcenter-cert-rotation", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEditTemplate fork: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, "Edited vcenter-cert-rotation@2") {
		t.Errorf("expected edited line; got %q", out)
	}
	if !strings.Contains(out, "forked from vcenter-cert-rotation@1") {
		t.Errorf("expected fork notice; got %q", out)
	}
	if !strings.Contains(out, "3 in-flight") {
		t.Errorf("expected in-flight count; got %q", out)
	}
}

// TestRunEditTemplateRequiresFromFlag — missing --from short-circuits.
func TestRunEditTemplateRequiresFromFlag(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runEditTemplate(cmd, editTemplateOptions{Slug: "x"})
	if err == nil {
		t.Fatal("expected error for missing --from")
	}
	if !strings.Contains(stderr.String(), "--from") {
		t.Errorf("expected --from hint; got %q", stderr.String())
	}
}

// TestRunEditTemplate404SurfacesSlugNotFound — slug_not_found surfaces
// the backend's detail.
func TestRunEditTemplate404SurfacesSlugNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/missing", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"slug_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, _, stderr := newRunCmd(t)
	err := runEditTemplate(cmd, editTemplateOptions{
		Slug: "missing", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "slug_not_found") {
		t.Errorf("expected detail; got %q", stderr.String())
	}
}

// TestRunEditTemplate403SurfacesInsufficientRole — tenant_admin
// required.
func TestRunEditTemplate403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, _, stderr := newRunCmd(t)
	err := runEditTemplate(cmd, editTemplateOptions{
		Slug: "x", FromPath: path, BackplaneOverride: srv.URL,
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

// TestRunEditTemplate422SurfacesValidationDetail — backend 422 round-trips.
func TestRunEditTemplate422SurfacesValidationDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":[{"loc":["body","body","steps",0,"verify"],"msg":"value does not match discriminator type"}]}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, _, stderr := newRunCmd(t)
	err := runEditTemplate(cmd, editTemplateOptions{
		Slug: "x", FromPath: path, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "discriminator type") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

// TestRunEditTemplateJSONHappyPath — --json emits the raw envelope
// including forked_from when set.
func TestRunEditTemplateJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.EditTemplateResponse{
			Slug: "x", Version: 2, Status: "draft",
			ForkedFrom: &api.ForkInfo{Slug: "x", Version: 1, InFlightRunCount: 7},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	path := writeYAML(t, validYAML)
	cmd, stdout, _ := newRunCmd(t)
	err := runEditTemplate(cmd, editTemplateOptions{
		Slug: "x", FromPath: path, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEditTemplate --json: %v", err)
	}
	var decoded api.EditTemplateResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.ForkedFrom == nil || decoded.ForkedFrom.InFlightRunCount != 7 {
		t.Errorf("envelope.ForkedFrom: %+v", decoded.ForkedFrom)
	}
}
