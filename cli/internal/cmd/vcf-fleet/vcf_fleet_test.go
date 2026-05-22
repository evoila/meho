// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

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
	got, err := normaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
	if _, err := normaliseURL("   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("empty should reject; got %v", err)
	}
}

func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrapped := &errNoBackplaneConfigured{inner: auth.ErrConfigNotFound}
	se := classifyBackplaneError(wrapped)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("wrapped ErrConfigNotFound should classify as auth_expired; got %+v", se)
	}
	se = classifyBackplaneError(errors.New("parse failure"))
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected_response; got %+v", se)
	}
}

func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "fleet-rest-9.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "fleet-rest-9.0")
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
	got, err := loadParamsFlag(`{"environmentId":"env-vrops"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["environmentId"] != "env-vrops" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	if err := os.WriteFile(path, []byte(`{"dataCenterVmid":"dc-001"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil || got["dataCenterVmid"] != "dc-001" {
		t.Fatalf("loadParamsFlag @file: err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil || !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("expected parse error; got %v", err)
	}
}

// ---------- decodeListResult ----------

func TestDecodeListResultBareArray(t *testing.T) {
	raw := json.RawMessage(`[{"vmid":"dc-1","name":"primary"},{"vmid":"dc-2","name":"secondary"}]`)
	entries, err := decodeListResult(raw)
	if err != nil {
		t.Fatalf("decodeListResult bare: %v", err)
	}
	if len(entries) != 2 || entries[0]["vmid"] != "dc-1" {
		t.Fatalf("bare-array decode: got %+v", entries)
	}
}

func TestDecodeListResultDataEnvelope(t *testing.T) {
	raw := json.RawMessage(`{"data":[{"vmid":"dc-1"}]}`)
	entries, err := decodeListResult(raw)
	if err != nil {
		t.Fatalf("decodeListResult data-envelope: %v", err)
	}
	if len(entries) != 1 || entries[0]["vmid"] != "dc-1" {
		t.Fatalf("data-envelope decode: got %+v", entries)
	}
}

func TestDecodeListResultEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		entries, err := decodeListResult(raw)
		if err != nil || entries != nil {
			t.Fatalf("decodeListResult empty: err=%v entries=%v", err, entries)
		}
	}
}

func TestDecodeListResultUnknownShapeErrors(t *testing.T) {
	raw := json.RawMessage(`{"unexpected":"shape"}`)
	if _, err := decodeListResult(raw); err == nil {
		t.Fatalf("decodeListResult should error on unknown shape")
	}
}

// ---------- renderers ----------

