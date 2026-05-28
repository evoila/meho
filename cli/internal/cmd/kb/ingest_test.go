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

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunIngestRejectsEmptyDirectory — empty arg is caught.
func TestRunIngestRejectsEmptyDirectory(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runIngest(cmd, ingestOptions{Directory: ""}); err == nil {
		t.Fatalf("expected error for empty directory")
	} else if !strings.Contains(stderr.String(), "non-empty <directory>") {
		t.Errorf("expected hint; got %q", stderr.String())
	}
}

// TestRunIngestHappyPath — POSTs the right body and renders the
// four-bucket summary. The handler decodes the wire body into the
// generated `api.IngestKbRequest` so the migration's "no
// consumer-side ingestKbRequest" property is the load-bearing
// claim under test.
func TestRunIngestHappyPath(t *testing.T) {
	var bodyOnWire api.IngestKbRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/ingest", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.KbIngestionResult{
			InsertedCount: 5,
			UpdatedCount:  2,
			SkippedCount:  37,
			ErrorCount:    0,
			Errors:        []string{},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runIngest(cmd, ingestOptions{Directory: "/srv/kb", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runIngest: %v; stderr=%s", err, stderr.String())
	}
	if bodyOnWire.Directory == nil || *bodyOnWire.Directory != "/srv/kb" {
		t.Errorf("expected directory in request; got %+v", bodyOnWire)
	}
	if bodyOnWire.DryRun != nil && *bodyOnWire.DryRun {
		t.Errorf("dry_run should be omitted (or false) when not set; got %+v", *bodyOnWire.DryRun)
	}
	if bodyOnWire.TarballUrl != nil {
		t.Errorf("tarball_url should be nil; got %+v", *bodyOnWire.TarballUrl)
	}
	for _, want := range []string{"inserted:", "updated:", "skipped:", "errored:", "total:", "5", "2", "37", "44"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

// TestRunIngestDryRunBindsBody — --dry-run sets dry_run=true in
// the body.
func TestRunIngestDryRunBindsBody(t *testing.T) {
	var bodyOnWire api.IngestKbRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/ingest", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.KbIngestionResult{Errors: []string{}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runIngest(cmd, ingestOptions{Directory: "/srv/kb", DryRun: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runIngest --dry-run: %v", err)
	}
	if bodyOnWire.DryRun == nil || !*bodyOnWire.DryRun {
		t.Errorf("expected dry_run=true; got %+v", bodyOnWire)
	}
	if !strings.Contains(stdout.String(), "dry-run:") {
		t.Errorf("expected dry-run banner; got %q", stdout.String())
	}
}

// TestRunIngestJSONHappyPath — --json emits the raw result.
func TestRunIngestJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/ingest", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.KbIngestionResult{
			InsertedCount: 1, UpdatedCount: 2, SkippedCount: 3, ErrorCount: 0, Errors: []string{},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runIngest(cmd, ingestOptions{Directory: "/x", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runIngest --json: %v", err)
	}
	var decoded api.KbIngestionResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.InsertedCount != 1 || decoded.UpdatedCount != 2 {
		t.Errorf("decode produced %+v", decoded)
	}
}

// TestRunIngest400SurfacesDirectoryNotFound — 400 from
// directory_not_found / not_a_directory surfaces the substrate's
// detail verbatim.
func TestRunIngest400SurfacesDirectoryNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/ingest", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"detail":"directory_not_found: /no/such/path"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runIngest(cmd, ingestOptions{Directory: "/no/such/path", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "directory_not_found") {
		t.Errorf("expected substrate detail; got %q", stderr.String())
	}
}

// TestRunIngest403SurfacesInsufficientRole — operator-role JWT
// surfaces with the role hint.
func TestRunIngest403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/ingest", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runIngest(cmd, ingestOptions{Directory: "/srv/kb", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "tenant_admin required") {
		t.Errorf("expected role hint; got %q", stderr.String())
	}
}

// TestPrintIngestSummaryWithErrors — errors are appended one per
// line so partial-failure runs are visible without --json.
func TestPrintIngestSummaryWithErrors(t *testing.T) {
	var buf bytes.Buffer
	printIngestSummary(&buf, &api.KbIngestionResult{
		InsertedCount: 1, UpdatedCount: 0, SkippedCount: 0, ErrorCount: 2,
		Errors: []string{"foo.md: bad slug", "bar.md: unreadable"},
	}, false)
	out := buf.String()
	for _, want := range []string{"errors:", "foo.md: bad slug", "bar.md: unreadable", "total:", "3"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in %q", want, out)
		}
	}
}

// TestPrintIngestSummaryDryRunBanner — --dry-run emits a banner.
func TestPrintIngestSummaryDryRunBanner(t *testing.T) {
	var buf bytes.Buffer
	printIngestSummary(&buf, &api.KbIngestionResult{}, true)
	if !strings.Contains(buf.String(), "dry-run") {
		t.Errorf("expected dry-run banner; got %q", buf.String())
	}
}

// TestRunIngestRejects200WithoutJSONPayload pins the JSON200
// nil-guard (M4). A 200 with a missing or mistyped Content-Type
// leaves resp.JSON200 nil; without the guard, printIngestSummary
// silently no-ops on nil — phantom success. Route to
// output.Unexpected (exit 4) instead.
func TestRunIngestRejects200WithoutJSONPayload(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/ingest", func(w http.ResponseWriter, _ *http.Request) {
		// Deliberately omit Content-Type so the generated parser
		// leaves JSON200 nil.
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not-json"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runIngest(cmd, ingestOptions{
		Directory: "/srv/kb", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 200 without JSON payload")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without an ingestion result payload") {
		t.Errorf("expected detail mentioning missing payload; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}
