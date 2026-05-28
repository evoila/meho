// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

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

// TestConnectorIDIsFrozen pins the pre-baked connector_id constant.
// A regression here would silently rebind every alias verb to a
// different connector. Match `VROPS_CONNECTOR_ID` on the backend
// side.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "vrops-rest-9.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "vrops-rest-9.0")
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
	got, err := loadParamsFlag(`{"resourceKind":"VirtualMachine","pageSize":50}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["resourceKind"] != "VirtualMachine" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	if err := os.WriteFile(path, []byte(`{"id":"vrops-resource-1"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil || got["id"] != "vrops-resource-1" {
		t.Fatalf("loadParamsFlag @file: err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil || !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("expected parse error; got %v", err)
	}
}

// ---------- decodeVropsListResult ----------

// vROps wraps each list payload under a noun-specific key
// (resourceList / alerts / alertDefinitions / symptoms /
// recommendations / superMetrics). The decoder must unwrap from the
// specified key.
func TestDecodeVropsListResultResourceListWrapper(t *testing.T) {
	raw := json.RawMessage(`{"resourceList":[{"identifier":"r-1"},{"identifier":"r-2"}],"pageInfo":{"totalCount":2}}`)
	entries, err := decodeVropsListResult(raw, "resourceList")
	if err != nil {
		t.Fatalf("decodeVropsListResult resourceList: %v", err)
	}
	if len(entries) != 2 || entries[0]["identifier"] != "r-1" {
		t.Fatalf("resourceList decode: got %+v", entries)
	}
}

func TestDecodeVropsListResultAlertsWrapper(t *testing.T) {
	raw := json.RawMessage(`{"alerts":[{"alertId":"a-1","status":"ACTIVE"}],"pageInfo":{"totalCount":1}}`)
	entries, err := decodeVropsListResult(raw, "alerts")
	if err != nil {
		t.Fatalf("decodeVropsListResult alerts: %v", err)
	}
	if len(entries) != 1 || entries[0]["alertId"] != "a-1" {
		t.Fatalf("alerts decode: got %+v", entries)
	}
}

func TestDecodeVropsListResultBareArrayFallback(t *testing.T) {
	// Bare-array fallback path — not the canonical shape but kept
	// for defence against future spec drift.
	raw := json.RawMessage(`[{"identifier":"r-1"}]`)
	entries, err := decodeVropsListResult(raw, "resourceList")
	if err != nil {
		t.Fatalf("decodeVropsListResult bare: %v", err)
	}
	if len(entries) != 1 || entries[0]["identifier"] != "r-1" {
		t.Fatalf("bare-array decode: got %+v", entries)
	}
}

func TestDecodeVropsListResultEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		entries, err := decodeVropsListResult(raw, "resourceList")
		if err != nil || entries != nil {
			t.Fatalf("decodeVropsListResult empty: err=%v entries=%v", err, entries)
		}
	}
}

// TestVropsResourceNameNested checks the resource-list helper that
// pulls `resourceKey.name` out of the nested vROps shape.
func TestVropsResourceNameNested(t *testing.T) {
	e := vropsEntry{
		"resourceKey": map[string]any{
			"name":            "vm-canary-00",
			"resourceKindKey": "VirtualMachine",
		},
	}
	if got := vropsResourceName(e); got != "vm-canary-00" {
		t.Errorf("vropsResourceName: got %q want vm-canary-00", got)
	}
	if got := vropsResourceKindKey(e); got != "VirtualMachine" {
		t.Errorf("vropsResourceKindKey: got %q want VirtualMachine", got)
	}
}

func TestVropsResourceNameMissing(t *testing.T) {
	// Missing resourceKey → empty string, not panic.
	if got := vropsResourceName(vropsEntry{}); got != "" {
		t.Errorf("vropsResourceName missing key: got %q", got)
	}
	if got := vropsResourceKindKey(vropsEntry{"resourceKey": "not-a-map"}); got != "" {
		t.Errorf("vropsResourceKindKey non-map: got %q", got)
	}
}

// ---------- renderers ----------

