// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

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
	if ConnectorID != "hetzner-rest-2026.04" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "hetzner-rest-2026.04")
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
	got, err := loadParamsFlag(`{"server-ip":"1.2.3.4"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["server-ip"] != "1.2.3.4" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	if err := os.WriteFile(path, []byte(`{"id":"4321"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil || got["id"] != "4321" {
		t.Fatalf("loadParamsFlag @file: err=%v got=%v", err, got)
	}
}

func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil || !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("expected parse error; got %v", err)
	}
}

// ---------- decodeRobotList ----------

func TestDecodeRobotListBareArray(t *testing.T) {
	raw := json.RawMessage(`[{"server_ip":"1.2.3.1"},{"server_ip":"1.2.3.2"}]`)
	items, err := decodeRobotList(raw)
	if err != nil {
		t.Fatalf("decodeRobotList bare array: %v", err)
	}
	if len(items) != 2 || items[0]["server_ip"] != "1.2.3.1" {
		t.Fatalf("bare-array decode: got %+v", items)
	}
}

func TestDecodeRobotListWrappedObject(t *testing.T) {
	raw := json.RawMessage(`{"server":[{"server_ip":"1.2.3.1"},{"server_ip":"1.2.3.2"}]}`)
	items, err := decodeRobotList(raw)
	if err != nil {
		t.Fatalf("decodeRobotList wrapped object: %v", err)
	}
	if len(items) != 2 {
		t.Fatalf("wrapped-object decode: got %+v", items)
	}
}

func TestDecodeRobotListEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		items, err := decodeRobotList(raw)
		if err != nil || items != nil {
			t.Fatalf("decodeRobotList empty: err=%v items=%v", err, items)
		}
	}
}

func TestDecodeRobotListEmptyArray(t *testing.T) {
	items, err := decodeRobotList(json.RawMessage(`[]`))
	if err != nil {
		t.Fatalf("decodeRobotList []: %v", err)
	}
	if len(items) != 0 {
		t.Fatalf("expected empty slice; got %v", items)
	}
}

// ---------- renderers ----------