func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       aboutOpID,
		Result:     json.RawMessage(`{"apiVersion":"8.0","productVersion":"9.0.0.0","buildNumber":"24123456","releaseDate":"2026-04-01"}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "fleet-rest-9.0", "8.0", "9.0.0.0", "24123456", "2026-04-01"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "appliance 500"
	r := &CallResult{
		Status:     "error",
		OpID:       aboutOpID,
		Error:      &errMsg,
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "appliance 500"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintDatacenterList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"vmid":"dc-001","name":"primary","type":"PRIVATE_CLOUD","city":"Vienna"}]`),
		DurationMs: 8.0,
	}
	var buf bytes.Buffer
	printDatacenterList(&buf, r)
	out := buf.String()
	for _, want := range []string{"dc-001", "primary", "PRIVATE_CLOUD", "Vienna"} {
		if !strings.Contains(out, want) {
			t.Errorf("printDatacenterList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintDatacenterListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`[]`)}
	var buf bytes.Buffer
	printDatacenterList(&buf, r)
	if !strings.Contains(buf.String(), "(0 datacenters)") {
		t.Errorf("empty list should announce 0 datacenters; got:\n%s", buf.String())
	}
}

func TestPrintVcenterList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"vmid":"vc-001","hostname":"vc-prod.lab.example.com","version":"8.0.3","buildNumber":"24001122"}]`),
		DurationMs: 7.0,
	}
	var buf bytes.Buffer
	printVcenterList(&buf, r)
	out := buf.String()
	for _, want := range []string{"vc-001", "vc-prod.lab.example.com", "8.0.3"} {
		if !strings.Contains(out, want) {
			t.Errorf("printVcenterList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintEnvironmentList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"environmentId":"env-vrops","environmentName":"vROps Prod","environmentStatus":"DEPLOY_SUCCESSFUL"}]`),
		DurationMs: 9.0,
	}
	var buf bytes.Buffer
	printEnvironmentList(&buf, r)
	out := buf.String()
	for _, want := range []string{"env-vrops", "vROps Prod", "DEPLOY_SUCCESSFUL"} {
		if !strings.Contains(out, want) {
			t.Errorf("printEnvironmentList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintEnvironmentInfo(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"environmentId":"env-vrops","environmentName":"vROps Prod","environmentStatus":"DEPLOY_SUCCESSFUL","transactionId":"txn-001","createdOn":"2026-05-01","products":[{"productId":"vrops","version":"9.0.0","status":"OK","nodes":[{"hostname":"vrops-01.lab","ipAddress":"10.1.1.10","role":"MASTER","vmStatus":"POWERED_ON"}]}]}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printEnvironmentInfo(&buf, r)
	out := buf.String()
	for _, want := range []string{"env-vrops", "vROps Prod", "DEPLOY_SUCCESSFUL", "txn-001", "vrops", "9.0.0", "vrops-01.lab", "POWERED_ON"} {
		if !strings.Contains(out, want) {
			t.Errorf("printEnvironmentInfo missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintProductList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"productId":"vrops","version":"9.0.0","status":"OK"},{"productId":"vrli","version":"9.0.1","status":"OK"}]`),
		DurationMs: 6.0,
	}
	var buf bytes.Buffer
	printProductList(&buf, r)
	out := buf.String()
	for _, want := range []string{"vrops", "9.0.0", "vrli", "9.0.1"} {
		if !strings.Contains(out, want) {
			t.Errorf("printProductList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintRequestList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`[{"vmid":"req-001","requestType":"ENVIRONMENT_CREATE","state":"COMPLETED","requestName":"Create vROps Prod"}]`),
		DurationMs: 4.0,
	}
	var buf bytes.Buffer
	printRequestList(&buf, r)
	out := buf.String()
	for _, want := range []string{"req-001", "ENVIRONMENT_CREATE", "COMPLETED", "Create vROps Prod"} {
		if !strings.Contains(out, want) {
			t.Errorf("printRequestList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintRequestInfo(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"vmid":"req-001","transactionId":"txn-009","requestName":"Upgrade vROps","requestType":"PRODUCT_UPGRADE","state":"FAILED","executionStatus":"FAILED","errorCause":"node unreachable","createdBy":"admin@local","lastUpdatedOn":"2026-05-10"}`),
		DurationMs: 3.0,
	}
	var buf bytes.Buffer
	printRequestInfo(&buf, r)
	out := buf.String()
	for _, want := range []string{"req-001", "Upgrade vROps", "PRODUCT_UPGRADE", "FAILED", "node unreachable", "admin@local", "2026-05-10"} {
		if !strings.Contains(out, want) {
			t.Errorf("printRequestInfo missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "List Fleet environments"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: environmentListOpID, Summary: &summary, FusedScore: 0.987},
		},
		QueryDurationMs: 12.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "list environments", r)
	out := buf.String()
	for _, want := range []string{"fleet-rest-9.0", "list environments", "1 hit(s)", environmentListOpID, "List Fleet environments"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSearchTable missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSearchTableEmpty(t *testing.T) {
	r := &searchResponse{Hits: nil, QueryDurationMs: 1.0}
	var buf bytes.Buffer
	printSearchTable(&buf, "no-match", r)
	if !strings.Contains(buf.String(), "0 hit(s)") {
		t.Errorf("empty search should announce 0 hits; got:\n%s", buf.String())
	}
}

// ---------- errOpError sentinel ----------

func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatal("errOpError should be non-nil")
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

// TestDispatchOpBakesConnectorID pins connector_id="fleet-rest-9.0" on every dispatch.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "fleet-rest-9.0" {
				t.Errorf("connector_id: got %q want fleet-rest-9.0", body.ConnectorID)
			}
			if body.OpID != datacenterListOpID {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   datacenterListOpID,
				Result: json.RawMessage(`[]`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, datacenterListOpID, "rdc-fleet", nil)
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
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: datacenterListOpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, datacenterListOpID, "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchVcenterListSendsDataCenterVmidParam pins that the vcenter list
// verb forwards dataCenterVmid as a path-template param.
func TestDispatchVcenterListSendsDataCenterVmidParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != vcenterListOpID {
				t.Errorf("op_id: got %q want %q", body.OpID, vcenterListOpID)
			}
			vmid, _ := body.Params["dataCenterVmid"].(string)
			if vmid != "dc-001" {
				t.Errorf("dataCenterVmid: got %q want dc-001", vmid)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID, Result: json.RawMessage(`[]`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"dataCenterVmid": "dc-001"}
	if _, err := dispatchOp(context.Background(), srv.URL, vcenterListOpID, "rdc-fleet", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchEnvironmentInfoSendsEnvironmentIDParam pins environment info routing.
func TestDispatchEnvironmentInfoSendsEnvironmentIDParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != environmentGetOpID {
				t.Errorf("op_id: got %q want %q", body.OpID, environmentGetOpID)
			}
			envID, _ := body.Params["environmentId"].(string)
			if envID != "env-vrops" {
				t.Errorf("environmentId: got %q want env-vrops", envID)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"environmentId": "env-vrops"}
	if _, err := dispatchOp(context.Background(), srv.URL, environmentGetOpID, "rdc-fleet", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchRequestInfoSendsRequestIDParam pins request info routing.
func TestDispatchRequestInfoSendsRequestIDParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			reqID, _ := body.Params["requestId"].(string)
			if reqID != "req-001" {
				t.Errorf("requestId: got %q want req-001", reqID)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"requestId": "req-001"}
	if _, err := dispatchOp(context.Background(), srv.URL, requestGetOpID, "rdc-fleet", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestSearchSendsConnectorIDPreBaked pins search wrapper connector_id pre-baking.
func TestSearchSendsConnectorIDPreBaked(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/operations/search": func(w http.ResponseWriter, r *http.Request) {
			if got := r.URL.Query().Get("connector_id"); got != "fleet-rest-9.0" {
				t.Errorf("connector_id: got %q", got)
			}
			if got := r.URL.Query().Get("query"); got != "list environments" {
				t.Errorf("query: got %q", got)
			}
			writeJSON(t, w, 200, searchResponse{
				Hits: []searchHit{{OpID: environmentListOpID, FusedScore: 1.0}},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := getSearch(context.Background(), srv.URL, "list environments", "", 10)
	if err != nil {
		t.Fatalf("getSearch: %v", err)
	}
	if len(r.Hits) != 1 || r.Hits[0].OpID != environmentListOpID {
		t.Fatalf("unexpected search response: %+v", r)
	}
}

// TestNewRootCmdAssemblesAllVerbs pins the full vcf-fleet verb tree shape.
func TestNewRootCmdAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"about":       true,
		"datacenter":  true,
		"vcenter":     true,
		"environment": true,
		"product":     true,
		"request":     true,
		"operation":   true,
	}
	for _, c := range root.Commands() {
		delete(want, c.Name())
	}
	if len(want) > 0 {
		t.Errorf("vcf-fleet tree missing top-level verbs: %v", want)
	}
}

// TestEnvironmentSubtreeAssemblesListAndInfo pins `vcf-fleet environment` shape.
func TestEnvironmentSubtreeAssemblesListAndInfo(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() != "environment" {
			continue
		}
		subnames := map[string]bool{}
		for _, sub := range c.Commands() {
			subnames[sub.Name()] = true
		}
		for _, want := range []string{"list", "info"} {
			if !subnames[want] {
				t.Errorf("environment sub-tree missing %q; got %v", want, subnames)
			}
		}
		return
	}
	t.Fatalf("environment sub-tree not found")
}

// TestRequestSubtreeAssemblesListAndInfo pins `vcf-fleet request` shape.
func TestRequestSubtreeAssemblesListAndInfo(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() != "request" {
			continue
		}
		subnames := map[string]bool{}
		for _, sub := range c.Commands() {
			subnames[sub.Name()] = true
		}
		for _, want := range []string{"list", "info"} {
			if !subnames[want] {
				t.Errorf("request sub-tree missing %q; got %v", want, subnames)
			}
		}
		return
	}
	t.Fatalf("request sub-tree not found")
}

// TestOperationSubtreeAssemblesAllVerbs pins `vcf-fleet operation` shape.
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

// TestFlatListVerbsHaveListSubcommand pins single-list-verb sub-trees.
func TestFlatListVerbsHaveListSubcommand(t *testing.T) {
	root := NewRootCmd()
	for _, parent := range []string{"datacenter", "vcenter", "product"} {
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
			t.Errorf("vcf-fleet %s missing 'list' sub-verb", parent)
		}
	}
}
