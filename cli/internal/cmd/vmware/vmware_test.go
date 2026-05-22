// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

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

// ---------- helper tests (pure-function) ----------

// TestTruncatePassthroughAndCut covers the rune-aware truncate
// helper. Same shape as the operation / connector siblings —
// duplicated because cmd/vmware can't import them without an import
// cycle.
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

// TestNormaliseURLBasic mirrors the operation / connector siblings —
// trailing-slash trimming + reject-empty are the load-bearing
// properties.
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

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any wrapping error) maps to AuthExpired; everything else maps
// to Unexpected.
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

// TestConnectorIDIsFrozen pins the pre-baked connector_id constant.
// Every verb file dispatches against this value; a regression here
// would silently rebind every alias verb to a different connector.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "vmware-rest-9.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "vmware-rest-9.0")
	}
}

// ---------- loadParamsFlag ----------

// TestLoadParamsFlagEmpty — empty flag value returns (nil, nil) so
// runners can omit the params key from the body.
func TestLoadParamsFlagEmpty(t *testing.T) {
	got, err := loadParamsFlag("")
	if err != nil {
		t.Fatalf("loadParamsFlag(\"\"): %v", err)
	}
	if got != nil {
		t.Fatalf("loadParamsFlag(\"\") should be nil; got %v", got)
	}
}

// TestLoadParamsFlagInlineJSON — happy-path inline JSON object.
func TestLoadParamsFlagInlineJSON(t *testing.T) {
	got, err := loadParamsFlag(`{"name":"web-01","power_on":true}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["name"] != "web-01" || got["power_on"] != true {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

// TestLoadParamsFlagFileReference — `@<file>` form reads + parses.
func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "spec.json")
	if err := os.WriteFile(path, []byte(`{"vm":"vm-101"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil {
		t.Fatalf("loadParamsFlag @file: %v", err)
	}
	if got["vm"] != "vm-101" {
		t.Fatalf("file params not parsed; got %v", got)
	}
}

// TestLoadParamsFlagInvalidJSONReportsError — malformed JSON
// surfaces a `parse params JSON` error string.
func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil {
		t.Fatalf("expected parse error; got nil")
	}
	if !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("error should name parse failure; got %v", err)
	}
}

// ---------- splitOnce ----------

// TestSplitOnce covers the inline `k=v` parser used by --filter.
func TestSplitOnce(t *testing.T) {
	cases := []struct {
		in, sep, wantBefore, wantAfter string
		wantOK                         bool
	}{
		{"k=v", "=", "k", "v", true},
		{"k=v=v2", "=", "k", "v=v2", true},
		{"kv", "=", "kv", "", false},
		{"", "=", "", "", false},
	}
	for _, tc := range cases {
		b, a, ok := splitOnce(tc.in, tc.sep)
		if b != tc.wantBefore || a != tc.wantAfter || ok != tc.wantOK {
			t.Errorf("splitOnce(%q, %q) = (%q, %q, %v); want (%q, %q, %v)",
				tc.in, tc.sep, b, a, ok, tc.wantBefore, tc.wantAfter, tc.wantOK)
		}
	}
}

// ---------- buildListParams ----------

// TestBuildListParamsTypedFlags — --names + --power-state land
// under the canonical filter.* keys.
func TestBuildListParamsTypedFlags(t *testing.T) {
	got, err := buildListParams([]string{"web-01", "web-02"}, []string{"POWERED_ON"}, nil)
	if err != nil {
		t.Fatalf("buildListParams: %v", err)
	}
	names, _ := got["filter.names"].([]string)
	if len(names) != 2 || names[0] != "web-01" {
		t.Errorf("filter.names: got %v", got["filter.names"])
	}
	ps, _ := got["filter.power_states"].([]string)
	if len(ps) != 1 || ps[0] != "POWERED_ON" {
		t.Errorf("filter.power_states: got %v", got["filter.power_states"])
	}
}

// TestBuildListParamsRawKV — --filter k=v lands verbatim.
func TestBuildListParamsRawKV(t *testing.T) {
	got, err := buildListParams(nil, nil, []string{"clusters=domain-c1"})
	if err != nil {
		t.Fatalf("buildListParams: %v", err)
	}
	if got["clusters"] != "domain-c1" {
		t.Errorf("clusters: got %v", got)
	}
}

