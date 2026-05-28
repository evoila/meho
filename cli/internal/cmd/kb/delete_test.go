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

// TestRunDeleteRejectsEmptySlug — args[0] empty is caught.
func TestRunDeleteRejectsEmptySlug(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{Slug: ""}); err == nil {
		t.Fatalf("expected error for empty slug")
	} else if !strings.Contains(stderr.String(), "non-empty <slug>") {
		t.Errorf("expected slug hint; got %q", stderr.String())
	}
}

// TestRunDeleteDeclinedWithoutLoginConfig — the docstring's "ask
// before doing destructive things" promise must hold even when the
// operator has not yet run `meho login`. Without a backplane URL
// configured, resolveBackplane would surface auth_expired; the
// runner must reach the prompt first so a decline exits 0 with
// status=declined and no backplane call is made.
func TestRunDeleteDeclinedWithoutLoginConfig(t *testing.T) {
	// Per-test XDG dir without seeding any config / token. This
	// mirrors a fresh workstation where no `meho login` has run yet.
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("n\n"))
	err := runDelete(cmd, deleteOptions{Slug: "x"})
	if err != nil {
		t.Fatalf("decline without login should exit 0; got err=%v stderr=%q", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "declined") {
		t.Errorf("expected declined line; got %q", stdout.String())
	}
	if strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("decline should not surface auth_expired; got stderr=%q", stderr.String())
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
// surfacing a not-found error. The typed-client carries the 204
// in `resp.StatusCode()`; the runner gates on `== 204` rather
// than the pre-migration `httpError` branch on non-2xx.
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

// TestRunDeleteEscapesSlugOnWire confirms the typed-client embeds
// the slug into `/api/v1/kb/{slug}` with proper URL escaping. The
// pre-migration test pinned this via the local buildDeletePath
// helper; the equivalent post-migration is the path the mock
// observes when the generated DeleteKbApiV1KbSlugDeleteWithResponse
// dispatches.
func TestRunDeleteEscapesSlugOnWire(t *testing.T) {
	var seenPath string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, r *http.Request) {
		seenPath = r.URL.Path
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runDelete(cmd, deleteOptions{
		Slug: "vcenter-9.0", Confirm: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runDelete: %v", err)
	}
	if seenPath != "/api/v1/kb/vcenter-9.0" {
		t.Errorf("path: got %q; want %q", seenPath, "/api/v1/kb/vcenter-9.0")
	}
}
