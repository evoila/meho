// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunShowRejectsNonUUIDArg — UUID-at-the-edge: a non-UUID
// argument is rejected client-side with a clear message, before any
// network round-trip. The typed-client path parameter is
// `openapi_types.UUID`; parsing here keeps the bad-input error a
// clean output.Unexpected instead of a panic.
func TestRunShowRejectsNonUUIDArg(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{AuditID: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected error for non-UUID audit-id")
	}
	if !strings.Contains(stderr.String(), "audit-id is not a valid UUID") {
		t.Errorf("stderr missing UUID-rejection hint: %s", stderr.String())
	}
}

// TestRunShowRequiresAuditIDArg — the empty-argument path goes
// through the same renderError surface as a backplane refusal, so
// operators see one consistent error class. (Cobra's ExactArgs(1)
// gate also catches this at parse time; this exercises the defence-
// in-depth check inside runShow.)
func TestRunShowRequiresAuditIDArg(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{AuditID: ""})
	if err == nil {
		t.Fatalf("expected error for empty audit-id")
	}
	if !strings.Contains(stderr.String(), "non-empty <audit-id>") {
		t.Errorf("stderr missing arg-required hint: %s", stderr.String())
	}
}

// TestRunShowHappyPath — the verb hits GET /api/v1/audit/show/{id}
// and renders the operator-friendly key-value summary with every
// pinned field.
func TestRunShowHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/show/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		expected := "/api/v1/audit/show/11111111-1111-1111-1111-111111111111"
		if r.URL.Path != expected {
			t.Errorf("path: got %q; want %q", r.URL.Path, expected)
		}
		tname := "rdc-vcenter"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AuditEntry{
			Id:           mustUUID(t, "11111111-1111-1111-1111-111111111111"),
			Ts:           mustTS(t, "2026-05-13T15:42:11Z"),
			PrincipalSub: "damir",
			TargetName:   &tname,
			Method:       "GET",
			Path:         "/api/v1/vsphere/vm/list",
			StatusCode:   200,
			OpId:         "vsphere.vm.list",
			OpClass:      "read",
			ResultStatus: "ok",
			Payload:      map[string]any{"hit_count": float64(7)},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{
		AuditID:           "11111111-1111-1111-1111-111111111111",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"id:", "11111111-1111-1111-1111-111111111111",
		"principal_sub:", "damir",
		"target_name:", "rdc-vcenter",
		"op_id:", "vsphere.vm.list",
		"op_class:", "read",
		"result_status:", "ok",
		"payload:", "hit_count=7",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("summary missing %q in %s", want, out)
		}
	}
}

// TestRunShow404SurfacesNotFound — the backend returns 404 both
// when an audit_id genuinely doesn't exist and when it exists but
// belongs to another tenant. The CLI surfaces a single message
// rather than two so existence never leaks.
func TestRunShow404SurfacesNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/show/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail": "audit row not found"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{
		AuditID:           "11111111-1111-1111-1111-111111111111",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "audit row not found") {
		t.Errorf("stderr missing not-found hint: %s", stderr.String())
	}
}

// TestRunShow422SurfacesValidationDetail — a 422 from the server
// (e.g. the route's downstream validator rejecting the body shape)
// passes through with the "invalid request" prefix. (Note: a
// malformed-UUID `<audit-id>` arg is now caught at the verb edge by
// `uuid.Parse` so it never reaches the network; this test covers
// the residual server-side 422 path.)
func TestRunShow422SurfacesValidationDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/show/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":[{"loc":["body"],"msg":"value error"}]}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runShow(cmd, showOptions{
		AuditID:           "11111111-1111-1111-1111-111111111111",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 422")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("stderr missing validation hint: %s", stderr.String())
	}
}