// TestBuildListParamsRawKVRejectsMalformed — bare value (no `=`)
// errors so the operator sees the typo client-side.
func TestBuildListParamsRawKVRejectsMalformed(t *testing.T) {
	_, err := buildListParams(nil, nil, []string{"no-equals"})
	if err == nil || !strings.Contains(err.Error(), "k=v") {
		t.Fatalf("malformed filter should reject with k=v hint; got %v", err)
	}
}

// TestBuildListParamsEmptyReturnsNil — no flags means no params on
// the wire (omitting the key, not sending an empty object).
func TestBuildListParamsEmptyReturnsNil(t *testing.T) {
	got, err := buildListParams(nil, nil, nil)
	if err != nil {
		t.Fatalf("buildListParams: %v", err)
	}
	if got != nil {
		t.Fatalf("empty flags should produce nil map; got %v", got)
	}
}

// ---------- isMoid / moidFieldForKind ----------

// TestIsMoidPerKind — each kind's grammar matches its expected
// moid shape and rejects everything else.
func TestIsMoidPerKind(t *testing.T) {
	cases := []struct {
		kind, input string
		want        bool
	}{
		{"vm", "vm-101", true},
		{"vm", "web-01", false},
		{"host", "host-23", true},
		{"host", "esxi-01", false},
		{"cluster", "domain-c123", true},
		{"cluster", "dc-prod-01", false},
		{"datacenter", "datacenter-1", true},
		{"datastore", "datastore-7", true},
		{"network", "network-5", true},
		{"unknown", "anything", false},
	}
	for _, c := range cases {
		if got := isMoid(c.kind, c.input); got != c.want {
			t.Errorf("isMoid(%q, %q) = %v; want %v", c.kind, c.input, got, c.want)
		}
	}
}

// TestMoidFieldForKindAllKinds pins the per-kind moid field name.
func TestMoidFieldForKindAllKinds(t *testing.T) {
	for _, k := range []string{"vm", "host", "cluster", "datacenter", "datastore", "network"} {
		if got := moidFieldForKind(k); got != k {
			t.Errorf("moidFieldForKind(%q) = %q; want %q", k, got, k)
		}
	}
}

// ---------- decodeListResult ----------

// TestDecodeListResultBareArray covers the canonical vSphere 9.0
// response shape.
func TestDecodeListResultBareArray(t *testing.T) {
	raw := json.RawMessage(`[{"vm":"vm-101","name":"web-01"},{"vm":"vm-102","name":"web-02"}]`)
	entries, err := decodeListResult(raw)
	if err != nil {
		t.Fatalf("decodeListResult bare: %v", err)
	}
	if len(entries) != 2 || entries[0]["vm"] != "vm-101" {
		t.Fatalf("bare-array decode: got %+v", entries)
	}
}

// TestDecodeListResultValueWrapped covers the legacy 6.x/7.x wrap
// shape that some 9.0 endpoints still emit.
func TestDecodeListResultValueWrapped(t *testing.T) {
	raw := json.RawMessage(`{"value":[{"vm":"vm-101","name":"web-01"}]}`)
	entries, err := decodeListResult(raw)
	if err != nil {
		t.Fatalf("decodeListResult wrapped: %v", err)
	}
	if len(entries) != 1 || entries[0]["name"] != "web-01" {
		t.Fatalf("value-wrapped decode: got %+v", entries)
	}
}

// TestDecodeListResultEmpty — null / empty raw returns nil with no
// error so the renderer can show "(0 …)".
func TestDecodeListResultEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		entries, err := decodeListResult(raw)
		if err != nil {
			t.Fatalf("decodeListResult empty: %v", err)
		}
		if entries != nil {
			t.Fatalf("empty raw should decode to nil; got %+v", entries)
		}
	}
}

// ---------- decodeAboutPayload ----------

