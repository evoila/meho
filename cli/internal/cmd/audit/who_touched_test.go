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

// TestBuildWhoTouchedParamsOmitsEmptyParams — the no-flag form
// leaves every pointer field nil so the query-string builder emits
// no `since` / `limit` keys and the backend's defaults take over.
func TestBuildWhoTouchedParamsOmitsEmptyParams(t *testing.T) {
	p := buildWhoTouchedParams(whoTouchedOptions{})
	if p.Since != nil {
		t.Errorf("Since should be nil; got %v", *p.Since)
	}
	if p.Limit != nil {
		t.Errorf("Limit should be nil; got %v", *p.Limit)
	}
}

// TestBuildWhoTouchedParamsEmitsParams — every set flag lands on the
// params struct with the correct value.
func TestBuildWhoTouchedParamsEmitsParams(t *testing.T) {
	p := buildWhoTouchedParams(whoTouchedOptions{Since: "7d", Limit: 50})
	if p.Since == nil || *p.Since != "7d" {
		t.Errorf("Since: got %v; want 7d", p.Since)
	}
	if p.Limit == nil || *p.Limit != 50 {
		t.Errorf("Limit: got %v; want 50", p.Limit)
	}
}

// TestRunWhoTouchedHappyPath — the verb hits GET /api/v1/audit/who-
// touched/{target} with the typed path parameter, sends the query-
// string params on the wire, and renders the response with the same
// table the query verb emits.
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
		_ = json.NewEncoder(w).Encode(api.AuditQueryResult{
			Rows: []api.AuditEntry{{
				Id:           mustUUID(t, "00000000-0000-0000-0000-000000000001"),
				Ts:           mustTS(t, "2026-05-12T12:00:00Z"),
				PrincipalSub: "tarik",
				TargetName:   &tname,
				Method:       "POST",
				Path:         "/api/v1/vsphere/nsx/firewall/update",
				StatusCode:   200,
				OpId:         "nsx.firewall.update",
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

// TestRunWhoTouchedEscapesTargetInPath — a target name with a slash
// is URL-encoded by the generated request builder so the path
// doesn't collapse.
func TestRunWhoTouchedEscapesTargetInPath(t *testing.T) {
	var seenPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenPath = r.URL.EscapedPath()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runWhoTouched(cmd, whoTouchedOptions{
		Target:            "foo/bar",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runWhoTouched: %v; stderr=%s", err, stderr.String())
	}
	// The slash must appear as %2F in the wire path; otherwise the
	// HTTP router would split it into a different sub-path.
	if !strings.Contains(seenPath, "foo%2Fbar") {
		t.Errorf("target slash not percent-encoded in wire path: %q", seenPath)
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

// TestRunWhoTouchedJSONRoundTrips — --json emits the raw server
// bytes; pinning the key set keeps jq pipelines stable.
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
	var decoded api.AuditQueryResult
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
