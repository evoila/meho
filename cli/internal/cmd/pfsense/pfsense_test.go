// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

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

// ---------- helper tests (pure-function) ----------

// TestConnectorIDIsFrozen pins the pre-baked connector_id constant.
// Every verb file dispatches against this value; a regression here
// would silently rebind every alias verb to a different connector.
// The id encodes the registry-v2 natural key triple
// `("pfsense", "2.7", "pfsense-ssh")` per parse_connector_id's grammar.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "pfsense-ssh-2.7" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "pfsense-ssh-2.7")
	}
}

// TestTruncatePassthroughAndCut covers the rune-aware truncate helper.
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

// TestNormaliseURLBasic mirrors the bind9 sibling — trailing-slash
// trimming + reject-empty are the load-bearing properties.
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

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any wrapping error) maps to AuthExpired; everything else maps
// to Unexpected.
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

// ---------- decode helpers ----------

func TestDecodeFlatResultHappy(t *testing.T) {
	raw := json.RawMessage(`{"vendor":"netgate","version":"2.7.2"}`)
	got, err := decodeFlatResult(raw)
	if err != nil {
		t.Fatalf("decodeFlatResult: %v", err)
	}
	if got["vendor"] != "netgate" || got["version"] != "2.7.2" {
		t.Fatalf("decoded: %+v", got)
	}
}

func TestDecodeFlatResultNullAndEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		got, err := decodeFlatResult(raw)
		if err != nil {
			t.Fatalf("decodeFlatResult empty: %v", err)
		}
		if got != nil {
			t.Fatalf("empty should be nil map; got %+v", got)
		}
	}
}

func TestDecodeRowsResultHappy(t *testing.T) {
	raw := json.RawMessage(`{"rows":[{"action":"pass","direction":"in","rule":"pass in quick on em0 all"}],"total":1}`)
	rows, err := decodeRowsResult(raw)
	if err != nil {
		t.Fatalf("decodeRowsResult: %v", err)
	}
	if len(rows) != 1 || rows[0]["action"] != "pass" {
		t.Fatalf("decoded: %+v", rows)
	}
}

func TestDecodeRowsResultEmpty(t *testing.T) {
	raw := json.RawMessage(`{"rows":[],"total":0}`)
	rows, err := decodeRowsResult(raw)
	if err != nil {
		t.Fatalf("decodeRowsResult: %v", err)
	}
	if len(rows) != 0 {
		t.Fatalf("expected empty rows; got %+v", rows)
	}
}

// TestSplitLinesBasic — splitLines handles the basic cases including
// empty string, single line, trailing newline.
func TestSplitLinesBasic(t *testing.T) {
	tests := []struct {
		in   string
		want []string
	}{
		{"", nil},
		{"abc", []string{"abc"}},
		{"a\nb\nc", []string{"a", "b", "c"}},
		{"a\nb\n", []string{"a", "b"}},
	}
	for _, tt := range tests {
		got := splitLines(tt.in)
		if len(got) != len(tt.want) {
			t.Errorf("splitLines(%q): len=%d want %d; got %v", tt.in, len(got), len(tt.want), got)
			continue
		}
		for i := range got {
			if got[i] != tt.want[i] {
				t.Errorf("splitLines(%q)[%d]=%q want %q", tt.in, i, got[i], tt.want[i])
			}
		}
	}
}

