// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestBuildQueryRequestEmptyOptsOmitsEveryFilter — the no-flag form
// sends a minimal `{}` body, letting the backend defaults take over
// (server-side limit=100, no narrowing filters). Mirrors the
// retire-checklist learning that empty Go pointers must drop on the
// wire — otherwise the backend reads "set to empty string" as a
// match condition.
func TestBuildQueryRequestEmptyOptsOmitsEveryFilter(t *testing.T) {
	body := buildQueryRequest(queryOptions{})
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	got := string(raw)
	if got != "{}" {
		t.Errorf("empty opts marshalled non-empty: %s", got)
	}
}

// TestBuildQueryRequestEveryFlagLandsOnWire — every operator-set
// flag round-trips into the JSON body with the backend-expected
// key. The AuditQueryRequest Pydantic model is `extra="ignore"` so
// a typo'd key would silently drop; the test pins each key by name.
func TestBuildQueryRequestEveryFlagLandsOnWire(t *testing.T) {
	opts := queryOptions{
		Target:        "rdc-vcenter",
		Principal:     "damir",
		OpID:          "vsphere.vm.*",
		OpClass:       "write",
		ResultStatus:  "ok",
		Since:         "24h",
		Until:         "1h",
		AuditID:       "00000000-0000-0000-0000-000000000001",
		ParentAuditID: "00000000-0000-0000-0000-000000000002",
		Limit:         50,
		Cursor:        "opaque-cursor-bytes",
	}
	body := buildQueryRequest(opts)
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	wire := string(raw)
	for _, want := range []string{
		`"target":"rdc-vcenter"`,
		`"principal":"damir"`,
		`"op_id":"vsphere.vm.*"`,
		`"op_class":"write"`,
		`"result_status":"ok"`,
		`"since":"24h"`,
		`"until":"1h"`,
		`"audit_id":"00000000-0000-0000-0000-000000000001"`,
		`"parent_audit_id":"00000000-0000-0000-0000-000000000002"`,
		`"limit":50`,
		`"cursor":"opaque-cursor-bytes"`,
	} {
		if !strings.Contains(wire, want) {
			t.Errorf("buildQueryRequest missing %q in %s", want, wire)
		}
	}
}

// TestBuildQueryRequestLimitZeroDoesNotMarshal — Limit=0 means "use
// server default 100"; the wire must not carry `"limit":0` because
// Pydantic's `ge=1` validation rejects it.
func TestBuildQueryRequestLimitZeroDoesNotMarshal(t *testing.T) {
	body := buildQueryRequest(queryOptions{Limit: 0})
	raw, _ := json.Marshal(body)
	if strings.Contains(string(raw), "limit") {
		t.Errorf("limit=0 should be omitted; got %s", string(raw))
	}
}

// TestRunQueryRejectsOutOfRangeLimit — AC validation: --limit must
// be 1..1000 inclusive; out-of-range surfaces as exit-code 4 without
// touching the network.
func TestRunQueryRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{Limit: 9999})
	if err == nil {
		t.Fatalf("expected error for --limit=9999")
	}
	if !strings.Contains(stderr.String()+err.Error(), "limit must be between") {
		t.Errorf("stderr did not surface limit error: %s / %v", stderr.String(), err)
	}
}

// TestRunQueryHappyPathTable — full round-trip: stub backplane,
// POST hits /api/v1/audit/query, response renders as the documented
// table with the operator-visible columns.
func TestRunQueryHappyPathTable(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), `"target":"rdc-vcenter"`) {
			t.Errorf("body missing target filter: %s", body)
		}
		tname := "rdc-vcenter"
		next := "opaque-next-cursor"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(QueryResult{
			Rows: []Entry{{
				ID:           "00000000-0000-0000-0000-000000000001",
				TS:           "2026-05-13T15:42:11Z",
				PrincipalSub: "damir",
				TargetName:   &tname,
				Method:       "GET",
				Path:         "/api/v1/vsphere/vm/list",
				StatusCode:   200,
				OpID:         "vsphere.vm.list",
				OpClass:      "read",
				ResultStatus: "ok",
				Payload:      map[string]any{},
			}},
			NextCursor: &next,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{Target: "rdc-vcenter", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runQuery: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"TIME", "PRINCIPAL", "TARGET", "OP_ID", "CLASS", "STATUS",
		"damir", "rdc-vcenter", "vsphere.vm.list", "read", "ok",
		"NEXT: --cursor=opaque-next-cursor",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in %s", want, out)
		}
	}
}

// TestRunQueryJSONPassthroughRoundTrips — --json emits the raw
// QueryResult shape so jq pipelines see a stable contract.
// The backend always emits `next_cursor` as a JSON key (Pydantic
// v2 default for `str | None`); the CLI must preserve the key when
// re-marshalling so consumers can `jq .next_cursor` without an
// optional-presence dance.
func TestRunQueryJSONPassthroughRoundTrips(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// Emit the null next_cursor case the schema-stability
		// contract pins.
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runQuery --json: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"next_cursor"`) {
		t.Errorf("--json dropped next_cursor key: %s", stdout.String())
	}
	var decoded QueryResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if decoded.NextCursor != nil {
		t.Errorf("expected nil NextCursor; got %v", decoded.NextCursor)
	}
}

// TestRunQuery400SurfacesParserError — DurationParseError /
// InvalidCursorError / UnsupportedFilterError all return 400 from
// the backend with the parser's own message in the detail. The CLI
// surfaces it through the `unexpected` exit-code channel rather
// than silently treating it as a transport error.
func TestRunQuery400SurfacesParserError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"detail": "unrecognised duration 'foo'",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{Since: "foo", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 400")
	}
	if !strings.Contains(stderr.String(), "unrecognised duration") {
		t.Errorf("stderr missing parser detail: %s", stderr.String())
	}
}

// TestRunQueryUnreachableSurfacesAsTransport — a TCP-level failure
// (no server listening) lands as `unreachable`, the operator-
// readable exit-code-3 category.
func TestRunQueryUnreachableSurfacesAsTransport(t *testing.T) {
	// Pin to an unused TCP port by starting then closing a server.
	srv := httptest.NewServer(http.NewServeMux())
	addr := srv.URL
	srv.Close()
	seedXDGAndToken(t, addr)

	cmd, _, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{BackplaneOverride: addr})
	if err == nil {
		t.Fatalf("expected unreachable error")
	}
	// Two acceptable readings: connection refused (Darwin) or the
	// generic call-error wrapper. Both go through Unreachable.
	if !strings.Contains(stderr.String(), "call ") {
		t.Errorf("stderr does not look like Unreachable: %s", stderr.String())
	}
}

// TestPrintQueryTableEmpty — zero-row tenant renders the helper line
// without a header so operators don't confuse "no rows" with "table
// header but missing body".
func TestPrintQueryTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printQueryTable(&buf, &QueryResult{Rows: []Entry{}})
	out := buf.String()
	if !strings.Contains(out, "no audit rows matched the filter") {
		t.Errorf("empty table missing helper line: %s", out)
	}
	if strings.Contains(out, "TIME") {
		t.Errorf("empty table should not print header: %s", out)
	}
}
