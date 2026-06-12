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
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// TestBuildAuditQueryRequestEmptyOptsHasZeroNonNilFilters — the
// no-flag form leaves every pointer-typed filter nil. The generated
// `api.AuditQueryRequest` JSON marshalling will emit each nullable
// key as `null` on the wire (the backend's Pydantic model accepts
// `Optional[...] = None`), and the `omitempty`-tagged `limit` is
// dropped because it's a `*int` left nil. The exact wire-shape
// assertion is on `TestBuildAuditQueryRequestEmptyOptsWireShape`.
func TestBuildAuditQueryRequestEmptyOptsHasZeroNonNilFilters(t *testing.T) {
	body, err := buildAuditQueryRequest(queryOptions{})
	if err != nil {
		t.Fatalf("buildAuditQueryRequest: %v", err)
	}
	if body.Target != nil {
		t.Errorf("Target should be nil; got %v", *body.Target)
	}
	if body.Principal != nil {
		t.Errorf("Principal should be nil; got %v", *body.Principal)
	}
	if body.OpId != nil {
		t.Errorf("OpId should be nil; got %v", *body.OpId)
	}
	if body.OpClass != nil {
		t.Errorf("OpClass should be nil; got %v", *body.OpClass)
	}
	if body.ResultStatus != nil {
		t.Errorf("ResultStatus should be nil; got %v", *body.ResultStatus)
	}
	if body.Since != nil {
		t.Errorf("Since should be nil; got %v", *body.Since)
	}
	if body.Until != nil {
		t.Errorf("Until should be nil; got %v", *body.Until)
	}
	if body.Cursor != nil {
		t.Errorf("Cursor should be nil; got %v", *body.Cursor)
	}
	if body.AuditId != nil {
		t.Errorf("AuditId should be nil; got %v", *body.AuditId)
	}
	if body.ParentAuditId != nil {
		t.Errorf("ParentAuditId should be nil; got %v", *body.ParentAuditId)
	}
	if body.AgentSessionId != nil {
		t.Errorf("AgentSessionId should be nil; got %v", *body.AgentSessionId)
	}
	if body.Limit != nil {
		t.Errorf("Limit should be nil; got %v", *body.Limit)
	}
}

// TestBuildAuditQueryRequestEmptyOptsWireShape — pin the wire-shape
// of the no-flag body so a regression away from null-permitted
// filters (e.g. someone re-adding `omitempty` to half the fields
// inconsistently) surfaces at unit-time. Every nullable filter
// renders as `"key":null`; `limit` is omitted entirely (the only
// field carrying `omitempty` on the generated struct).
func TestBuildAuditQueryRequestEmptyOptsWireShape(t *testing.T) {
	body, _ := buildAuditQueryRequest(queryOptions{})
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	got := string(raw)
	for _, want := range []string{
		`"target":null`, `"principal":null`, `"op_id":null`, `"op_class":null`,
		`"result_status":null`, `"since":null`, `"until":null`, `"cursor":null`,
		`"audit_id":null`, `"parent_audit_id":null`, `"agent_session_id":null`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("wire shape missing %q in %s", want, got)
		}
	}
	if strings.Contains(got, `"limit"`) {
		t.Errorf("limit should be omitted when nil; got %s", got)
	}
}

// TestBuildAuditQueryRequestEveryFlagLandsOnWire — every operator-
// set flag round-trips into the JSON body with the backend-expected
// key. The `extra="forbid"` Pydantic model rejects unknown fields
// with 422, so a typo'd key would fail the contract; the test pins
// each key by name.
func TestBuildAuditQueryRequestEveryFlagLandsOnWire(t *testing.T) {
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
		SessionID:     "00000000-0000-0000-0000-000000000003",
		Limit:         50,
		Cursor:        "opaque-cursor-bytes",
	}
	body, err := buildAuditQueryRequest(opts)
	if err != nil {
		t.Fatalf("buildAuditQueryRequest: %v", err)
	}
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
		`"agent_session_id":"00000000-0000-0000-0000-000000000003"`,
		`"limit":50`,
		`"cursor":"opaque-cursor-bytes"`,
	} {
		if !strings.Contains(wire, want) {
			t.Errorf("buildAuditQueryRequest missing %q in %s", want, wire)
		}
	}
}

