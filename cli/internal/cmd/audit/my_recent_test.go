// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestBuildMyRecentPathOmitsEmptyParams — the no-flag form sends a
// bare path so the backend's defaults take over.
func TestBuildMyRecentPathOmitsEmptyParams(t *testing.T) {
	got := buildMyRecentPath("", 0)
	if got != "/api/v1/audit/my-recent" {
		t.Errorf("buildMyRecentPath empty: got %q", got)
	}
}

// TestBuildMyRecentPathEmitsParams — every set flag lands on the
// query string.
func TestBuildMyRecentPathEmitsParams(t *testing.T) {
	got := buildMyRecentPath("2w", 25)
	if !strings.Contains(got, "since=2w") {
		t.Errorf("missing since: %q", got)
	}
	if !strings.Contains(got, "limit=25") {
		t.Errorf("missing limit: %q", got)
	}
}

// TestRunMyRecentHappyPath — the verb hits GET /api/v1/audit/my-
// recent. The backend's `principal` filter is derived from the JWT
// sub claim server-side; the CLI never supplies it.
func TestRunMyRecentHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/my-recent", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		// The route reads the principal filter from the JWT
		// server-side; no client-supplied principal flag.
		if r.URL.Query().Get("principal") != "" {
			t.Errorf("CLI should not supply principal query param: got %q",
				r.URL.Query().Get("principal"))
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(QueryResult{
			Rows: []Entry{{
				ID:           "00000000-0000-0000-0000-000000000001",
				TS:           "2026-05-15T09:00:00Z",
				PrincipalSub: "damir",
				Method:       "POST",
				Path:         "/api/v1/retrieve",
				StatusCode:   200,
				OpID:         "meho.retrieval.query",
				OpClass:      "audit_query",
				ResultStatus: "ok",
				Payload:      map[string]any{},
			}},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runMyRecent(cmd, myRecentOptions{BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runMyRecent: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"TIME", "damir", "meho.retrieval.query", "audit_query"} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in %s", want, out)
		}
	}
}

// TestRunMyRecentRejectsOutOfRangeLimit — defensive --limit
// validation matches the query verb.
func TestRunMyRecentRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	if err := runMyRecent(cmd, myRecentOptions{Limit: 99999}); err == nil {
		t.Fatalf("expected error for --limit=99999")
	}
}

// TestRunMyRecentJSONRoundTrips — --json emits the structured shape.
func TestRunMyRecentJSONRoundTrips(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/my-recent", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runMyRecent(cmd, myRecentOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runMyRecent --json: %v", err)
	}
	var decoded QueryResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
}
