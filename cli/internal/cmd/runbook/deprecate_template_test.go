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

// TestRunDeprecateTemplateHappyPath — POST to .../deprecate carries
// the right version body and renders the 1-line confirmation.
func TestRunDeprecateTemplateHappyPath(t *testing.T) {
	var sawBody api.UnderscoreVersionBody
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/vcenter-cert-rotation/deprecate", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &sawBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.DeprecateTemplateResponse{
			Slug: "vcenter-cert-rotation", Version: sawBody.Version, Status: "deprecated",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runDeprecateTemplate(cmd, deprecateTemplateOptions{
		Slug: "vcenter-cert-rotation", Version: 2, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDeprecateTemplate: %v; stderr=%s", err, stderr.String())
	}
	if sawBody.Version != 2 {
		t.Errorf("wire version: got %d; want 2", sawBody.Version)
	}
	if !strings.Contains(stdout.String(), "Deprecated vcenter-cert-rotation@2") {
		t.Errorf("expected deprecated line; got %q", stdout.String())
	}
}

// TestRunDeprecateTemplateRequiresVersion — missing --version fails
// fast.
func TestRunDeprecateTemplateRequiresVersion(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runDeprecateTemplate(cmd, deprecateTemplateOptions{Slug: "x"})
	if err == nil {
		t.Fatal("expected error for missing --version")
	}
	if !strings.Contains(stderr.String(), "--version") {
		t.Errorf("expected --version hint; got %q", stderr.String())
	}
}

// TestRunDeprecateTemplate404SurfacesSlugNotFound — slug_not_found.
func TestRunDeprecateTemplate404SurfacesSlugNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/missing/deprecate", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"slug_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runDeprecateTemplate(cmd, deprecateTemplateOptions{
		Slug: "missing", Version: 1, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "slug_not_found") {
		t.Errorf("expected detail; got %q", stderr.String())
	}
}

// TestRunDeprecateTemplate403SurfacesInsufficientRole — tenant_admin
// required.
func TestRunDeprecateTemplate403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x/deprecate", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runDeprecateTemplate(cmd, deprecateTemplateOptions{
		Slug: "x", Version: 1, BackplaneOverride: srv.URL,
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

// TestRunDeprecateTemplate400OnDraftSurfacesDetail — backend's 400
// for "cannot deprecate a draft" surfaces verbatim.
func TestRunDeprecateTemplate400OnDraftSurfacesDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x/deprecate", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"detail":"cannot deprecate a draft; publish it first"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runDeprecateTemplate(cmd, deprecateTemplateOptions{
		Slug: "x", Version: 1, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "publish it first") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

// TestRunDeprecateTemplateJSONHappyPath — --json emits the envelope.
func TestRunDeprecateTemplateJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x/deprecate", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.DeprecateTemplateResponse{
			Slug: "x", Version: 1, Status: "deprecated",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runDeprecateTemplate(cmd, deprecateTemplateOptions{
		Slug: "x", Version: 1, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDeprecateTemplate --json: %v", err)
	}
	var decoded api.DeprecateTemplateResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Status != "deprecated" {
		t.Errorf("envelope.status: %q", decoded.Status)
	}
}
