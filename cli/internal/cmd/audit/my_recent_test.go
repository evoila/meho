// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestBuildMyRecentParamsOmitsEmptyParams — the no-flag form leaves
// every pointer field on the generated params struct nil, so the
// query-string builder emits no `since` / `limit` keys and the
// backend's own defaults take over.
func TestBuildMyRecentParamsOmitsEmptyParams(t *testing.T) {
	p := buildMyRecentParams(myRecentOptions{})
	if p.Since != nil {
		t.Errorf("Since should be nil; got %v", *p.Since)
	}
	if p.Limit != nil {
		t.Errorf("Limit should be nil; got %v", *p.Limit)
	}
}

// TestBuildMyRecentParamsEmitsParams — every set flag lands on the
// params struct with the correct value.
func TestBuildMyRecentParamsEmitsParams(t *testing.T) {
	p := buildMyRecentParams(myRecentOptions{Since: "2w", Limit: 25})
	if p.Since == nil || *p.Since != "2w" {
		t.Errorf("Since: got %v; want 2w", p.Since)
	}
	if p.Limit == nil || *p.Limit != 25 {
		t.Errorf("Limit: got %v; want 25", p.Limit)
	}
}

// TestRunMyRecentSendsParamsToWire — round-trip: a set --since /
// --limit pair lands on the wire as query-string params, and the
// CLI does NOT supply a `principal` param (the backend reads it from
// the JWT server-side).
func TestRunMyRecentSendsParamsToWire(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/my-recent", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		if got := r.URL.Query().Get("since"); got != "2w" {
			t.Errorf("since: got %q; want 2w", got)
		}
		if got := r.URL.Query().Get("limit"); got != "25" {
			t.Errorf("limit: got %q; want 25", got)
		}
		// Server-side `principal` filter — never client-supplied.
		if r.URL.Query().Get("principal") != "" {
			t.Errorf("CLI should not supply principal query param: got %q",
				r.URL.Query().Get("principal"))
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AuditQueryResult{
			Rows: []api.AuditEntry{{
				Id:           mustUUID(t, "00000000-0000-0000-0000-000000000001"),
				Ts:           mustTS(t, "2026-05-15T09:00:00Z"),
				PrincipalSub: "damir",
				Method:       "POST",
				Path:         "/api/v1/retrieve",
				StatusCode:   200,
				OpId:         "meho.retrieval.query",
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
	err := runMyRecent(cmd, myRecentOptions{
		Since:             "2w",
		Limit:             25,
		BackplaneOverride: srv.URL,
	})
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

// TestRunMyRecentOmitsEmptyParamsOnWire — the no-flag form sends a
// bare GET so the backend's defaults take over.
func TestRunMyRecentOmitsEmptyParamsOnWire(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/my-recent", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.RawQuery; got != "" {
			t.Errorf("no-flag form should send empty query; got %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runMyRecent(cmd, myRecentOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runMyRecent: %v; stderr=%s", err, stderr.String())
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

// TestRunMyRecentJSONRoundTrips — --json emits the raw server bytes
// verbatim; the typed-AuditQueryResult shape parses back cleanly.
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
	var decoded api.AuditQueryResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
}
