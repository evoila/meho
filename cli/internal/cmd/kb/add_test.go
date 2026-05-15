// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestRunAddRejectsEmptySlug — empty slug short-circuits before the
// body parsing path so the operator sees the slug hint first.
func TestRunAddRejectsEmptySlug(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runAdd(cmd, addOptions{Slug: "", BodyArg: "x"})
	if err == nil {
		t.Fatalf("expected error for empty slug")
	}
	if !strings.Contains(stderr.String(), "non-empty <slug>") {
		t.Errorf("expected slug hint; got %q", stderr.String())
	}
}

// TestRunAddRejectsEmptyBody — empty --body is caught at the runner.
func TestRunAddRejectsEmptyBody(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{Slug: "x", BodyArg: ""})
	if err == nil {
		t.Fatalf("expected error for empty body")
	}
	if !strings.Contains(stderr.String(), "--body") {
		t.Errorf("expected --body hint; got %q", stderr.String())
	}
}

// TestRunAddRejectsBadMetadata — malformed --metadata surfaces a
// CLI-side error rather than a 422.
func TestRunAddRejectsBadMetadata(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{Slug: "x", BodyArg: "body", MetadataArg: "missing-equals"})
	if err == nil {
		t.Fatalf("expected error for malformed --metadata")
	}
	if !strings.Contains(stderr.String(), "metadata") {
		t.Errorf("expected metadata hint; got %q", stderr.String())
	}
}

// TestRunAddHappyPath — the runner POSTs the right body and renders
// the success summary.
func TestRunAddHappyPath(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{
			ID:        "00000000-0000-0000-0000-000000000001",
			TenantID:  "00000000-0000-0000-0000-000000000002",
			Slug:      "new-slug",
			Body:      "body content",
			Metadata:  map[string]any{"owner": "ops"},
			CreatedAt: "2026-05-15T00:00:00Z",
			UpdatedAt: "2026-05-15T00:00:00Z",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{
		Slug:              "new-slug",
		BodyArg:           "body content",
		MetadataArg:       "owner=ops",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAdd: %v; stderr=%s", err, stderr.String())
	}
	if got := bodyJSON["slug"]; got != "new-slug" {
		t.Errorf("expected slug in body; got %+v", bodyJSON)
	}
	if got := bodyJSON["body"]; got != "body content" {
		t.Errorf("expected body in request; got %+v", bodyJSON)
	}
	md, ok := bodyJSON["metadata"].(map[string]any)
	if !ok {
		t.Fatalf("expected metadata map; got %T", bodyJSON["metadata"])
	}
	if md["owner"] != "ops" {
		t.Errorf("expected owner=ops in metadata; got %+v", md)
	}
	if !strings.Contains(stdout.String(), "created kb entry") {
		t.Errorf("expected success line; got %q", stdout.String())
	}
}

// TestRunAddOmitsMetadataWhenAbsent — without --metadata the JSON
// body must not carry a metadata field (so the backend's default {}
// applies).
func TestRunAddOmitsMetadataWhenAbsent(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{Slug: "x"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runAdd(cmd, addOptions{Slug: "x", BodyArg: "b", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runAdd: %v", err)
	}
	if _, ok := bodyJSON["metadata"]; ok {
		t.Errorf("expected metadata absent; got %+v", bodyJSON)
	}
}

// TestRunAddJSONHappyPath — --json emits the round-tripped entry.
func TestRunAddJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{Slug: "x", Body: "y"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runAdd(cmd, addOptions{Slug: "x", BodyArg: "y", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runAdd --json: %v", err)
	}
	var decoded Entry
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Slug != "x" || decoded.Body != "y" {
		t.Errorf("decode: %+v", decoded)
	}
}

// TestRunAdd403SurfacesInsufficientRole — operator-role JWT lands
// as 403; the CLI must classify it as insufficient_role exit 5.
func TestRunAdd403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{Slug: "x", BodyArg: "y", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "tenant_admin required") {
		t.Errorf("expected required-role hint; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunAdd422SurfacesValidationDetail — 422 from invalid_slug
// must include the backend's detail string so operators see why.
func TestRunAdd422SurfacesValidationDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":"slug 'BAD' does not match SLUG_PATTERN"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{Slug: "BAD", BodyArg: "y", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("expected invalid request hint; got %q", stderr.String())
	}
	// Tighten the assertion: the CLI's value-add over a raw curl is
	// surfacing the backend's `detail` payload so operators see *what*
	// was wrong (which field, which pattern). A regression that
	// swallows the detail would still pass the "invalid request"
	// check alone — assert the substrate's pattern-mismatch string
	// survives the round-trip into stderr.
	if !strings.Contains(stderr.String(), "SLUG_PATTERN") {
		t.Errorf("expected backend detail to survive into stderr; got %q", stderr.String())
	}
}

// TestRunAddReadsBodyFromStdin — --body @- pipes content through
// cmd.InOrStdin().
func TestRunAddReadsBodyFromStdin(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &bodyJSON)
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{Slug: "x", Body: "from stdin"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("from stdin\n"))
	if err := runAdd(cmd, addOptions{Slug: "x", BodyArg: "@-", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runAdd: %v", err)
	}
	if got := bodyJSON["body"]; got != "from stdin" {
		t.Errorf("expected stdin body; got %+v", bodyJSON)
	}
}

// TestAddCmdHelpMentionsBodyAndMetadata — the cobra help text must
// surface --body and --metadata so operators discover the inputs.
func TestAddCmdHelpMentionsBodyAndMetadata(t *testing.T) {
	cmd := newAddCmd()
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	cmd.SetErr(&buf)
	cmd.SetArgs([]string{"--help"})
	// --help short-circuits with err=nil per cobra's conventions.
	if err := cmd.Execute(); err != nil {
		t.Fatalf("help: %v", err)
	}
	// Case-insensitive checks — cobra renders "Tenant_admin only —
	// operator-role JWT lands as 403"; the comparison reads through
	// strings.ToLower so the test doesn't pin to the description's
	// chosen capitalisation.
	lower := strings.ToLower(buf.String())
	for _, want := range []string{"--body", "--metadata", "tenant_admin"} {
		if !strings.Contains(lower, want) {
			t.Errorf("expected help to mention %q; got %q", want, buf.String())
		}
	}
}