func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "GET:/suite-api/api/versions/current",
		Result: json.RawMessage(
			`{"releaseName":"9.0.0.1.23456789","buildNumber":23456789,"humanlyReadableReleaseName":"VMware Aria Operations 9.0"}`,
		),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "vrops-rest-9.0", "9.0.0.1.23456789", "23456789", "VMware Aria Operations 9.0"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "auth failed"
	r := &CallResult{
		Status:     "error",
		OpID:       "GET:/suite-api/api/versions/current",
		Error:      &errMsg,
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "auth failed"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintResourceList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"resourceList":[{"identifier":"r-1","resourceKey":{"name":"vm-01","resourceKindKey":"VirtualMachine","adapterKindKey":"VMWARE"}}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printResourceList(&buf, r)
	out := buf.String()
	for _, want := range []string{"r-1", "vm-01", "VirtualMachine", "identifier", "name", "kind"} {
		if !strings.Contains(out, want) {
			t.Errorf("printResourceList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintResourceListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`{"resourceList":[]}`)}
	var buf bytes.Buffer
	printResourceList(&buf, r)
	if !strings.Contains(buf.String(), "(0 resources)") {
		t.Errorf("empty resource list should announce 0; got:\n%s", buf.String())
	}
}

func TestPrintResourceGet(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"identifier":"r-1","resourceKey":{"name":"vm-01","resourceKindKey":"VirtualMachine"},"resourceStatusStates":[{"resourceStatus":"DATA_RECEIVING","resourceState":"STARTED"}]}`),
	}
	var buf bytes.Buffer
	printResourceGet(&buf, r)
	out := buf.String()
	for _, want := range []string{"r-1", "vm-01", "VirtualMachine", "DATA_RECEIVING", "STARTED"} {
		if !strings.Contains(out, want) {
			t.Errorf("printResourceGet missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintAlertList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"alerts":[{"alertId":"a-1","alertDefinitionName":"CPU high","resourceId":"res-1","alertLevel":3,"status":"ACTIVE"}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printAlertList(&buf, r)
	out := buf.String()
	// The header advertises a `resourceId` column (alert.go Long text);
	// pin that the renderer actually emits it and the row's value.
	for _, want := range []string{"a-1", "CPU high", "ACTIVE", "3", "resourceId", "res-1"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAlertList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintAlertListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`{"alerts":[]}`)}
	var buf bytes.Buffer
	printAlertList(&buf, r)
	if !strings.Contains(buf.String(), "(0 alerts)") {
		t.Errorf("empty alert list should announce 0; got:\n%s", buf.String())
	}
}

func TestPrintAlertDefinitionList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"alertDefinitions":[{"id":"AlertDef-1","name":"CPU","adapterKindKey":"VMWARE","resourceKindKey":"VirtualMachine"}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printAlertDefinitionList(&buf, r)
	out := buf.String()
	for _, want := range []string{"AlertDef-1", "CPU", "VMWARE", "VirtualMachine"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAlertDefinitionList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSymptomList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"symptoms":[{"id":"sym-1","symptomDefinitionName":"CPU breach","resourceId":"res-1","severity":"WARNING"}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printSymptomList(&buf, r)
	out := buf.String()
	// The header advertises a `resourceId` column (symptom.go Long text);
	// pin that the renderer actually emits it and the row's value.
	for _, want := range []string{"sym-1", "CPU breach", "WARNING", "resourceId", "res-1"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSymptomList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintRecommendationList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"recommendations":[{"id":"rec-1","description":"Reduce workload","actionId":""}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printRecommendationList(&buf, r)
	out := buf.String()
	for _, want := range []string{"rec-1", "Reduce workload"} {
		if !strings.Contains(out, want) {
			t.Errorf("printRecommendationList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSupermetricList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"superMetrics":[{"id":"sm-1","name":"cpu-ratio","formula":"a/b"}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printSupermetricList(&buf, r)
	out := buf.String()
	for _, want := range []string{"sm-1", "cpu-ratio", "a/b"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSupermetricList missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintRecommendationListFirstLineOnly pins the "first line"
// contract advertised in the recommendation list help text — a
// multi-line description must be collapsed so embedded newlines
// don't spill across multiple table rows.
func TestPrintRecommendationListFirstLineOnly(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"recommendations":[{"id":"rec-1","description":"Reduce workload\nthen restart\nthe VM","actionId":""}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printRecommendationList(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "Reduce workload") {
		t.Errorf("printRecommendationList missing first line in:\n%s", out)
	}
	if strings.Contains(out, "then restart") || strings.Contains(out, "the VM") {
		t.Errorf("printRecommendationList must render only the first line; got:\n%s", out)
	}
}

// TestPrintSupermetricListFirstLineOnly pins the "first line"
// contract advertised in the super-metric list help text — a
// multi-line formula must be collapsed so embedded newlines don't
// spill across multiple table rows.
func TestPrintSupermetricListFirstLineOnly(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`{"superMetrics":[{"id":"sm-1","name":"cpu-ratio","formula":"$This, metric=cpu\n+ $This, metric=ready"}],"pageInfo":{"totalCount":1}}`),
	}
	var buf bytes.Buffer
	printSupermetricList(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "$This, metric=cpu") {
		t.Errorf("printSupermetricList missing first line in:\n%s", out)
	}
	if strings.Contains(out, "metric=ready") {
		t.Errorf("printSupermetricList must render only the first line; got:\n%s", out)
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "List vROps resources"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/suite-api/api/resources", Summary: &summary, FusedScore: 0.987},
		},
		QueryDurationMs: 12.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "list resources", r)
	out := buf.String()
	for _, want := range []string{"vrops-rest-9.0", "list resources", "1 hit(s)", "GET:/suite-api/api/resources", "List vROps resources"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSearchTable missing %q in:\n%s", want, out)
		}
	}
}

func TestStrDerefNilEmptyOtherwiseValue(t *testing.T) {
	if got := strDeref(nil); got != "" {
		t.Fatalf("strDeref(nil): got %q", got)
	}
	v := "hello"
	if got := strDeref(&v); got != "hello" {
		t.Fatalf("strDeref(&v): got %q", got)
	}
}

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

// TestDispatchOpBakesConnectorID — every verb dispatches with
// connector_id="vrops-rest-9.0" pre-baked. This pins that the
// dispatcher writes the canonical connector_id into the
// CallOperationBody — a regression here would silently rebind every
// alias verb to a different connector. Mirrors the NSX / SDDC Manager
// siblings.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "vrops-rest-9.0" {
				t.Errorf("connector_id: got %q want vrops-rest-9.0", body.ConnectorID)
			}
			if body.OpID != "GET:/suite-api/api/versions/current" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/suite-api/api/versions/current",
				Result: json.RawMessage(`{"releaseName":"9.0.0"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Call(context.Background(), srv.URL, "GET:/suite-api/api/versions/current", "rdc-vrops", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

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
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "x"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "x", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

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
			if tgt == nil || tgt["name"] != "rdc-vrops" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "x"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "x", "rdc-vrops", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchResourceGetSendsIDParam — the resource-get verb passes
// the {id} path parameter through `params.id` so the dispatcher's
// `_substitute_path` can fill the template at dispatch time. Pin
// that wire shape — a regression would route every resource-get to
// the un-substituted path.
func TestDispatchResourceGetSendsIDParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "GET:/suite-api/api/resources/{id}" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["id"] != "vrops-resource-uuid" {
				t.Errorf("params.id: got %v", body.Params["id"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/suite-api/api/resources/{id}",
				Result: json.RawMessage(`{"identifier":"vrops-resource-uuid"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Call(
		context.Background(), srv.URL,
		"GET:/suite-api/api/resources/{id}",
		"rdc-vrops",
		map[string]any{"id": "vrops-resource-uuid"},
	)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestSearchSendsConnectorIDPreBaked — the search wrapper pre-bakes
// connector_id="vrops-rest-9.0" into the query string.
func TestSearchSendsConnectorIDPreBaked(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/operations/search": func(w http.ResponseWriter, r *http.Request) {
			if got := r.URL.Query().Get("connector_id"); got != "vrops-rest-9.0" {
				t.Errorf("connector_id: got %q", got)
			}
			if got := r.URL.Query().Get("query"); got != "list resources" {
				t.Errorf("query: got %q", got)
			}
			writeJSON(t, w, 200, searchResponse{
				Hits: []searchHit{{OpID: "GET:/suite-api/api/resources", FusedScore: 1.0}},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Search(context.Background(), srv.URL, "list resources", "", 10)
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(r.Hits) != 1 || r.Hits[0].OpID != "GET:/suite-api/api/resources" {
		t.Fatalf("unexpected search response: %+v", r)
	}
}

// TestRenderCallResultUnknownStatus — anything outside the
// ok/error/denied enum surfaces as an unexpected-response error.
func TestRenderCallResultUnknownStatus(t *testing.T) {
	r := &CallResult{Status: "weird", OpID: "x"}
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
// issue #837 acceptance lists about / resource / alert /
// alertdefinition / symptom / recommendation / supermetric (+
// operation). A regression here (missing AddCommand, typo'd Use)
// would silently drop a verb from the tree.
func TestNewRootCmdAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"about":           true,
		"resource":        true,
		"alert":           true,
		"alertdefinition": true,
		"symptom":         true,
		"recommendation":  true,
		"supermetric":     true,
		"operation":       true,
	}
	for _, c := range root.Commands() {
		delete(want, c.Name())
	}
	if len(want) > 0 {
		t.Errorf("vcf-operations tree missing top-level verbs: %v", want)
	}
}

// TestResourceSubtreeAssemblesListAndGet pins the resource sub-tree.
func TestResourceSubtreeAssemblesListAndGet(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() != "resource" {
			continue
		}
		subnames := map[string]bool{}
		for _, sub := range c.Commands() {
			subnames[sub.Name()] = true
		}
		for _, want := range []string{"list", "get"} {
			if !subnames[want] {
				t.Errorf("resource sub-tree missing %q; got %v", want, subnames)
			}
		}
		return
	}
	t.Fatalf("resource sub-tree not found")
}

// TestSingleListSubtreesAssembleListVerb checks that each list-only
// noun (alert / alertdefinition / symptom / recommendation /
// supermetric) carries its single “list“ sub-verb.
func TestSingleListSubtreesAssembleListVerb(t *testing.T) {
	root := NewRootCmd()
	for _, parent := range []string{"alert", "alertdefinition", "symptom", "recommendation", "supermetric"} {
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
			t.Errorf("vcf-operations %s missing 'list' sub-verb", parent)
		}
	}
}

// TestOperationSubtreeAssemblesAllVerbs pins the
// `vcf-operations operation` sub-tree.
func TestOperationSubtreeAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() != "operation" {
			continue
		}
		subnames := map[string]bool{}
		for _, sub := range c.Commands() {
			subnames[sub.Name()] = true
		}
		for _, want := range []string{"search", "call"} {
			if !subnames[want] {
				t.Errorf("operation sub-tree missing %q; got %v", want, subnames)
			}
		}
		return
	}
	t.Fatalf("operation sub-tree not found")
}
