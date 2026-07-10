// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcflogs

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// ---------- helper tests ----------

func TestConnectorIDIsFrozen(t *testing.T) {
	// Pins the operator-visible connector id; drift here breaks every
	// alias verb at once.
	if ConnectorID != "vrli-rest-9.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "vrli-rest-9.0")
	}
}

func TestTruncatePassthroughAndCut(t *testing.T) {
	tests := []struct {
		name   string
		in     string
		maxLen int
		want   string
	}{
		{"within budget", "abc", 5, "abc"},
		{"over budget ascii", "abcdef", 4, "abc…"},
		{"multi-byte safe", "café world", 5, "café…"},
		{"zero budget", "x", 0, ""},
	}
	for _, tt := range tests {
		if got := truncate(tt.in, tt.maxLen); got != tt.want {
			t.Errorf("%s: truncate(%q, %d) = %q; want %q", tt.name, tt.in, tt.maxLen, got, tt.want)
		}
	}
}

func TestNormaliseURLBasic(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
	if _, err := backplane.NormaliseURL("   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("empty should reject; got %v", err)
	}
}

func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrapped := &backplane.NotConfiguredError{Inner: auth.ErrConfigNotFound}
	se := backplane.ClassifyError(wrapped)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("wrapped ErrConfigNotFound should classify as auth_expired; got %+v", se)
	}
	se = backplane.ClassifyError(errors.New("parse failure"))
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected_response; got %+v", se)
	}
}

// ---------- loadParamsFlag ----------

