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
	"time"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

// stubEntryID / stubTenantID are deterministic UUIDs used in every
// fixture so a test failure reads "11111111-..." in the request URL
// or response body. Easier to chase than a uuid.New() that rotates
// per run.
const (
	stubEntryID  = "11111111-1111-1111-1111-111111111111"
	stubTenantID = "22222222-2222-2222-2222-222222222222"
)

func mustParseUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	id, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("uuid.Parse(%q): %v", s, err)
	}
	return id
}

// newKbEntry constructs an api.KbEntry fixture with deterministic
// IDs + timestamps so handler responses survive `json.Marshal` and
// the verb's renderer formats them stably.
func newKbEntry(t *testing.T, slug, body string) api.KbEntry {
	t.Helper()
	createdAt := time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC)
	updatedAt := time.Date(2026, 5, 12, 10, 11, 12, 0, time.UTC)
	return api.KbEntry{
		Id:        mustParseUUID(t, stubEntryID),
		TenantId:  mustParseUUID(t, stubTenantID),
		Slug:      slug,
		Body:      body,
		Metadata:  map[string]any{},
		CreatedAt: createdAt,
		UpdatedAt: updatedAt,
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
// wraps the full entry. The handler echoes the typed `api.KbEntry`
// directly so the migration's "no consumer-side duplicate" property
// is the load-bearing claim under test.
func TestRunShowHappyPath(t *testing.T) {
	entry := newKbEntry(t, "vcenter-9.0", "# vcenter 9.0\n\nOverview body.")
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

// TestRunShowJSONHappyPath — --json wraps the full KbEntry shape.
func TestRunShowJSONHappyPath(t *testing.T) {
	entry := newKbEntry(t, "x", "b")
	entry.Metadata = map[string]any{"k": "v"}
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
	var decoded api.KbEntry
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
			printEntryBody(&buf, &api.KbEntry{Body: c.body})
			if buf.String() != c.want {
				t.Errorf("unexpected render: got %q; want %q", buf.String(), c.want)
			}
		})
	}
}

// TestPrintEntryBodyNilSafe pins the nil-guard: a nil entry is a
// no-op rather than a panic. Defensive against a renderer that
// somehow receives `nil` from runShow (the runner gates on
// StatusCode() == 200 + non-nil JSON200, but a regression that
// dropped the gate should surface here as a clean nothing rather
// than a runtime crash).
func TestPrintEntryBodyNilSafe(t *testing.T) {
	var buf bytes.Buffer
	printEntryBody(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("nil entry should write nothing; got %q", buf.String())
	}
}
