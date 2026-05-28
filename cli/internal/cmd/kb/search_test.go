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
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunSearchRejectsEmptyQuery — empty <query> arg is caught.
func TestRunSearchRejectsEmptyQuery(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{Query: ""}); err == nil {
		t.Fatalf("expected error for empty query")
	} else if !strings.Contains(stderr.String(), "non-empty <query>") {
		t.Errorf("expected query hint; got %q", stderr.String())
	}
}

// TestRunSearchRejectsOutOfRangeLimit — --limit > 50 is rejected
// before the round-trip.
func TestRunSearchRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{Query: "x", Limit: 51})
	if err == nil {
		t.Fatalf("expected error for over-budget limit")
	}
	if !strings.Contains(stderr.String(), "between 1 and 50") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

// TestRunSearchRejectsExplicitZeroLimit — `--limit 0` is outside
// the documented 1..50 range and must be rejected. The cobra-default
// zero (no flag passed) is still permitted; the distinction is made
// via `searchOptions.Changed`, which the verb's RunE wires from
// `cmd.Flags().Changed("limit")`.
func TestRunSearchRejectsExplicitZeroLimit(t *testing.T) {
	// Build a real cobra command so Changed("limit") returns true
	// after the flag is set by name — the runSearch-only helper used
	// by other tests doesn't go through flag parsing.
	cmd := newSearchCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"x", "--limit", "0"})
	err := cmd.Execute()
	if err == nil {
		t.Fatalf("expected error for explicit --limit=0")
	}
	if !strings.Contains(stderr.String(), "between 1 and 50") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

// TestRunSearchAllowsDefaultZeroLimit — without `--limit`,
// opts.Limit is cobra's default zero and Changed is false; the
// range gate must not fire so the backend's default applies.
func TestRunSearchAllowsDefaultZeroLimit(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.RetrieveResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	// Drive through the cobra command so Changed("limit") is false.
	cmd := newSearchCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"x", "--backplane", srv.URL})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("default zero limit should pass; got err=%v stderr=%q", err, stderr.String())
	}
}

