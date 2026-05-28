// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunRecentBindsSince24h — `meho audit recent` is the shortcut
// over `meho audit query --since 24h`. The verb's only job is to
// bind --since to 24h server-side; the rest of the round-trip is
// the same query path the full filter uses. This pins that the
// canonical "since=24h" filter actually lands on the wire (so the
// shortcut doesn't silently degrade into a full-window scan if a
// regression nudges the default constant).
func TestRunRecentBindsSince24h(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), `"since":"24h"`) {
			t.Errorf("recent did not bind since=24h: %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{
		Since:             recentDefaultSince,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runQuery via recent shortcut: %v; stderr=%s", err, stderr.String())
	}
}

// TestRunRecentJSONRoundTrips — --json behaviour is the same as the
// full query verb; the recent shortcut doesn't sit between the
// caller and the response.
func TestRunRecentJSONRoundTrips(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runQuery(cmd, queryOptions{
		Since:             recentDefaultSince,
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runQuery via recent --json: %v", err)
	}
	var decoded api.AuditQueryResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
}

// TestNewRecentCmdHasLimitFlag — the verb advertises --limit on its
// own help text; operators with autocomplete enabled rely on this.
func TestNewRecentCmdHasLimitFlag(t *testing.T) {
	cmd := newRecentCmd()
	if cmd.Flag("limit") == nil {
		t.Errorf("recent verb missing --limit flag")
	}
	if cmd.Flag("json") == nil {
		t.Errorf("recent verb missing --json flag")
	}
}