func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "GET:/query",
		Result:     json.RawMessage(`{"api_version":"1.0","account_id":"robot-acc-001"}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "hetzner-rest-2026.04", "1.0", "robot-acc-001"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "auth_failed"
	r := &CallResult{
		Status:     "error",
		OpID:       "GET:/query",
		Error:      &errMsg,
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "auth_failed"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintServerList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`[{"server":{"server_ip":"1.2.3.1","server_number":100001,"product":"AX41-NVMe","dc":"FSN1-DC14","status":"ready"}}]`),
	}
	var buf bytes.Buffer
	printServerList(&buf, r)
	out := buf.String()
	for _, want := range []string{"1.2.3.1", "100001", "AX41-NVMe", "FSN1-DC14", "ready"} {
		if !strings.Contains(out, want) {
			t.Errorf("printServerList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintServerListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`[]`)}
	var buf bytes.Buffer
	printServerList(&buf, r)
	if !strings.Contains(buf.String(), "(0 servers)") {
		t.Errorf("empty list should announce 0 servers; got:\n%s", buf.String())
	}
}

func TestPrintIPList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`[{"ip":{"ip":"1.2.3.1","server_ip":"1.2.3.1","locked":false}},{"ip":{"ip":"1.2.3.2","server_ip":"1.2.3.1","locked":true}}]`),
	}
	var buf bytes.Buffer
	printIPList(&buf, r)
	out := buf.String()
	for _, want := range []string{"1.2.3.1", "1.2.3.2", "true"} {
		if !strings.Contains(out, want) {
			t.Errorf("printIPList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSSHKeyList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`[{"key":{"fingerprint":"aa:bb:cc","name":"my-key","type":"ED25519","size":256}}]`),
	}
	var buf bytes.Buffer
	printSSHKeyList(&buf, r)
	out := buf.String()
	for _, want := range []string{"aa:bb:cc", "my-key", "ED25519", "256"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSSHKeyList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSSHKeyListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", Result: json.RawMessage(`[]`)}
	var buf bytes.Buffer
	printSSHKeyList(&buf, r)
	if !strings.Contains(buf.String(), "(0 SSH keys)") {
		t.Errorf("empty list should announce 0 SSH keys; got:\n%s", buf.String())
	}
}

func TestPrintVswitchList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`[{"vswitch":{"id":4321,"name":"canary-vswitch","vlan":4000,"cancelled":false,"server":[{"server_ip":"1.2.3.1","server_number":100001,"status":"ready"}]}}]`),
	}
	var buf bytes.Buffer
	printVswitchList(&buf, r)
	out := buf.String()
	for _, want := range []string{"4321", "canary-vswitch", "4000"} {
		if !strings.Contains(out, want) {
			t.Errorf("printVswitchList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintFailoverList(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		Result: json.RawMessage(`[{"failover":{"ip":"1.2.3.10","server_ip":"1.2.3.1","active_server_ip":"1.2.3.2","netmask":"255.255.255.255"}}]`),
	}
	var buf bytes.Buffer
	printFailoverList(&buf, r)
	out := buf.String()
	for _, want := range []string{"1.2.3.10", "1.2.3.1", "1.2.3.2", "ROUTED AWAY"} {
		if !strings.Contains(out, want) {
			t.Errorf("printFailoverList missing %q in:\n%s", want, out)
		}
	}
}

func TestPrintSearchTable(t *testing.T) {
	summary := "List dedicated servers"
	r := &searchResponse{
		Hits: []searchHit{
			{OpID: "GET:/server", Summary: &summary, FusedScore: 0.987},
		},
		QueryDurationMs: 12.0,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "list servers", r)
	out := buf.String()
	for _, want := range []string{"hetzner-rest-2026.04", "list servers", "1 hit(s)", "GET:/server", "List dedicated servers"} {
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

// TestDispatchOpBakesConnectorID — pins that connector_id="hetzner-rest-2026.04"
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
			if body.ConnectorID != "hetzner-rest-2026.04" {
				t.Errorf("connector_id: got %q want hetzner-rest-2026.04", body.ConnectorID)
			}
			if body.OpID != "GET:/query" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "GET:/query",
				Result: json.RawMessage(`{"api_version":"1.0"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "GET:/query", "rdc-robot", nil)
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
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "GET:/query"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "GET:/query", "", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchServerInfoSendsParams — server info passes server-ip in params.
func TestDispatchServerInfoSendsParams(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			serverIP, _ := body.Params["server-ip"].(string)
			if serverIP != "1.2.3.1" {
				t.Errorf("server-ip: got %q want %q", serverIP, "1.2.3.1")
			}
			if body.OpID != "GET:/server/{server-ip}" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"server-ip": "1.2.3.1"}
	if _, err := dispatchOp(context.Background(), srv.URL, "GET:/server/{server-ip}", "rdc-robot", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchVswitchInfoSendsID — vswitch info passes id in params.
func TestDispatchVswitchInfoSendsID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			id, _ := body.Params["id"].(string)
			if id != "4321" {
				t.Errorf("id: got %q want %q", id, "4321")
			}
			if body.OpID != "GET:/vswitch/{id}" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{"id": "4321"}
	if _, err := dispatchOp(context.Background(), srv.URL, "GET:/vswitch/{id}", "rdc-robot", params); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestErrOpErrorIsSentinel — pins the exported sentinel.
func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatal("errOpError should be non-nil")
	}
}

// TestNewRootCmdHasExpectedSubcommands — verifies the verb tree is fully wired.
func TestNewRootCmdHasExpectedSubcommands(t *testing.T) {
	root := NewRootCmd()
	names := map[string]bool{}
	for _, c := range root.Commands() {
		names[c.Name()] = true
	}
	for _, expected := range []string{"about", "server", "ip", "subnet", "vswitch", "failover", "rdns", "ssh-key", "operation"} {
		if !names[expected] {
			t.Errorf("expected subcommand %q under hetzner-robot; commands: %v", expected, names)
		}
	}
}