// TestRunSearchHappyPath — POSTs the right body (source pinned to
// "kb") and renders the ranked-hits table. Handler decodes the
// wire body into the generated `api.RetrieveRequest` so the
// "no consumer-side retrieveRequest" property is the load-bearing
// claim under test.
func TestRunSearchHappyPath(t *testing.T) {
	var bodyOnWire api.RetrieveRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.RetrieveResponse{
			Hits: []api.RetrievalHit{
				{
					DocumentId:  mustParseUUID(t, "00000000-0000-0000-0000-000000000001"),
					TenantId:    mustParseUUID(t, stubTenantID),
					Source:      "kb",
					SourceId:    "vcenter-9.0-snapshot-revert",
					Kind:        "kb-entry",
					Body:        "Revert a snapshot in vCenter 9.0 via …",
					DocMetadata: map[string]any{},
					FusedScore:  0.95,
					CreatedAt:   time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC),
					UpdatedAt:   time.Date(2026, 5, 12, 0, 0, 0, 0, time.UTC),
				},
			},
			QueryDurationMs: 12.5,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{Query: "vsphere snapshot", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runSearch: %v; stderr=%s", err, stderr.String())
	}
	if bodyOnWire.Source == nil || *bodyOnWire.Source != "kb" {
		t.Errorf("expected source=kb pinned; got %+v", bodyOnWire)
	}
	if bodyOnWire.Query != "vsphere snapshot" {
		t.Errorf("expected query in body; got %+v", bodyOnWire)
	}
	if bodyOnWire.Limit != nil {
		t.Errorf("expected limit nil at zero (omitempty); got %+v", *bodyOnWire.Limit)
	}
	for _, want := range []string{"RANK", "SCORE", "SLUG", "vcenter-9.0-snapshot-revert", "0.9500"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

// TestRunSearchSendsLimitWhenSet — operator-supplied --limit lands
// on the wire as the typed pointer.
func TestRunSearchSendsLimitWhenSet(t *testing.T) {
	var bodyOnWire api.RetrieveRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &bodyOnWire)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.RetrieveResponse{Hits: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{Query: "x", Limit: 25, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runSearch: %v", err)
	}
	if bodyOnWire.Limit == nil || *bodyOnWire.Limit != 25 {
		t.Errorf("expected limit=25; got %+v", bodyOnWire.Limit)
	}
}

// TestRunSearchZeroHits — empty hits renders the no-hits hint.
func TestRunSearchZeroHits(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.RetrieveResponse{Hits: []api.RetrievalHit{}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{Query: "obscure", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runSearch: %v", err)
	}
	if !strings.Contains(stdout.String(), "no kb hits") {
		t.Errorf("expected no-hits line; got %q", stdout.String())
	}
}

// TestRunSearchJSONHappyPath — --json emits the raw RetrieveResponse.
func TestRunSearchJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.RetrieveResponse{
			Hits: []api.RetrievalHit{
				{
					DocumentId:  mustParseUUID(t, "00000000-0000-0000-0000-000000000001"),
					TenantId:    mustParseUUID(t, stubTenantID),
					Source:      "kb",
					SourceId:    "x",
					Kind:        "kb-entry",
					DocMetadata: map[string]any{},
					FusedScore:  0.5,
					CreatedAt:   time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC),
					UpdatedAt:   time.Date(2026, 5, 12, 0, 0, 0, 0, time.UTC),
				},
			},
			QueryDurationMs: 1.0,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{Query: "x", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runSearch --json: %v", err)
	}
	var decoded api.RetrieveResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if len(decoded.Hits) != 1 || decoded.Hits[0].SourceId != "x" {
		t.Errorf("decode produced %+v", decoded)
	}
}

// TestRunSearch403SurfacesInsufficientRole — read_only role on the
// retrieve route lands as 403.
func TestRunSearch403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: operator required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{Query: "x", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "operator required") {
		t.Errorf("expected role hint; got %q", stderr.String())
	}
}

// TestSnippetOfShortBody — body within the budget passes through.
func TestSnippetOfShortBody(t *testing.T) {
	if got := snippetOf("hello"); got != "hello" {
		t.Errorf("snippetOf short: got %q", got)
	}
}

// TestSnippetOfLongBody — body over the budget gets ellipsis-clipped.
func TestSnippetOfLongBody(t *testing.T) {
	long := strings.Repeat("a", 250)
	got := snippetOf(long)
	if !strings.HasSuffix(got, "…") {
		t.Errorf("expected ellipsis suffix on long body; got %q", got)
	}
	// 200 chars + the ellipsis rune; the test must not rely on
	// byte count due to multi-byte ellipsis.
	if len([]rune(got)) != 201 {
		t.Errorf("expected 201 runes; got %d", len([]rune(got)))
	}
}

// TestRunSearchRejects200WithoutJSONPayload pins the JSON200
// nil-guard (M5). A 200 with a missing or mistyped Content-Type
// leaves resp.JSON200 nil; without the guard, printSearchTable
// prints "no kb hits for this query" — actively misleading
// (conflated with a genuinely-empty result set). Route to
// output.Unexpected (exit 4) instead.
func TestRunSearchRejects200WithoutJSONPayload(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, _ *http.Request) {
		// Deliberately omit Content-Type so the generated parser
		// leaves JSON200 nil.
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not-json"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runSearch(cmd, searchOptions{Query: "x", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 200 without JSON payload")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without a retrieve response payload") {
		t.Errorf("expected detail mentioning missing payload; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}

// TestPrintSearchTableHandlesNilScores — *float32 fields must
// render without panic when nil (one signal missed the hit). The
// generated RetrievalHit type uses *float32 (matching the
// substrate's pydantic Field shape); ranks are *int.
func TestPrintSearchTableHandlesNilScores(t *testing.T) {
	cosine := float32(0.8)
	r := &api.RetrieveResponse{
		Hits: []api.RetrievalHit{
			{
				SourceId:    "only-cosine",
				FusedScore:  0.5,
				CosineScore: &cosine,
				Bm25Score:   nil,
			},
		},
		QueryDurationMs: 2.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, r)
	if !strings.Contains(buf.String(), "only-cosine") {
		t.Errorf("expected hit rendered; got %q", buf.String())
	}
}
