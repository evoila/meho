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

// TestRunSearchHappyPath — POSTs the right body (source pinned to
// "kb") and renders the ranked-hits table.
func TestRunSearchHappyPath(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &bodyJSON)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RetrieveResponse{
			Hits: []RetrievalHit{
				{
					DocumentID: "00000000-0000-0000-0000-000000000001",
					Source:     "kb",
					SourceID:   "vcenter-9.0-snapshot-revert",
					Kind:       "kb-entry",
					Body:       "Revert a snapshot in vCenter 9.0 via …",
					FusedScore: 0.95,
				},
			},
			QueryDurationMS: 12.5,
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
	if bodyJSON["source"] != "kb" {
		t.Errorf("expected source=kb pinned; got %+v", bodyJSON)
	}
	if bodyJSON["query"] != "vsphere snapshot" {
		t.Errorf("expected query in body; got %+v", bodyJSON)
	}
	// limit=0 → omitempty drops it from the wire; the backend's
	// default of 10 applies.
	if _, present := bodyJSON["limit"]; present {
		t.Errorf("expected limit omitted at zero; got %+v", bodyJSON)
	}
	for _, want := range []string{"RANK", "SCORE", "SLUG", "vcenter-9.0-snapshot-revert", "0.9500"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

// TestRunSearchSendsLimitWhenSet — operator-supplied --limit lands
// on the wire.
func TestRunSearchSendsLimitWhenSet(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &bodyJSON)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RetrieveResponse{Hits: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{Query: "x", Limit: 25, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runSearch: %v", err)
	}
	got, ok := bodyJSON["limit"].(float64)
	if !ok || int(got) != 25 {
		t.Errorf("expected limit=25; got %+v", bodyJSON)
	}
}

// TestRunSearchZeroHits — empty hits renders the no-hits hint.
func TestRunSearchZeroHits(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(RetrieveResponse{Hits: []RetrievalHit{}})
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
		_ = json.NewEncoder(w).Encode(RetrieveResponse{
			Hits:            []RetrievalHit{{SourceID: "x", FusedScore: 0.5}},
			QueryDurationMS: 1.0,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{Query: "x", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runSearch --json: %v", err)
	}
	var decoded RetrieveResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if len(decoded.Hits) != 1 || decoded.Hits[0].SourceID != "x" {
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

// TestPrintSearchTableRendersBM25CosineScores — *float64 fields
// must render without panic when nil (one signal missed the hit).
func TestPrintSearchTableHandlesNilScores(t *testing.T) {
	cosine := 0.8
	r := &RetrieveResponse{
		Hits: []RetrievalHit{
			{SourceID: "only-cosine", FusedScore: 0.5, CosineScore: &cosine, BM25Score: nil},
		},
		QueryDurationMS: 2.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, r)
	if !strings.Contains(buf.String(), "only-cosine") {
		t.Errorf("expected hit rendered; got %q", buf.String())
	}
}
