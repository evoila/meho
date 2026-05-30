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

// TestRunPublishTemplateHappyPath — POST to .../publish carries the
// right `version` payload; 200 response renders the 1-line
// confirmation.
func TestRunPublishTemplateHappyPath(t *testing.T) {
	var sawBody api.UnderscoreVersionBody
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/vcenter-cert-rotation/publish", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &sawBody)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.PublishTemplateResponse{
			Slug: "vcenter-cert-rotation", Version: sawBody.Version, Status: "published",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runPublishTemplate(cmd, publishTemplateOptions{
		Slug: "vcenter-cert-rotation", Version: 3, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runPublishTemplate: %v; stderr=%s", err, stderr.String())
	}
	if sawBody.Version != 3 {
		t.Errorf("wire version: got %d; want 3", sawBody.Version)
	}
	if !strings.Contains(stdout.String(), "Published vcenter-cert-rotation@3") {
		t.Errorf("expected published line; got %q", stdout.String())
	}
}

// TestRunPublishTemplateRequiresVersion — missing --version fails
// fast.
func TestRunPublishTemplateRequiresVersion(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runPublishTemplate(cmd, publishTemplateOptions{Slug: "x"})
	if err == nil {
		t.Fatal("expected error for missing --version")
	}
	if !strings.Contains(stderr.String(), "--version") {
		t.Errorf("expected --version hint; got %q", stderr.String())
	}
}

// TestRunPublishTemplate404SurfacesSlugNotFound — slug_not_found
// surfaces the backend's detail.
func TestRunPublishTemplate404SurfacesSlugNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/missing/publish", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"slug_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runPublishTemplate(cmd, publishTemplateOptions{
		Slug: "missing", Version: 1, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "slug_not_found") {
		t.Errorf("expected detail; got %q", stderr.String())
	}
}

// TestRunPublishTemplate403SurfacesInsufficientRole — tenant_admin
// required.
func TestRunPublishTemplate403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x/publish", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runPublishTemplate(cmd, publishTemplateOptions{
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

// TestRunPublishTemplateNetworkError — connection refused → exit 3.
func TestRunPublishTemplateNetworkError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runPublishTemplate(cmd, publishTemplateOptions{
		Slug: "x", Version: 1, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "unreachable") {
		t.Errorf("expected unreachable; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 3 {
		t.Errorf("expected ExitCode 3; got %v", err)
	}
}

// TestRunPublishTemplateJSONHappyPath — --json emits the envelope.
func TestRunPublishTemplateJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates/x/publish", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.PublishTemplateResponse{
			Slug: "x", Version: 1, Status: "published",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runPublishTemplate(cmd, publishTemplateOptions{
		Slug: "x", Version: 1, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runPublishTemplate --json: %v", err)
	}
	var decoded api.PublishTemplateResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Status != "published" {
		t.Errorf("envelope.status: %q", decoded.Status)
	}
}