// TestDecodeAboutPayloadBare covers the canonical 9.0 shape.
func TestDecodeAboutPayloadBare(t *testing.T) {
	raw := []byte(`{"product":"VMware vCenter Server","version":"9.0.0","build":"12345","api_type":"VirtualCenter"}`)
	got, ok := decodeAboutPayload(raw)
	if !ok {
		t.Fatalf("decodeAboutPayload bare: ok=false")
	}
	if got.Product != "VMware vCenter Server" || got.Version != "9.0.0" {
		t.Fatalf("decoded payload: %+v", got)
	}
}

// TestDecodeAboutPayloadWrapped covers the legacy wrap.
func TestDecodeAboutPayloadWrapped(t *testing.T) {
	raw := []byte(`{"value":{"product":"VCSA","version":"7.0.3","build":"22222","api_type":"VirtualCenter"}}`)
	got, ok := decodeAboutPayload(raw)
	if !ok || got.Product != "VCSA" {
		t.Fatalf("decoded wrapped: ok=%v payload=%+v", ok, got)
	}
}

// TestDecodeAboutPayloadShapeDrift — unknown shape returns ok=false
// so the renderer falls back to raw JSON dump.
func TestDecodeAboutPayloadShapeDrift(t *testing.T) {
	raw := []byte(`{"unexpected":true}`)
	_, ok := decodeAboutPayload(raw)
	if ok {
		t.Fatalf("decodeAboutPayload should reject unknown shape")
	}
}

// ---------- renderers ----------