func TestLoadParamsFlagEmpty(t *testing.T) {
	got, err := loadParamsFlag("")
	if err != nil || got != nil {
		t.Fatalf("loadParamsFlag(\"\"): err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInlineJSON(t *testing.T) {
	got, err := loadParamsFlag(`{"constraints":"text/CONTAINS+error"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["constraints"] != "text/CONTAINS+error" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	if err := os.WriteFile(path, []byte(`{"limit":"50"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil || got["limit"] != "50" {
		t.Fatalf("loadParamsFlag @file: err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil || !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("expected parse error; got %v", err)
	}
}

// ---------- decodeArrayField ----------

func TestDecodeArrayFieldWrapped(t *testing.T) {
	raw := json.RawMessage(`{"fields":[{"name":"hostname","type":"string"},{"name":"timestamp","type":"long"}]}`)
	entries, err := decodeArrayField(raw, "fields")
	if err != nil {
		t.Fatalf("decodeArrayField wrapped: %v", err)
	}
	if len(entries) != 2 || entries[0]["name"] != "hostname" {
		t.Fatalf("wrapped decode: got %+v", entries)
	}
}

func TestDecodeArrayFieldBareArray(t *testing.T) {
	raw := json.RawMessage(`[{"name":"x"},{"name":"y"}]`)
	entries, err := decodeArrayField(raw, "fields")
	if err != nil {
		t.Fatalf("decodeArrayField bare: %v", err)
	}
	if len(entries) != 2 || entries[1]["name"] != "y" {
		t.Fatalf("bare-array decode: got %+v", entries)
	}
}

func TestDecodeArrayFieldEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		entries, err := decodeArrayField(raw, "fields")
		if err != nil || entries != nil {
			t.Fatalf("decodeArrayField empty: err=%v entries=%v", err, entries)
		}
	}
}

// ---------- renderers ----------

func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/api/v2/version",
		Result:     json.RawMessage(`{"version":"9.0.0","releaseName":"VMware Aria Operations for Logs 9.0"}`),
		DurationMs: 21.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "vrli-rest-9.0", "9.0.0", "Aria Operations for Logs"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "session expired"
	r := &CallResult{
		Status:     "error",
		OpID:       "GET:/api/v2/version",
		Error:      &errMsg,
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "session expired"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintQueryRawShape(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(
			`{"events":[{"timestamp":"1747896000000","hostname":"esx-01","text":"login failure"}],` +
				`"complete":false}`,
		),
		DurationMs: 14.0,
	}
	var buf bytes.Buffer
	printQuery(&buf, r)
	out := buf.String()
	for _, want := range []string{"events:   1", "complete: false", "esx-01", "login failure"} {
		if !strings.Contains(out, want) {
			t.Errorf("printQuery missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintQueryHandleShape(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"row_count": 1234}`),
		DurationMs: 9.0,
	}
	var buf bytes.Buffer
	printQuery(&buf, r)
	out := buf.String()
	for _, want := range []string{"rows: 1234", "result-handle path"} {
		if !strings.Contains(out, want) {
			t.Errorf("printQuery handle missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintFieldList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"fields":[{"name":"hostname","type":"string","source":"static"},{"name":"text","type":"string","source":"static"}]}`),
		DurationMs: 4.0,
	}
	var buf bytes.Buffer
	printFieldList(&buf, r)
	out := buf.String()
	for _, want := range []string{"hostname", "string", "static", "text"} {
		if !strings.Contains(out, want) {
			t.Errorf("printFieldList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintFieldListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`{"fields":[]}`)}
	var buf bytes.Buffer
	printFieldList(&buf, r)
	if !strings.Contains(buf.String(), "(0 fields)") {
		t.Errorf("empty list should announce 0 fields; got:\n%s", buf.String())
	}
}

func TestPrintHostList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"hosts":[{"hostname":"esx-01.lab","sourceType":"syslog","lastReceivedTimestamp":"2026-05-22T10:00:00Z"}]}`),
		DurationMs: 6.0,
	}
	var buf bytes.Buffer
	printHostList(&buf, r)
	out := buf.String()
	for _, want := range []string{"esx-01.lab", "syslog", "2026-05-22"} {
		if !strings.Contains(out, want) {
			t.Errorf("printHostList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintContentPackList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"contentPackMetadataList":[{"namespace":"com.vmware.nsx","name":"NSX-T","contentPackVersion":"1.0.0"}]}`),
		DurationMs: 7.0,
	}
	var buf bytes.Buffer
	printContentPackList(&buf, r)
	out := buf.String()
	for _, want := range []string{"com.vmware.nsx", "NSX-T", "1.0.0"} {
		if !strings.Contains(out, want) {
			t.Errorf("printContentPackList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintAlertList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"alerts":[{"name":"high-error-rate","enabled":true,"hitCount":10}]}`),
		DurationMs: 8.0,
	}
	var buf bytes.Buffer
	printAlertList(&buf, r)
	out := buf.String()
	for _, want := range []string{"high-error-rate", "true", "10"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAlertList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintAggregated(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"bins":[{"minTimestamp":"1747896000000","value":42}]}`),
		DurationMs: 6.0,
	}
	var buf bytes.Buffer
	printAggregated(&buf, r)
	out := buf.String()
	for _, want := range []string{"1747896000000", "42"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAggregated missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "Query vRLI events"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/api/v2/events/{constraints}", Summary: &summary, FusedScore: 0.981},
		},
		QueryDurationMs: 11.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "event query", r)
	out := buf.String()
	for _, want := range []string{"vrli-rest-9.0", "event query", "1 hit(s)", "GET:/api/v2/events/{constraints}", "Query vRLI events"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSearchTable missing %q in:\n%s", want, out)
		}
	}
}

// ---------- HTTP wire shape ----------

type mockHandler = http.HandlerFunc

func mockBackplane(t *testing.T, routes map[string]mockHandler) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		if h, ok := routes[key]; ok {
			h(w, r)
			return
		}
		if h, ok := routes[""]; ok {
			h(w, r)
			return
		}
		t.Errorf("mockBackplane: unhandled route %s", key)
		w.WriteHeader(404)
	}))
}

func writeJSON(t *testing.T, w http.ResponseWriter, status int, body any) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Errorf("writeJSON marshal: %v", err)
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write(raw); err != nil {
		t.Errorf("writeJSON write: %v", err)
	}
}

func primeToken(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cfg := filepath.Join(dir, "meho", "config.json")
	if err := os.MkdirAll(filepath.Dir(cfg), 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	cfgBlob, _ := json.Marshal(map[string]string{"backplane_url": backplaneURL})
	if err := os.WriteFile(cfg, cfgBlob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: backplaneURL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
}

// TestDispatchOpBakesConnectorID — pins that connector_id="vrli-rest-9.0"
// is sent on every alias-verb dispatch.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "vrli-rest-9.0" {
				t.Errorf("connector_id: got %q want vrli-rest-9.0", body.ConnectorID)
			}
			if body.OpID != "GET:/api/v2/version" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/api/v2/version",
				Result: json.RawMessage(`{"version":"9.0.0","releaseName":"vRLI"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Call(context.Background(), srv.URL, "GET:/api/v2/version", "rdc-vrli", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestDispatchOpEmptyTargetSendsNullTarget — empty slug → null target on wire.
func TestDispatchOpEmptyTargetSendsNullTarget(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var raw map[string]any
			if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if raw["target"] != nil {
				t.Errorf("empty target should be null on wire; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "GET:/api/v2/version"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "GET:/api/v2/version", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestQueryDispatchesTypedOpWithSchemaParams — post-#2266 the verb
// dispatches the typed op vrli.event.query, whose closed
// parameter_schema accepts only constraints (string, rendered into the
// request path by build_event_query_path) and limit (integer). The
// verb must send exactly those keys: no timestamp_window (the legacy
// param the closed schema would reject with additionalProperties:false)
// and limit as a JSON number, not a string.
func TestQueryDispatchesTypedOpWithSchemaParams(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "vrli.event.query" {
				t.Errorf("op_id: got %q want vrli.event.query", body.OpID)
			}
			if got, _ := body.Params["constraints"].(string); got != "text/CONTAINS+error" {
				t.Errorf("constraints: got %q", got)
			}
			// limit is a JSON number on the wire (schema type integer);
			// encoding/json decodes it into float64 in a map[string]any.
			if got, ok := body.Params["limit"].(float64); !ok || got != 100 {
				t.Errorf("limit: got %v (%T) want 100 (number)", body.Params["limit"], body.Params["limit"])
			}
			// The closed schema rejects unknown keys — the verb must not
			// send the retired timestamp_window param.
			if _, present := body.Params["timestamp_window"]; present {
				t.Errorf("timestamp_window must not be sent to the typed op; params=%v", body.Params)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newQueryCmd()
	cmd.SetArgs([]string{"text/CONTAINS+error",
		"--target", "rdc-vrli",
		"--limit", "100",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("query Execute: %v", err)
	}
}

// TestQueryEmptyConstraintsAllowed — constraints positional may be omitted;
// the appliance accepts an empty trailing path segment.
func TestQueryEmptyConstraintsAllowed(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if got, _ := body.Params["constraints"].(string); got != "" {
				t.Errorf("empty constraints expected; got %q", got)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newQueryCmd()
	cmd.SetArgs([]string{
		"--target", "rdc-vrli",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("query Execute: %v", err)
	}
}

// TestQueryRejectsNegativeLimit — --limit=-1 surfaces a clean error,
// not a panic, before any HTTP round-trip.
func TestQueryRejectsNegativeLimit(t *testing.T) {
	cmd := newQueryCmd()
	cmd.SetArgs([]string{
		"--target", "rdc-vrli",
		"--limit", "-1",
		"--backplane", "https://nowhere.test.invalid",
	})
	err := cmd.Execute()
	if err == nil {
		t.Fatal("expected error for --limit=-1")
	}
}

// TestErrOpErrorIsSentinel — pins the exported sentinel.
func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatal("errOpError should be non-nil")
	}
}
