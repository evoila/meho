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

// TestBuildWhoTouchedPathOmitsEmptyParams — the no-flag form sends a
// bare path so the backend's defaults (since=24h, limit=100) take
// over.
func TestBuildWhoTouchedPathOmitsEmptyParams(t *testing.T) {
	got := buildWhoTouchedPath("rdc-vcenter", "", 0)
	if got != "/api/v1/audit/who-touched/rdc-vcenter" {
		t.Errorf("buildWhoTouchedPath empty: got %q", got)
	}
}

// TestBuildWhoTouchedPathEmitsParams — every set flag lands on the
// wire as a query string param.
func TestBuildWhoTouchedPathEmitsParams(t *testing.T) {
	got := buildWhoTouchedPath("rdc-vcenter", "7d", 50)
	if !strings.Contains(got, "since=7d") {
		t.Errorf("missing since: %q", got)
	}
	if !strings.Contains(got, "limit=50") {
		t.Errorf("missing limit: %q", got)
	}
}

// TestBuildWhoTouchedPathEscapesTarget — a target name with a slash
// (operator typo) doesn't collapse the URL path.
func TestBuildWhoTouchedPathEscapesTarget(t *testing.T) {
	got := buildWhoTouchedPath("foo/bar", "", 0)
	if !strings.Contains(got, "foo%2Fbar") {
		t.Errorf("buildWhoTouchedPath did not URL-encode slash: %q", got)
	}
}

// TestRunWhoTouchedRequiresTarget — the cobra `ExactArgs(1)` gate
// already catches missing args at the parser level; this exercises
// the defence-in-depth path inside runWhoTouched.
func TestRunWhoTouchedRequiresTarget(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runWhoTouched(cmd, whoTouchedOptions{Target: ""})
	if err == nil {
		t.Fatalf("expected error for empty target")
	}
	if !strings.Contains(stderr.String(), "non-empty <target>") {
		t.Errorf("stderr missing target-required hint: %s", stderr.String())
	}
}

// TestRunWhoTouchedHappyPath — the verb hits GET /api/v1/audit/who-
// touched/{target} and renders the rows using the same table the
// query verb emits.
func TestRunWhoTouchedHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/who-touched/rdc-vcenter", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		if r.URL.Query().Get("since") != "7d" {
			t.Errorf("query since: got %q; want 7d", r.URL.Query().Get("since"))
		}
		tname := "rdc-vcenter"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(QueryResult{
			Rows: []Entry{{
				ID:           "00000000-0000-0000-0000-000000000001",
				TS:           "2026-05-12T12:00:00Z",
				PrincipalSub: "tarik",
				TargetName:   &tname,
				Method:       "POST",
				Path:         "/api/v1/vsphere/nsx/firewall/update",
				StatusCode:   200,
				OpID:         "nsx.firewall.update",
				OpClass:      "write",
				ResultStatus: "ok",
				Payload:      map[string]any{},
			}},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runWhoTouched(cmd, whoTouchedOptions{
		Target:            "rdc-vcenter",
		Since:             "7d",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runWhoTouched: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"TIME", "tarik", "rdc-vcenter", "nsx.firewall.update", "write"} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in %s", want, out)
		}
	}
}

// TestRunWhoTouchedJSONRoundTrips — --json emits the same wire shape
// the substrate returns; pinning the key set keeps jq pipelines
// stable.
func TestRunWhoTouchedJSONRoundTrips(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/who-touched/rdc-vcenter", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runWhoTouched(cmd, whoTouchedOptions{
		Target:            "rdc-vcenter",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runWhoTouched --json: %v", err)
	}
	var decoded QueryResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
}

// TestRunWhoTouchedRejectsOutOfRangeLimit — defensive --limit
// validation matches the query verb.
func TestRunWhoTouchedRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	if err := runWhoTouched(cmd, whoTouchedOptions{Target: "rdc-vcenter", Limit: 99999}); err == nil {
		t.Fatalf("expected error for --limit=99999")
	}
}