// ---------- dispatcher wire-shape tests ----------

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
// connector_id="pfsense-ssh-2.7" pre-baked. A regression here would
// silently rebind every alias verb to a different connector.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "pfsense-ssh-2.7" {
				t.Errorf("connector_id: got %q want pfsense-ssh-2.7", body.ConnectorID)
			}
			if body.OpID != "pfsense.about" {
				t.Errorf("op_id: got %q want pfsense.about", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "pfsense.about",
				Result: json.RawMessage(`{"vendor":"netgate"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "pfsense.about", "pfsense-hetzner-dc", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
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
			if tgt == nil || tgt["name"] != "pfsense-hetzner-dc" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "pfsense.about"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "pfsense.about", "pfsense-hetzner-dc", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchOpEmptyTargetSendsNullTarget — an empty target slug must
// result in `"target": null` on the wire (not `{"name": ""}`).
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
				t.Errorf("empty target slug should serialise as null; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "pfsense.version"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "pfsense.version", "", nil); err != nil {
		t.Fatalf("dispatchOp empty target: %v", err)
	}
}

// TestAllOpsUseCanonicalOpIDs — pin the 8 canonical pfSense op_ids
// that the CLI dispatches. Any drift here surfaces as a test failure
// rather than a silent 404 from the backplane op registry.
func TestAllOpsUseCanonicalOpIDs(t *testing.T) {
	expectedOps := []string{
		"pfsense.about",
		"pfsense.version",
		"pfsense.firewall.rules",
		"pfsense.firewall.state",
		"pfsense.nat.rules",
		"pfsense.interface.list",
		"pfsense.gateway.list",
		"pfsense.config.show",
	}

	// Build a mock server that records which op_ids were dispatched.
	dispatched := make(map[string]bool)
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			dispatched[body.OpID] = true
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: body.OpID,
				Result: json.RawMessage(`{"rows":[],"total":0}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	for _, opID := range expectedOps {
		if _, err := dispatchOp(context.Background(), srv.URL, opID, "pfsense-test", nil); err != nil {
			t.Fatalf("dispatchOp %s: %v", opID, err)
		}
	}
	for _, opID := range expectedOps {
		if !dispatched[opID] {
			t.Errorf("op_id %q was not dispatched", opID)
		}
	}
}

// TestNewRootCmdHasExpectedSubcommands — the root command must expose
// the expected verb names so `meho pfsense --help` lists them.
func TestNewRootCmdHasExpectedSubcommands(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"about":    false,
		"version":  false,
		"firewall": false,
		"nat":      false,
		"network":  false,
		"config":   false,
	}
	for _, sub := range root.Commands() {
		want[sub.Name()] = true
	}
	for name, found := range want {
		if !found {
			t.Errorf("root command is missing subcommand %q", name)
		}
	}
}

// TestFirewallHasRulesAndState — the `firewall` sub-command must have
// `rules` and `state` sub-verbs.
func TestFirewallHasRulesAndState(t *testing.T) {
	fw := newFirewallCmd()
	subs := make(map[string]bool)
	for _, s := range fw.Commands() {
		subs[s.Name()] = true
	}
	for _, name := range []string{"rules", "state"} {
		if !subs[name] {
			t.Errorf("firewall is missing sub-verb %q", name)
		}
	}
}

// TestNetworkHasInterfaceAndGateway — the `network` sub-command must
// have `interface` and `gateway` sub-verbs.
func TestNetworkHasInterfaceAndGateway(t *testing.T) {
	net := newNetworkCmd()
	subs := make(map[string]bool)
	for _, s := range net.Commands() {
		subs[s.Name()] = true
	}
	for _, name := range []string{"interface", "gateway"} {
		if !subs[name] {
			t.Errorf("network is missing sub-verb %q", name)
		}
	}
}

// TestNatHasRules — the `nat` sub-command must have a `rules` sub-verb.
func TestNatHasRules(t *testing.T) {
	nat := newNatCmd()
	subs := make(map[string]bool)
	for _, s := range nat.Commands() {
		subs[s.Name()] = true
	}
	if !subs["rules"] {
		t.Errorf("nat is missing sub-verb 'rules'")
	}
}

// TestConfigHasShow — the `config` sub-command must have a `show` sub-verb.
func TestConfigHasShow(t *testing.T) {
	cfg := newConfigCmd()
	subs := make(map[string]bool)
	for _, s := range cfg.Commands() {
		subs[s.Name()] = true
	}
	if !subs["show"] {
		t.Errorf("config is missing sub-verb 'show'")
	}
}

// TestPrintAboutRendersFields — printAbout writes the canonical field
// set; regression against a future field-name drift.
func TestPrintAboutRendersFields(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "pfsense.about",
		Result:     json.RawMessage(`{"vendor":"netgate","product":"pfsense","version":"2.7.2","build":"pfSense-CE-2.7.2-RELEASE-amd64","kernel":"FreeBSD 14.0-RELEASE-p6"}`),
		DurationMs: 42,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"netgate", "pfsense", "2.7.2", "FreeBSD"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout output missing %q; got:\n%s", want, out)
		}
	}
}

// TestPrintFirewallRulesRendersTable — printFirewallRules writes the
// header row and one data row for a single-rule result.
func TestPrintFirewallRulesRendersTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "pfsense.firewall.rules",
		Result: json.RawMessage(`{"rows":[{"action":"pass","direction":"in","rule":"pass in quick on em0 all"}],"total":1}`),
	}
	var buf bytes.Buffer
	printFirewallRules(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "ACTION") || !strings.Contains(out, "pass") {
		t.Errorf("printFirewallRules output unexpected; got:\n%s", out)
	}
}

// TestPrintFirewallStateTruncatesAt20 — printFirewallState caps the
// human render at 20 rows and appends a truncation note.
func TestPrintFirewallStateTruncatesAt20(t *testing.T) {
	rows := make([]map[string]any, 25)
	for i := range rows {
		rows[i] = map[string]any{
			"proto": "tcp", "iface": "em0",
			"src": "1.2.3.4:1000", "direction": "->", "dst": "5.6.7.8:80",
		}
	}
	rowsJSON, _ := json.Marshal(map[string]any{"rows": rows, "total": 25})
	r := &CallResult{
		Status: "ok",
		OpID:   "pfsense.firewall.state",
		Result: json.RawMessage(rowsJSON),
	}
	var buf bytes.Buffer
	printFirewallState(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "more rows") {
		t.Errorf("expected truncation note for 25 rows; got:\n%s", out)
	}
}

// TestPrintNetworkGatewayDefaultFlag — printNetworkGateway renders
// "YES" for the default gateway.
func TestPrintNetworkGatewayDefaultFlag(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "pfsense.gateway.list",
		Result: json.RawMessage(`{"rows":[{"name":"WAN_DHCP","interface":"wan","gateway":"192.168.1.1","defaultgw":true,"descr":"WAN gateway"}],"total":1}`),
	}
	var buf bytes.Buffer
	printNetworkGateway(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "YES") {
		t.Errorf("expected 'YES' for default gateway; got:\n%s", out)
	}
}

// TestPrintConfigShowTruncatesAt40Lines — printConfigShow limits the
// human render to 40 XML lines and appends a truncation note.
func TestPrintConfigShowTruncatesAt40Lines(t *testing.T) {
	// Build 50-line XML content.
	var sb strings.Builder
	sb.WriteString("<?xml version=\"1.0\"?>\n<pfsense>\n")
	for i := 0; i < 48; i++ {
		sb.WriteString("  <item>value</item>\n")
	}
	sb.WriteString("</pfsense>")
	xmlStr := sb.String()
	result := map[string]any{"config_xml": xmlStr, "length": len(xmlStr)}
	resultJSON, _ := json.Marshal(result)
	r := &CallResult{
		Status: "ok",
		OpID:   "pfsense.config.show",
		Result: json.RawMessage(resultJSON),
	}
	var buf bytes.Buffer
	printConfigShow(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "more lines") {
		t.Errorf("expected truncation note for >40 lines; got:\n%s", out)
	}
}