// TestRunShowJSONVerbatim — --json emits the raw server bytes so a
// payload integer above 2^53 (Unix-millis timestamps, hash blobs)
// survives without rounding through the generated client's
// `map[string]interface{}` decoder.
func TestRunShowJSONVerbatim(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/show/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// Hand-encode so the integer isn't normalised through
		// json.Marshal's float path.
		_, _ = w.Write([]byte(`{"id":"11111111-1111-1111-1111-111111111111",` +
			`"ts":"2026-05-13T15:42:11Z","tenant_id":null,"principal_sub":"damir",` +
			`"principal_name":null,"target_id":null,"target_name":null,` +
			`"method":"GET","path":"/x","status_code":200,"request_id":null,` +
			`"duration_ms":null,"payload":{"hit_count":9007199254740993},` +
			`"op_id":"x.y","op_class":"read","result_status":"ok",` +
			`"parent_audit_id":null,"agent_session_id":null,` +
			`"broadcast_event_id":null}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runShow(cmd, showOptions{
		AuditID:           "11111111-1111-1111-1111-111111111111",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runShow --json: %v", err)
	}
	if !strings.Contains(stdout.String(), `"hit_count":9007199254740993`) {
		t.Errorf("integer precision lost in --json: %s", stdout.String())
	}
}

// TestPrintEntrySummaryShowsDashForNullFields — optional fields
// render as "-" so the summary stays grep-friendly across rows
// with different optional-field populations.
func TestPrintEntrySummaryShowsDashForNullFields(t *testing.T) {
	var buf bytes.Buffer
	printEntrySummary(&buf, &api.AuditEntry{
		Id:           mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		Ts:           mustTS(t, "2026-05-13T00:00:00Z"),
		PrincipalSub: "damir",
		Method:       "GET",
		Path:         "/x",
		StatusCode:   200,
		OpId:         "x.y",
		OpClass:      "read",
		ResultStatus: "ok",
		Payload:      map[string]any{},
	})
	out := buf.String()
	for _, want := range []string{
		"tenant_id:         -",
		"principal_name:    -",
		"target_id:         -",
		"target_name:       -",
		"request_id:        -",
		"duration_ms:       -",
		"parent_audit_id:   -",
		"agent_session_id:  -",
		"broadcast_event_id: -",
		"payload:           -",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("summary missing %q in %s", want, out)
		}
	}
}

// TestPrintEntrySummaryRendersUUIDsAndOptionals — the populated-
// field render path. UUIDs come back as canonical
// lowercase-hyphenated strings; optional strings render verbatim.
func TestPrintEntrySummaryRendersUUIDsAndOptionals(t *testing.T) {
	var buf bytes.Buffer
	pname := "Damir"
	tname := "rdc-vcenter"
	dur := "12.3"
	printEntrySummary(&buf, &api.AuditEntry{
		Id:               mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		Ts:               mustTS(t, "2026-05-13T00:00:00Z"),
		TenantId:         mustUUIDPtr(t, "22222222-2222-2222-2222-222222222222"),
		PrincipalSub:     "damir",
		PrincipalName:    &pname,
		TargetId:         mustUUIDPtr(t, "33333333-3333-3333-3333-333333333333"),
		TargetName:       &tname,
		Method:           "GET",
		Path:             "/x",
		StatusCode:       200,
		RequestId:        mustUUIDPtr(t, "44444444-4444-4444-4444-444444444444"),
		DurationMs:       &dur,
		OpId:             "x.y",
		OpClass:          "read",
		ResultStatus:     "ok",
		ParentAuditId:    mustUUIDPtr(t, "55555555-5555-5555-5555-555555555555"),
		AgentSessionId:   mustUUIDPtr(t, "66666666-6666-6666-6666-666666666666"),
		BroadcastEventId: mustUUIDPtr(t, "77777777-7777-7777-7777-777777777777"),
		Payload:          map[string]any{"k": "v"},
	})
	out := buf.String()
	for _, want := range []string{
		"tenant_id:         22222222-2222-2222-2222-222222222222",
		"principal_name:    Damir",
		"target_id:         33333333-3333-3333-3333-333333333333",
		"target_name:       rdc-vcenter",
		"request_id:        44444444-4444-4444-4444-444444444444",
		"duration_ms:       12.3",
		"parent_audit_id:   55555555-5555-5555-5555-555555555555",
		"agent_session_id:  66666666-6666-6666-6666-666666666666",
		"broadcast_event_id: 77777777-7777-7777-7777-777777777777",
		"payload:           k=v",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("summary missing %q in %s", want, out)
		}
	}
}

// TestFormatPayloadSortedKeys — payload renderer keys are sorted so
// snapshot tests stay deterministic and operator diffs across runs
// don't churn on map-iteration order.
func TestFormatPayloadSortedKeys(t *testing.T) {
	got := formatPayload(map[string]any{
		"zoo":      true,
		"alpha":    1,
		"midpoint": "x",
	})
	// Sorted: alpha, midpoint, zoo
	if !strings.HasPrefix(got, "alpha=") {
		t.Errorf("formatPayload keys not sorted: %s", got)
	}
	if !strings.Contains(got, "midpoint=x") {
		t.Errorf("formatPayload missing middle key: %s", got)
	}
}

// TestFormatPayloadScalarFloat64IntegerRendersBare pins the
// summary's payload-render contract: a float64 whose fractional
// part is zero (the common shape audit payloads land as via the
// generated client's `map[string]interface{}` decoder) renders as
// a bare integer rather than `7.000000`. Exact-precision payload
// bytes are preserved via `--json` (see show_test/query_test).
func TestFormatPayloadScalarFloat64IntegerRendersBare(t *testing.T) {
	if got := formatPayloadScalar(float64(7)); got != "7" {
		t.Errorf("integer-valued float should render bare; got %q", got)
	}
	if got := formatPayloadScalar(0.5); got != "0.5" {
		t.Errorf("fractional float: got %q", got)
	}
}

// TestFormatPayloadScalarJSONNumberRendersExact — `json.Number`
// values (from test fixtures that explicitly use UseNumber()) keep
// their exact decimal representation.
func TestFormatPayloadScalarJSONNumberRendersExact(t *testing.T) {
	n := json.Number("9007199254740993")
	if got := formatPayloadScalar(n); got != "9007199254740993" {
		t.Errorf("json.Number precision lost; got %q", got)
	}
}
