// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

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
	if ConnectorID != "nsx-rest-4.2" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "nsx-rest-4.2")
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
	got, err := loadParamsFlag(`{"domain-id":"default"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["domain-id"] != "default" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	if err := os.WriteFile(path, []byte(`{"security-policy-id":"pol-1"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil || got["security-policy-id"] != "pol-1" {
		t.Fatalf("loadParamsFlag @file: err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil || !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("expected parse error; got %v", err)
	}
}

// ---------- decodeNsxListResult ----------

func TestDecodeNsxListResultResultsWrapped(t *testing.T) {
	raw := json.RawMessage(`{"results":[{"id":"seg-1","display_name":"web"},{"id":"seg-2","display_name":"db"}],"result_count":2}`)
	entries, err := decodeNsxListResult(raw)
	if err != nil {
		t.Fatalf("decodeNsxListResult wrapped: %v", err)
	}
	if len(entries) != 2 || entries[0]["id"] != "seg-1" {
		t.Fatalf("results-wrapped decode: got %+v", entries)
	}
}

func TestDecodeNsxListResultBareArray(t *testing.T) {
	raw := json.RawMessage(`[{"id":"tz-1"},{"id":"tz-2"}]`)
	entries, err := decodeNsxListResult(raw)
	if err != nil {
		t.Fatalf("decodeNsxListResult bare: %v", err)
	}
	if len(entries) != 2 || entries[1]["id"] != "tz-2" {
		t.Fatalf("bare-array decode: got %+v", entries)
	}
}

func TestDecodeNsxListResultEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		entries, err := decodeNsxListResult(raw)
		if err != nil || entries != nil {
			t.Fatalf("decodeNsxListResult empty: err=%v entries=%v", err, entries)
		}
	}
}

// ---------- renderers ----------

func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/api/v1/node",
		Result:     json.RawMessage(`{"node_version":"4.2.1","kernel_version":"4.2.1.0.0","node_uuid":"deadbeef-1234","hostname":"nsxmgr-rdc"}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "nsx-rest-4.2", "4.2.1", "nsxmgr-rdc", "deadbeef-1234"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "session expired"
	r := &CallResult{
		Status:     "error",
		OpID:       "GET:/api/v1/node",
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

func TestPrintNodeList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"results":[{"id":"transport-node-1","display_name":"esx-01","node_deployment_info":{"resource_type":"EsxiNode"}}],"result_count":1}`),
		DurationMs: 10.0,
	}
	var buf bytes.Buffer
	printNodeList(&buf, r)
	out := buf.String()
	for _, want := range []string{"transport-node-1", "esx-01", "EsxiNode"} {
		if !strings.Contains(out, want) {
			t.Errorf("printNodeList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintNodeListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`{"results":[],"result_count":0}`)}
	var buf bytes.Buffer
	printNodeList(&buf, r)
	if !strings.Contains(buf.String(), "(0 transport nodes)") {
		t.Errorf("empty list should announce 0 nodes; got:\n%s", buf.String())
	}
}

func TestPrintSegmentList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"results":[{"id":"seg-1","display_name":"web-seg","transport_zone_path":"/infra/sites/default/enforcement-points/default/transport-zones/tz-1"}],"result_count":1}`),
		DurationMs: 8.0,
	}
	var buf bytes.Buffer
	printSegmentList(&buf, r)
	out := buf.String()
	for _, want := range []string{"seg-1", "web-seg", "tz-1"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSegmentList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintClusterStatus(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"mgmt_cluster_status":{"status":"STABLE"},"control_cluster_status":{"status":"STABLE"}}`),
		DurationMs: 3.0,
	}
	var buf bytes.Buffer
	printClusterStatus(&buf, r)
	out := buf.String()
	for _, want := range []string{"STABLE", "mgmt_cluster", "control_cluster"} {
		if !strings.Contains(out, want) {
			t.Errorf("printClusterStatus missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintFirewallPolicyList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"results":[{"id":"pol-app","display_name":"app-tier","category":"Application"}],"result_count":1}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printFirewallPolicyList(&buf, r)
	out := buf.String()
	for _, want := range []string{"pol-app", "app-tier", "Application"} {
		if !strings.Contains(out, want) {
			t.Errorf("printFirewallPolicyList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintFirewallRuleList(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		Result:     json.RawMessage(`{"results":[{"id":"rule-1","display_name":"allow-http","action":"ALLOW"}],"result_count":1}`),
		DurationMs: 4.0,
	}
	var buf bytes.Buffer
	printFirewallRuleList(&buf, r)
	out := buf.String()
	for _, want := range []string{"rule-1", "allow-http", "ALLOW"} {
		if !strings.Contains(out, want) {
			t.Errorf("printFirewallRuleList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "List NSX segments"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/policy/api/v1/infra/segments", Summary: &summary, FusedScore: 0.987},
		},
		QueryDurationMs: 12.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "list segments", r)
	out := buf.String()
	for _, want := range []string{"nsx-rest-4.2", "list segments", "1 hit(s)", "GET:/policy/api/v1/infra/segments", "List NSX segments"} {
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

// TestDispatchOpBakesConnectorID — pins that connector_id="nsx-rest-4.2"
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
			if body.ConnectorID != "nsx-rest-4.2" {
				t.Errorf("connector_id: got %q want nsx-rest-4.2", body.ConnectorID)
			}
			if body.OpID != "GET:/api/v1/node" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/api/v1/node",
				Result: json.RawMessage(`{"node_version":"4.2.1","hostname":"nsx-test"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Call(context.Background(), srv.URL, "GET:/api/v1/node", "rdc-nsx", nil)
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
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "GET:/api/v1/node"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "GET:/api/v1/node", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchFirewallPolicySendsScope — firewall policy list passes
// domain-id in params map so the backend can substitute the path template.
func TestDispatchFirewallPolicySendsScope(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			domainID, _ := body.Params["domain-id"].(string)
			if domainID != "my-domain" {
				t.Errorf("domain-id: got %q want %q", domainID, "my-domain")
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	const opID = "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies"
	params := map[string]any{"domain-id": "my-domain"}
	if _, err := conn.Call(context.Background(), srv.URL, opID, "rdc-nsx", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchFirewallRuleListSendsPolicyAndScope — firewall rule list
// passes both domain-id and security-policy-id.
func TestDispatchFirewallRuleListSendsPolicyAndScope(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			domainID, _ := body.Params["domain-id"].(string)
			policyID, _ := body.Params["security-policy-id"].(string)
			if domainID != "default" {
				t.Errorf("domain-id: got %q", domainID)
			}
			if policyID != "policy-app-tier" {
				t.Errorf("security-policy-id: got %q", policyID)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	const opID = "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules"
	params := map[string]any{
		"domain-id":          "default",
		"security-policy-id": "policy-app-tier",
	}
	if _, err := conn.Call(context.Background(), srv.URL, opID, "rdc-nsx", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestErrOpErrorIsSentinel — pins the exported sentinel.
func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatal("errOpError should be non-nil")
	}
}
