// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func ptr[T any](v T) *T { return &v }

// TestRunSearchPrintsWikiHits — the wiki's hits follow the store table,
// with their source link.
func TestRunSearchPrintsWikiHits(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.RetrieveResponse{
			Wiki: &api.KbWikiSearch{
				Collection: "team-wiki",
				Searched:   true,
				Hits: &[]api.DocsChunk{{
					ChunkId:    "c1",
					Collection: ptr("team-wiki"),
					Content:    "Snapshot limits for the product.",
					Title:      ptr("Snapshot limits"),
					SourceUrl:  ptr("https://git.example.com/wiki/-/blob/abc123/products/x/snapshot-limits.md"),
				}},
				DuplicateCount: ptr(1),
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runSearch(cmd, searchOptions{Query: "snapshot", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runSearch: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{
		"no kb hits for this query",
		"team wiki hits (docs collection team-wiki)",
		"Snapshot limits",
		"blob/abc123/products/x/snapshot-limits.md",
		"1 wiki hits are shown above as their imported copy",
	} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

// TestPrintWikiHitsSaysWhyNotSearched — a wiki that was not searched is
// named with the reason, never left out in silence.
func TestPrintWikiHitsSaysWhyNotSearched(t *testing.T) {
	var buf bytes.Buffer
	printWikiHits(&buf, &api.KbWikiSearch{
		Collection: "team-wiki",
		Searched:   false,
		Message:    ptr("The team wiki (docs collection `team-wiki`) was not searched: the knowledge index did not answer; try again later."),
	})
	if !strings.Contains(buf.String(), "was not searched: the knowledge index did not answer") {
		t.Errorf("missing reason in %q", buf.String())
	}
}

// TestPrintWikiHitsNilPrintsNothing — a tenant without a wiki sees no change.
func TestPrintWikiHitsNilPrintsNothing(t *testing.T) {
	var buf bytes.Buffer
	printWikiHits(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("expected no output; got %q", buf.String())
	}
}