// TestBuildAuditQueryRequestLimitZeroDoesNotMarshal — Limit=0 means
// "use server default 100"; the wire must not carry `"limit":0`
// because Pydantic's `ge=1` validation rejects it.
func TestBuildAuditQueryRequestLimitZeroDoesNotMarshal(t *testing.T) {
	body, _ := buildAuditQueryRequest(queryOptions{Limit: 0})
	raw, _ := json.Marshal(body)
	if strings.Contains(string(raw), `"limit"`) {
		t.Errorf("limit=0 should be omitted; got %s", string(raw))
	}
}

// TestBuildAuditQueryRequestRejectsBadAuditID — UUID-at-the-edge:
// a non-UUID `--audit-id` is rejected client-side before any
// network round-trip.
func TestBuildAuditQueryRequestRejectsBadAuditID(t *testing.T) {
	_, err := buildAuditQueryRequest(queryOptions{AuditID: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected error for non-UUID --audit-id")
	}
	if !strings.Contains(err.Error(), "must be a valid UUID") {
		t.Errorf("error missing UUID hint: %v", err)
	}
}

// TestBuildAuditQueryRequestRejectsBadParentAuditID — same gate on
// --parent-audit-id.
func TestBuildAuditQueryRequestRejectsBadParentAuditID(t *testing.T) {
	_, err := buildAuditQueryRequest(queryOptions{ParentAuditID: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected error for non-UUID --parent-audit-id")
	}
}

// TestBuildAuditQueryRequestRejectsBadSessionID — same gate on
// --session-id (mirrors the dedicated test below, kept here so the
// builder's contract is one-stop).
func TestBuildAuditQueryRequestRejectsBadSessionID(t *testing.T) {
	_, err := buildAuditQueryRequest(queryOptions{SessionID: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected error for non-UUID --session-id")
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
		_ = json.NewEncoder(w).Encode(api.AuditQueryResult{
			Rows: []api.AuditEntry{{
				Id:           mustUUID(t, "00000000-0000-0000-0000-000000000001"),
				Ts:           mustTS(t, "2026-05-13T15:42:11Z"),
				PrincipalSub: "damir",
				TargetName:   &tname,
				Method:       "GET",
				Path:         "/api/v1/vsphere/vm/list",
				StatusCode:   200,
				OpId:         "vsphere.vm.list",
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
// server bytes verbatim so jq pipelines see a stable contract.
// The backend always emits `next_cursor` as a JSON key (Pydantic
// v2 default for `str | None`); the CLI's verbatim passthrough
// preserves the key when re-emitting so consumers can `jq
// .next_cursor` without an optional-presence dance.
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
	var decoded api.AuditQueryResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
	if decoded.NextCursor != nil {
		t.Errorf("expected nil NextCursor; got %v", decoded.NextCursor)
	}
}

// TestRunQueryJSONPreservesPayloadIntegerPrecision pins the
// precision-preservation contract --json carries: an integer above
// 2^53 in an audit row's payload survives the round-trip exactly,
// because the verb writes the server bytes verbatim rather than
// round-tripping through the generated client's
// `map[string]interface{}` (float64-conversion) decoder.
func TestRunQueryJSONPreservesPayloadIntegerPrecision(t *testing.T) {
	bigInt := int64(9007199254740993) // 2^53 + 1; rounds under float64
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// Hand-encode so the integer literal isn't normalised
		// through json.Marshal's float path; the operator-side
		// guarantee is "the bytes the backend wrote, unchanged".
		_, _ = w.Write([]byte(`{"rows":[{"id":"00000000-0000-0000-0000-000000000001",` +
			`"ts":"2026-05-13T15:42:11Z","tenant_id":null,"principal_sub":"damir",` +
			`"principal_name":null,"target_id":null,"target_name":null,` +
			`"method":"GET","path":"/x","status_code":200,"request_id":null,` +
			`"duration_ms":null,"payload":{"hit_count":9007199254740993},` +
			`"op_id":"x.y","op_class":"read","result_status":"ok",` +
			`"parent_audit_id":null,"agent_session_id":null,` +
			`"broadcast_event_id":null}],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runQuery(cmd, queryOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runQuery --json: %v", err)
	}
	// The exact integer survives in the emitted bytes.
	if !strings.Contains(stdout.String(), `"hit_count":9007199254740993`) {
		t.Errorf("integer precision lost in --json passthrough: %s", stdout.String())
	}
	// And the decoded shape (with UseNumber) preserves it too.
	dec := json.NewDecoder(bytes.NewReader(stdout.Bytes()))
	dec.UseNumber()
	var roundtrip map[string]any
	if err := dec.Decode(&roundtrip); err != nil {
		t.Fatalf("decode --json output: %v", err)
	}
	rows, _ := roundtrip["rows"].([]any)
	if len(rows) != 1 {
		t.Fatalf("rows: got %d; want 1", len(rows))
	}
	row, _ := rows[0].(map[string]any)
	payload, _ := row["payload"].(map[string]any)
	got, _ := payload["hit_count"].(json.Number)
	if got.String() != "9007199254740993" {
		t.Errorf("precision lost on decode: got %s; want 9007199254740993", got)
	}
	_ = bigInt // pin the literal to its semantic value in the assertion
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
	if !strings.Contains(stderr.String(), "call ") {
		t.Errorf("stderr does not look like Unreachable: %s", stderr.String())
	}
}

// TestRunQuerySessionIDRejectsNonUUID — AC: a non-UUID --session-id
// is rejected client-side with a clear message, before any network
// round-trip (the backend would otherwise emit a 422 validation
// envelope). The check now runs inside `buildAuditQueryRequest`
// since the generated client requires a parsed UUID on the typed
// param; the rejection still surfaces via the same render path.
func TestRunQuerySessionIDRejectsNonUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{SessionID: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected error for non-UUID --session-id")
	}
	if !strings.Contains(stderr.String(), "must be a valid UUID") {
		t.Errorf("stderr missing UUID-rejection hint: %s", stderr.String())
	}
}

// TestRunQuerySessionIDLandsInBody — a valid --session-id sets
// agent_session_id on the wire so the backend narrows to that session.
// This is the flat companion to `meho audit replay`.
func TestRunQuerySessionIDLandsInBody(t *testing.T) {
	sessionID := "11111111-1111-1111-1111-111111111111"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/query", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), `"agent_session_id":"`+sessionID+`"`) {
			t.Errorf("body missing agent_session_id filter: %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[],"next_cursor":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runQuery(cmd, queryOptions{SessionID: sessionID, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runQuery --session-id: %v; stderr=%s", err, stderr.String())
	}
}

// TestPrintQueryTableEmpty — zero-row tenant renders the helper line
// without a header so operators don't confuse "no rows" with "table
// header but missing body".
func TestPrintQueryTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printQueryTable(&buf, &api.AuditQueryResult{Rows: []api.AuditEntry{}})
	out := buf.String()
	if !strings.Contains(out, "no audit rows matched the filter") {
		t.Errorf("empty table missing helper line: %s", out)
	}
	if strings.Contains(out, "TIME") {
		t.Errorf("empty table should not print header: %s", out)
	}
}

// mustTS parses s as RFC3339, failing the test on error. The
// generated client's `Ts` field is `time.Time`; fixtures use the
// helper so callers don't repeat the parse-or-fatal pattern.
func mustTS(t *testing.T, s string) time.Time {
	t.Helper()
	ts, err := time.Parse(time.RFC3339, s)
	if err != nil {
		t.Fatalf("mustTS(%q): %v", s, err)
	}
	return ts
}
