// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

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

func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "sddc-rest-9.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "sddc-rest-9.0")
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
	got, err := loadParamsFlag(`{"id":"domain-mgmt"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["id"] != "domain-mgmt" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	if err := os.WriteFile(path, []byte(`{"domainId":"domain-wld01"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil || got["domainId"] != "domain-wld01" {
		t.Fatalf("loadParamsFlag @file: err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil || !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("expected parse error; got %v", err)
	}
}

// ---------- decodeElementsResult ----------

func TestDecodeElementsResultElementsWrapped(t *testing.T) {
	raw := json.RawMessage(`{"elements":[{"id":"mgmt","name":"MGMT","type":"MANAGEMENT"},{"id":"wld01","name":"WLD-01","type":"WORKLOAD"}],"pageMetadata":{"totalElements":2}}`)
	entries, err := decodeElementsResult(raw)
	if err != nil {
		t.Fatalf("decodeElementsResult wrapped: %v", err)
	}
	if len(entries) != 2 || entries[0]["id"] != "mgmt" {
		t.Fatalf("elements-wrapped decode: got %+v", entries)
	}
}

func TestDecodeElementsResultBareArray(t *testing.T) {
	raw := json.RawMessage(`[{"id":"pool-01"},{"id":"pool-02"}]`)
	entries, err := decodeElementsResult(raw)
	if err != nil {
		t.Fatalf("decodeElementsResult bare: %v", err)
	}
	if len(entries) != 2 || entries[1]["id"] != "pool-02" {
		t.Fatalf("bare-array decode: got %+v", entries)
	}
}

func TestDecodeElementsResultEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		entries, err := decodeElementsResult(raw)
		if err != nil || entries != nil {
			t.Fatalf("decodeElementsResult empty: err=%v entries=%v", err, entries)
		}
	}
}

// ---------- renderers ----------