// TestPrintAboutHumanFormat — happy-path render with all four
// product fields present.
func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/api/about",
		Result:     json.RawMessage(`{"product":"VMware vCenter Server","version":"9.0.0","build":"12345","api_type":"VirtualCenter"}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "vmware-rest-9.0", "VMware vCenter Server", "9.0.0", "12345", "VirtualCenter"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintAboutErrorRendersErrorString — status=error with a
// non-nil Error pointer surfaces the error string + extras.
func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "unknown_op: GET:/api/about"
	r := &CallResult{
		Status:     "error",
		OpID:       "GET:/api/about",
		Error:      &errMsg,
		Extras:     json.RawMessage(`{"known_op_count":7}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "unknown_op", "extras:", "known_op_count"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintVMListTable — happy-path render with 2 VMs.
func TestPrintVMListTable(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/vcenter/vm",
		Result:     json.RawMessage(`[{"vm":"vm-101","name":"web-01","power_state":"POWERED_ON","cpu_count":4,"memory_size_MiB":8192},{"vm":"vm-102","name":"web-02","power_state":"POWERED_OFF","cpu_count":2,"memory_size_MiB":4096}]`),
		DurationMs: 12.0,
	}
	var buf bytes.Buffer
	printVMList(&buf, r)
	out := buf.String()
	for _, want := range []string{"vm-101", "web-01", "POWERED_ON", "vm-102", "POWERED_OFF", "moid", "name", "power"} {
		if !strings.Contains(out, want) {
			t.Errorf("printVMList missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintVMListEmpty — zero VMs renders the count line.
func TestPrintVMListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`[]`)}
	var buf bytes.Buffer
	printVMList(&buf, r)
	if !strings.Contains(buf.String(), "(0 VMs)") {
		t.Errorf("empty list should announce 0 VMs; got:\n%s", buf.String())
	}
}

// TestPrintHostList — happy-path render.
func TestPrintHostList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`[{"host":"host-23","name":"esxi-01","connection_state":"CONNECTED","power_state":"POWERED_ON"}]`),
	}
	var buf bytes.Buffer
	printHostList(&buf, r)
	out := buf.String()
	for _, want := range []string{"host-23", "esxi-01", "CONNECTED"} {
		if !strings.Contains(out, want) {
			t.Errorf("printHostList missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintClusterList — happy-path render.
func TestPrintClusterList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`[{"cluster":"domain-c123","name":"dc-prod","drs_enabled":true,"ha_enabled":false}]`),
	}
	var buf bytes.Buffer
	printClusterList(&buf, r)
	out := buf.String()
	for _, want := range []string{"domain-c123", "dc-prod"} {
		if !strings.Contains(out, want) {
			t.Errorf("printClusterList missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintSearchTable — happy-path render with 2 hits.
func TestPrintSearchTable(t *testing.T) {
	summary := "List VMs"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/vcenter/vm", Summary: &summary, FusedScore: 0.987},
			{OpID: "POST:/vcenter/vm", Summary: nil, FusedScore: 0.413},
		},
		QueryDurationMs: 42.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "list vms", r)
	out := buf.String()
	for _, want := range []string{"vmware-rest-9.0", "list vms", "2 hit(s)", "GET:/vcenter/vm", "List VMs", "0.987"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSearchTable missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintSearchTableEmpty — zero hits skips the table header.
func TestPrintSearchTableEmpty(t *testing.T) {
	r := &searchResponse{Hits: nil, QueryDurationMs: 1.0}
	var buf bytes.Buffer
	printSearchTable(&buf, "no-match", r)
	out := buf.String()
	if !strings.Contains(out, "0 hit(s)") {
		t.Errorf("empty search should announce 0 hits; got:\n%s", out)
	}
	if strings.Contains(out, "op_id") {
		t.Errorf("empty search should skip header; got:\n%s", out)
	}
}

// TestStrDerefNilEmptyOtherwiseValue — Optional[str] handling.
func TestStrDerefNilEmptyOtherwiseValue(t *testing.T) {
	if got := strDeref(nil); got != "" {
		t.Fatalf("strDeref(nil): got %q", got)
	}
	v := "hello"
	if got := strDeref(&v); got != "hello" {
		t.Fatalf("strDeref(&v): got %q", got)
	}
}

// ---------- errOpError sentinel ----------

func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatalf("errOpError should be non-nil")
	}
	if errors.Is(errOpError, errors.New("other")) {
		t.Fatalf("errOpError should not match arbitrary errors")
	}
}

// ---------- HTTP wire shape (mocked) ----------

type mockHandler = http.HandlerFunc

// mockBackplane stands up an httptest.Server that routes by
// `<METHOD> <path>` keys. The empty key acts as a catch-all. Same
// shape as the connector sibling's helper.
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

// primeToken installs an in-memory token store with a usable bearer
// for the mocked backplane URL. Mirrors the connector sibling's
// primeToken helper.
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

// TestDispatchOpBakesConnectorID — every verb dispatches with
// connector_id="vmware-rest-9.0" pre-baked. This test pins that the
// dispatcher helper writes the canonical connector_id into the
// CallOperationBody — a regression here would silently rebind every
// alias verb to a different connector.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "vmware-rest-9.0" {
				t.Errorf("connector_id: got %q want vmware-rest-9.0", body.ConnectorID)
			}
			if body.OpID != "GET:/api/about" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/api/about",
				Result: json.RawMessage(`{"product":"vCenter"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Call(context.Background(), srv.URL, "GET:/api/about", "rdc-vcenter", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestDispatchOpEmptyTargetSendsNullTarget — the empty target slug
// must surface as `null` on the wire so the dispatcher's resolver
// can fall through to the no-target path for typed handlers that
// don't need a target.
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
				t.Errorf("target should be null when targetSlug empty; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "x"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "x", "", nil); err != nil {
		t.Fatalf("dispatchOp empty-target: %v", err)
	}
}

// TestDispatchOpTargetSlugWrappedAsName — a non-empty target slug
// must surface as `{"name": "<slug>"}` so the dispatcher's resolver
// pulls it under the canonical TargetRef shape.
func TestDispatchOpTargetSlugWrappedAsName(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var raw map[string]any
			if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			tgt, _ := raw["target"].(map[string]any)
			if tgt == nil || tgt["name"] != "rdc-vcenter" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "x"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "x", "rdc-vcenter", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestSearchSendsConnectorIDPreBaked — the search wrapper pre-bakes
// connector_id="vmware-rest-9.0" into the query string so operators
// don't type it.
func TestSearchSendsConnectorIDPreBaked(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/operations/search": func(w http.ResponseWriter, r *http.Request) {
			if got := r.URL.Query().Get("connector_id"); got != "vmware-rest-9.0" {
				t.Errorf("connector_id: got %q", got)
			}
			if got := r.URL.Query().Get("query"); got != "list VMs" {
				t.Errorf("query: got %q", got)
			}
			writeJSON(t, w, 200, searchResponse{
				Hits: []searchHit{{OpID: "GET:/vcenter/vm", FusedScore: 1.0}},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := getSearch(context.Background(), srv.URL, "list VMs", "", 10)
	if err != nil {
		t.Fatalf("getSearch: %v", err)
	}
	if len(r.Hits) != 1 || r.Hits[0].OpID != "GET:/vcenter/vm" {
		t.Fatalf("unexpected search response: %+v", r)
	}
}

// TestResolveNameAlreadyMoid — moid input passes through without
// the resolve round-trip.
func TestResolveNameAlreadyMoid(t *testing.T) {
	// Use a server that errors if hit so the test fails noisily on
	// any accidental round-trip.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Errorf("resolveName should not round-trip for a moid input")
		w.WriteHeader(500)
	}))
	defer srv.Close()
	primeToken(t, srv.URL)

	moid, err := resolveName(context.Background(), srv.URL, "rdc-vcenter", "vm", "vm-101")
	if err != nil {
		t.Fatalf("resolveName moid passthrough: %v", err)
	}
	if moid != "vm-101" {
		t.Fatalf("moid passthrough: got %q want vm-101", moid)
	}
}

// TestResolveNameSingleMatch — happy-path name → moid resolution.
func TestResolveNameSingleMatch(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "GET:/vcenter/vm" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["filter.names"] != "web-prod-01" {
				t.Errorf("filter.names: got %v", body.Params["filter.names"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/vcenter/vm",
				Result: json.RawMessage(`[{"vm":"vm-101","name":"web-prod-01"}]`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	moid, err := resolveName(context.Background(), srv.URL, "rdc-vcenter", "vm", "web-prod-01")
	if err != nil {
		t.Fatalf("resolveName: %v", err)
	}
	if moid != "vm-101" {
		t.Fatalf("resolved moid: got %q want vm-101", moid)
	}
}

// TestResolveNameNotFound — zero matches produces a clear error.
func TestResolveNameNotFound(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/vcenter/vm",
				Result: json.RawMessage(`[]`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	_, err := resolveName(context.Background(), srv.URL, "rdc-vcenter", "vm", "ghost-vm")
	if err == nil {
		t.Fatalf("expected not-found error")
	}
	if !strings.Contains(err.Error(), "no vm named") {
		t.Errorf("error should name kind + value; got %v", err)
	}
}

// TestResolveNameAmbiguous — multiple matches lists candidates.
func TestResolveNameAmbiguous(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/vcenter/vm",
				Result: json.RawMessage(`[{"vm":"vm-101","name":"web-01"},{"vm":"vm-204","name":"web-01"}]`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	_, err := resolveName(context.Background(), srv.URL, "rdc-vcenter", "vm", "web-01")
	if err == nil {
		t.Fatalf("expected ambiguous error")
	}
	if !strings.Contains(err.Error(), "candidates") || !strings.Contains(err.Error(), "vm-101") || !strings.Contains(err.Error(), "vm-204") {
		t.Errorf("ambiguous error should list candidates; got %v", err)
	}
}

// TestResolveNameDispatcherError — dispatcher status==error during
// resolve round-trip surfaces with the dispatcher's error string.
func TestResolveNameDispatcherError(t *testing.T) {
	errMsg := "vcenter timeout"
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "error",
				OpID:   "GET:/vcenter/vm",
				Error:  &errMsg,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	_, err := resolveName(context.Background(), srv.URL, "rdc-vcenter", "vm", "web-01")
	if err == nil {
		t.Fatalf("expected dispatcher-error wrap")
	}
	if !strings.Contains(err.Error(), "vcenter timeout") {
		t.Errorf("error should propagate dispatcher message; got %v", err)
	}
}

// TestRenderCallResultUnknownStatus — anything outside the
// ok/error/denied enum surfaces as an unexpected-response
// StructuredError so main exits with code 4 instead of pretending
// nothing happened.
func TestRenderCallResultUnknownStatus(t *testing.T) {
	r := &CallResult{Status: "what", OpID: "x"}
	var buf bytes.Buffer
	cmd := newOperationCallCmd()
	cmd.SetErr(&buf)
	cmd.SetOut(&buf)
	err := conn.Render(cmd, "x", r, false, nil)
	if err == nil {
		t.Fatalf("unknown status should surface as error")
	}
	if !strings.Contains(buf.String(), "unexpected_response") && !strings.Contains(buf.String(), "invalid OperationResult") {
		t.Errorf("unknown-status render should mention unexpected_response or invalid; got:\n%s", buf.String())
	}
}

// TestNewRootCmdAssemblesAllVerbs pins the verb tree shape — the
// acceptance criteria require `meho vmware --help` to list every
// top-level verb. A regression here (missing AddCommand, typo'd
// Use) would silently drop a verb from the tree.
func TestNewRootCmdAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"about":      true,
		"vm":         true,
		"host":       true,
		"cluster":    true,
		"datacenter": true,
		"datastore":  true,
		"network":    true,
		"operation":  true,
	}
	for _, c := range root.Commands() {
		delete(want, c.Name())
	}
	if len(want) > 0 {
		t.Errorf("vmware tree missing top-level verbs: %v", want)
	}
}

// TestVMSubtreeAssemblesAllVerbs pins the `vmware vm` sub-tree.
func TestVMSubtreeAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	var vmCmd *struct{ commands func() }
	_ = vmCmd
	for _, c := range root.Commands() {
		if c.Name() == "vm" {
			subnames := map[string]bool{}
			for _, sub := range c.Commands() {
				subnames[sub.Name()] = true
			}
			for _, want := range []string{"list", "info", "create"} {
				if !subnames[want] {
					t.Errorf("vmware vm sub-tree missing %q; got %v", want, subnames)
				}
			}
			return
		}
	}
	t.Fatalf("vm sub-tree not found")
}

// TestHostSubtreeAssemblesAllVerbs pins the `vmware host` sub-tree.
func TestHostSubtreeAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() == "host" {
			subnames := map[string]bool{}
			for _, sub := range c.Commands() {
				subnames[sub.Name()] = true
			}
			for _, want := range []string{"list", "evacuate"} {
				if !subnames[want] {
					t.Errorf("vmware host sub-tree missing %q; got %v", want, subnames)
				}
			}
			return
		}
	}
	t.Fatalf("host sub-tree not found")
}

// TestClusterSubtreeAssemblesAllVerbs pins the `vmware cluster` sub-tree.
func TestClusterSubtreeAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() == "cluster" {
			subnames := map[string]bool{}
			for _, sub := range c.Commands() {
				subnames[sub.Name()] = true
			}
			for _, want := range []string{"list", "patch"} {
				if !subnames[want] {
					t.Errorf("vmware cluster sub-tree missing %q; got %v", want, subnames)
				}
			}
			return
		}
	}
	t.Fatalf("cluster sub-tree not found")
}

// TestOperationSubtreeAssemblesAllVerbs pins the `vmware operation`
// sub-tree. Both verbs are pre-scoped meta-tool wrappers.
func TestOperationSubtreeAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() == "operation" {
			subnames := map[string]bool{}
			for _, sub := range c.Commands() {
				subnames[sub.Name()] = true
			}
			for _, want := range []string{"search", "call"} {
				if !subnames[want] {
					t.Errorf("vmware operation sub-tree missing %q; got %v", want, subnames)
				}
			}
			return
		}
	}
	t.Fatalf("operation sub-tree not found")
}

// TestFlatListVerbsHaveListSubcommand pins the
// datacenter/datastore/network "list" sub-verbs.
func TestFlatListVerbsHaveListSubcommand(t *testing.T) {
	root := NewRootCmd()
	for _, parent := range []string{"datacenter", "datastore", "network"} {
		found := false
		for _, c := range root.Commands() {
			if c.Name() != parent {
				continue
			}
			for _, sub := range c.Commands() {
				if sub.Name() == "list" {
					found = true
				}
			}
		}
		if !found {
			t.Errorf("vmware %s missing 'list' sub-verb", parent)
		}
	}
}
