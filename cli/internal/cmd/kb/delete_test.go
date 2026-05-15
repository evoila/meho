// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestBuildDeletePathEscapesSlug — path encoding follows the same
// contract as buildShowPath.
func TestBuildDeletePathEscapesSlug(t *testing.T) {
	if got := buildDeletePath("vcenter-9.0"); got != "/api/v1/kb/vcenter-9.0" {
		t.Errorf("buildDeletePath: got %q", got)
	}
	if got := buildDeletePath("a b"); got != "/api/v1/kb/a%20b" {
		t.Errorf("buildDeletePath space: got %q", got)
	}
}

// TestRunDeleteRejectsEmptySlug — args[0] empty is caught.
func TestRunDeleteRejectsEmptySlug(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{Slug: ""}); err == nil {
		t.Fatalf("expected error for empty slug")
	} else if !strings.Contains(stderr.String(), "non-empty <slug>") {
		t.Errorf("expected slug hint; got %q", stderr.String())
	}
}

// TestRunDeletePromptDeclined — when --confirm is not set and the
// operator answers "n", the runner must NOT call the backplane.
func TestRunDeletePromptDeclined(t *testing.T) {
	called := false
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(_ http.ResponseWriter, _ *http.Request) {
		called = true
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("n\n"))
	err := runDelete(cmd, deleteOptions{Slug: "x", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runDelete declined: %v", err)
	}
	if called {
		t.Errorf("decline should not call backend; but DELETE was issued")
	}
	if !strings.Contains(stdout.String(), "declined") {
		t.Errorf("expected declined line; got %q", stdout.String())
	}
}

// TestRunDeleteWithConfirmSkipsPrompt — --confirm skips the prompt
// and calls DELETE directly.
func TestRunDeleteWithConfirmSkipsPrompt(t *testing.T) {
	method := ""
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	// EOF stdin — if --confirm were ignored, confirmPrompt would
	// return false and the backend wouldn't be called.
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runDelete(cmd, deleteOptions{Slug: "x", Confirm: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runDelete --confirm: %v", err)
	}
	if method != http.MethodDelete {
		t.Errorf("expected DELETE; got %s", method)
	}
	if !strings.Contains(stdout.String(), "deleted") {
		t.Errorf("expected deleted line; got %q", stdout.String())
	}
}

// TestRunDeleteIdempotent204 — the substrate returns 204 on a
// missing slug; the CLI must surface that as success without
// surfacing a not-found error.
func TestRunDeleteIdempotent204(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("y\n"))
	err := runDelete(cmd, deleteOptions{Slug: "ghost", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runDelete: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "deleted") {
		t.Errorf("expected deleted line; got %q", stdout.String())
	}
}

// TestRunDeleteJSONHappyPath — --json emits the structured envelope.
func TestRunDeleteJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runDelete(cmd, deleteOptions{
		Slug: "x", Confirm: true, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDelete --json: %v", err)
	}
	var decoded deleteResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Status != "deleted" || decoded.Slug != "x" {
		t.Errorf("--json: %+v", decoded)
	}
}

// TestRunDelete403SurfacesInsufficientRole — operator-role JWT
// surfaces with the required-role detail.
func TestRunDelete403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runDelete(cmd, deleteOptions{Slug: "x", Confirm: true, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "tenant_admin required") {
		t.Errorf("expected role hint; got %q", stderr.String())
	}
}
