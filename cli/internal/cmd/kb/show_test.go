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

// TestBuildShowPathEscapesSlug — dots / hyphens pass through;
// space-like operator typos surface as %20 so the route's
// `{slug:str}` matcher sees a single segment.
func TestBuildShowPathEscapesSlug(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"vcenter-9.0", "/api/v1/kb/vcenter-9.0"},
		{"a", "/api/v1/kb/a"},
		{"slug with space", "/api/v1/kb/slug%20with%20space"},
	}
	for _, c := range cases {
		if got := buildShowPath(c.in); got != c.want {
			t.Errorf("buildShowPath(%q): got %q; want %q", c.in, got, c.want)
		}
	}
}

// TestRunShowRejectsEmptySlug — args[0] of empty string must fail
// at the runner before hitting the backplane.
func TestRunShowRejectsEmptySlug(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{Slug: ""})
	if err == nil {
		t.Fatalf("expected error for empty slug")
	}
	if !strings.Contains(stderr.String(), "non-empty <slug>") {
		t.Errorf("expected slug hint in stderr; got %q", stderr.String())
	}
}

// TestRunShowHappyPath — body goes to stdout verbatim; --json
// wraps the full entry.
func TestRunShowHappyPath(t *testing.T) {
	entry := Entry{
		ID:        "00000000-0000-0000-0000-000000000001",
		TenantID:  "00000000-0000-0000-0000-000000000002",
		Slug:      "vcenter-9.0",
		Body:      "# vcenter 9.0\n\nOverview body.",
		Metadata:  map[string]any{},
		CreatedAt: "2026-05-01T00:00:00Z",
		UpdatedAt: "2026-05-12T10:11:12Z",
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/vcenter-9.0") {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(entry)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{Slug: "vcenter-9.0", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "# vcenter 9.0") {
		t.Errorf("expected Markdown body in stdout; got %q", stdout.String())
	}
	// The non-JSON mode must NOT emit JSON.
	if strings.Contains(stdout.String(), "tenant_id") {
		t.Errorf("non-JSON mode leaked envelope: %q", stdout.String())
	}
}

// TestRunShowJSONHappyPath — --json wraps the full Entry shape.
func TestRunShowJSONHappyPath(t *testing.T) {
	entry := Entry{
		ID:   "00000000-0000-0000-0000-000000000001",
		Slug: "x", Body: "b",
		Metadata: map[string]any{"k": "v"},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(entry)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{Slug: "x", JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runShow --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded Entry
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout.String())
	}
	if decoded.Slug != "x" || decoded.Body != "b" {
		t.Errorf("decode produced %+v", decoded)
	}
}

// TestRunShow404SurfacesSlugNotFound — the substrate returns
// `slug_not_found` for both cross-tenant probes and genuine
// absences; the CLI must render it as an unexpected_response with
// the backend's detail intact.
func TestRunShow404SurfacesSlugNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"slug_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{Slug: "missing", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "slug_not_found") {
		t.Errorf("expected slug_not_found detail; got %q", stderr.String())
	}
}

// TestPrintEntryBodyEmitsBodyAndNewline — non-JSON render writes
// the body then a single trailing newline. Bodies with no trailing
// newline get exactly one appended; bodies that already end in
// `\n` or `\r\n` get normalised down to exactly one trailing `\n`
// rather than doubling up.
func TestPrintEntryBodyEmitsBodyAndNewline(t *testing.T) {
	cases := []struct {
		name, body, want string
	}{
		{"no trailing newline", "hello", "hello\n"},
		{"already ends in LF", "hello\n", "hello\n"},
		{"already ends in CRLF", "hello\r\n", "hello\n"},
		{"multiple trailing newlines", "hello\n\n\n", "hello\n"},
		{"empty body", "", "\n"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var buf bytes.Buffer
			printEntryBody(&buf, &Entry{Body: c.body})
			if buf.String() != c.want {
				t.Errorf("unexpected render: got %q; want %q", buf.String(), c.want)
			}
		})
	}
}