func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/v1/releases/system",
		Result:     json.RawMessage(`{"version":"9.0.0.0-24000000","releaseDate":"2026-01-15","description":"VMware Cloud Foundation 9.0","bom":[{"componentType":"VCENTER","componentVersion":"8.0.3"}]}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "sddc-rest-9.0", "9.0.0.0-24000000", "2026-01-15", "VCENTER", "8.0.3"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "connector timeout"
	r := &CallResult{
		Status:     "error",
		OpID:       "GET:/v1/releases/system",
		Error:      &errMsg,
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "connector timeout"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintManagerList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"elements":[{"id":"sddc-01","fqdn":"sddc-01.test","version":"9.0.0.0","domain":{"id":"domain-mgmt","name":"MGMT"}}]}`),
		DurationMs: 10.0,
	}
	var buf bytes.Buffer
	printManagerList(&buf, r)
	out := buf.String()
	for _, want := range []string{"sddc-01", "sddc-01.test", "MGMT"} {
		if !strings.Contains(out, want) {
			t.Errorf("printManagerList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintManagerListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`{"elements":[]}`)}
	var buf bytes.Buffer
	printManagerList(&buf, r)
	if !strings.Contains(buf.String(), "(0 appliances)") {
		t.Errorf("empty list should announce 0 appliances; got:\n%s", buf.String())
	}
}

func TestPrintDomainList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"elements":[{"id":"domain-mgmt","name":"MGMT","type":"MANAGEMENT"},{"id":"domain-wld01","name":"WLD-01","type":"WORKLOAD"}]}`),
		DurationMs: 8.0,
	}
	var buf bytes.Buffer
	printDomainList(&buf, r)
	out := buf.String()
	for _, want := range []string{"domain-mgmt", "MGMT", "MANAGEMENT", "WLD-01", "WORKLOAD"} {
		if !strings.Contains(out, want) {
			t.Errorf("printDomainList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintDomainInfo(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"id":"domain-mgmt","name":"MGMT","type":"MANAGEMENT","vcenters":[{"id":"vc-01","fqdn":"vc-01.test"}],"nsxtCluster":{"id":"nsx-01","vipFqdn":"nsx-01.test"},"clusters":[{"id":"cluster-01","name":"Cluster-01"}],"ssoId":"vsphere.local"}`),
		DurationMs: 6.0,
	}
	var buf bytes.Buffer
	printDomainInfo(&buf, r)
	out := buf.String()
	for _, want := range []string{"domain-mgmt", "MGMT", "MANAGEMENT", "vc-01.test", "nsx-01.test", "Cluster-01", "vsphere.local"} {
		if !strings.Contains(out, want) {
			t.Errorf("printDomainInfo missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintClusterList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"elements":[{"id":"cluster-mgmt-01","name":"Cluster-MGMT-01","primaryDatastoreType":"VMFS_FC","domainId":"domain-mgmt"}]}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printClusterList(&buf, r)
	out := buf.String()
	for _, want := range []string{"cluster-mgmt-01", "Cluster-MGMT-01", "VMFS_FC"} {
		if !strings.Contains(out, want) {
			t.Errorf("printClusterList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintHostList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"elements":[{"id":"host-01","fqdn":"esx-01.test","esxiVersion":"8.0.3","status":"ASSIGNED"}]}`),
		DurationMs: 7.0,
	}
	var buf bytes.Buffer
	printHostList(&buf, r)
	out := buf.String()
	for _, want := range []string{"host-01", "esx-01.test", "8.0.3", "ASSIGNED"} {
		if !strings.Contains(out, want) {
			t.Errorf("printHostList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintHostListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`{"elements":[]}`)}
	var buf bytes.Buffer
	printHostList(&buf, r)
	if !strings.Contains(buf.String(), "(0 hosts)") {
		t.Errorf("empty list should announce 0 hosts; got:\n%s", buf.String())
	}
}

func TestPrintNetworkPoolList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"elements":[{"id":"pool-01","name":"NetworkPool-01"}]}`),
		DurationMs: 4.0,
	}
	var buf bytes.Buffer
	printNetworkPoolList(&buf, r)
	out := buf.String()
	for _, want := range []string{"pool-01", "NetworkPool-01"} {
		if !strings.Contains(out, want) {
			t.Errorf("printNetworkPoolList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintBundleList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"elements":[{"id":"bundle-9-0-1","version":"9.0.1.0","isCompliant":false,"applicabilityStatus":"APPLICABLE","description":"VCF 9.0.1 update"}]}`),
		DurationMs: 3.0,
	}
	var buf bytes.Buffer
	printBundleList(&buf, r)
	out := buf.String()
	for _, want := range []string{"bundle-9-0-1", "9.0.1.0", "APPLICABLE"} {
		if !strings.Contains(out, want) {
			t.Errorf("printBundleList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintWorkflowList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"elements":[{"id":"task-01","status":"Successful","name":"Expand WLD-01","type":"WORKLOAD_DOMAIN_EXPAND"}]}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printWorkflowList(&buf, r)
	out := buf.String()
	for _, want := range []string{"task-01", "Successful", "Expand WLD-01"} {
		if !strings.Contains(out, want) {
			t.Errorf("printWorkflowList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "List VCF domains"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/v1/domains", Summary: &summary, FusedScore: 0.987},
			{OpID: "GET:/v1/domains/{id}", Summary: nil, FusedScore: 0.413},
		},
		QueryDurationMs: 12.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "list domains", r)
	out := buf.String()
	for _, want := range []string{"sddc-rest-9.0", "list domains", "2 hit(s)", "GET:/v1/domains", "List VCF domains", "0.987"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSearchTable missing %q in:\n%s", want, out)
		}
	}
}

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

// ---------- errOpError sentinel ----------

func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatal("errOpError should be non-nil")
	}
}

// ---------- HTTP wire helpers ----------

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

// TestDispatchOpBakesConnectorID — pins that connector_id="sddc-rest-9.0"
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
			if body.ConnectorID != "sddc-rest-9.0" {
				t.Errorf("connector_id: got %q want sddc-rest-9.0", body.ConnectorID)
			}
			if body.OpID != "GET:/v1/releases/system" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/v1/releases/system",
				Result: json.RawMessage(`{"version":"9.0.0.0"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Call(context.Background(), srv.URL, "GET:/v1/releases/system", "rdc-sddc", nil)
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
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "GET:/v1/domains"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "GET:/v1/domains", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchDomainInfoSendsIDParam — domain info verb passes id in params
// so the backend substitutes the GET:/v1/domains/{id} path template.
func TestDispatchDomainInfoSendsIDParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			domainID, _ := body.Params["id"].(string)
			if domainID != "domain-mgmt" {
				t.Errorf("id param: got %q want %q", domainID, "domain-mgmt")
			}
			if body.OpID != "GET:/v1/domains/{id}" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID,
				Result: json.RawMessage(`{"id":"domain-mgmt","name":"MGMT","type":"MANAGEMENT"}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"id": "domain-mgmt"}
	if _, err := conn.Call(context.Background(), srv.URL, "GET:/v1/domains/{id}", "rdc-sddc", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchWorkflowListSendsStatusFilter — workflow list passes status in params.
func TestDispatchWorkflowListSendsStatusFilter(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			status, _ := body.Params["status"].(string)
			if status != "In_Progress" {
				t.Errorf("status param: got %q want In_Progress", status)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID,
				Result: json.RawMessage(`{"elements":[]}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"status": "In_Progress"}
	if _, err := conn.Call(context.Background(), srv.URL, "GET:/v1/tasks", "rdc-sddc", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestSearchSendsConnectorIDPreBaked — search wrapper pre-bakes
// connector_id="sddc-rest-9.0" into the query string.
func TestSearchSendsConnectorIDPreBaked(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/operations/search": func(w http.ResponseWriter, r *http.Request) {
			if got := r.URL.Query().Get("connector_id"); got != "sddc-rest-9.0" {
				t.Errorf("connector_id: got %q", got)
			}
			if got := r.URL.Query().Get("q"); got != "list domains" {
				t.Errorf("q: got %q", got)
			}
			writeJSON(t, w, 200, searchResponse{
				Hits: []searchHit{{OpID: "GET:/v1/domains", FusedScore: 1.0}},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Search(context.Background(), srv.URL, "list domains", "", 10)
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(r.Hits) != 1 || r.Hits[0].OpID != "GET:/v1/domains" {
		t.Fatalf("unexpected search response: %+v", r)
	}
}

// TestNewRootCmdAssemblesAllVerbs pins the full sddc-manager verb tree shape.
func TestNewRootCmdAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"about":        true,
		"manager":      true,
		"domain":       true,
		"cluster":      true,
		"host":         true,
		"network-pool": true,
		"bundle":       true,
		"workflow":     true,
		"operation":    true,
	}
	for _, c := range root.Commands() {
		delete(want, c.Name())
	}
	if len(want) > 0 {
		t.Errorf("sddc-manager tree missing top-level verbs: %v", want)
	}
}

// TestDomainSubtreeAssemblesAllVerbs pins the `sddc-manager domain` sub-tree.
func TestDomainSubtreeAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() == "domain" {
			subnames := map[string]bool{}
			for _, sub := range c.Commands() {
				subnames[sub.Name()] = true
			}
			for _, want := range []string{"list", "info"} {
				if !subnames[want] {
					t.Errorf("domain sub-tree missing %q; got %v", want, subnames)
				}
			}
			return
		}
	}
	t.Fatalf("domain sub-tree not found")
}

// TestOperationSubtreeAssemblesAllVerbs pins the `sddc-manager operation` sub-tree.
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
					t.Errorf("operation sub-tree missing %q; got %v", want, subnames)
				}
			}
			return
		}
	}
	t.Fatalf("operation sub-tree not found")
}

// TestFlatListVerbsHaveListSubcommand pins single-verb sub-trees.
func TestFlatListVerbsHaveListSubcommand(t *testing.T) {
	root := NewRootCmd()
	for _, parent := range []string{"manager", "cluster", "host", "network-pool", "bundle", "workflow"} {
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
			t.Errorf("sddc-manager %s missing 'list' sub-verb", parent)
		}
	}
}
