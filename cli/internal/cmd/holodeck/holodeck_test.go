// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

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
// `("holodeck", "9.0", "holodeck-ssh")` per parse_connector_id's grammar.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "holodeck-ssh-9.0" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "holodeck-ssh-9.0")
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

// TestNormaliseURLBasic mirrors the pfsense / bind9 siblings —
// trailing-slash trimming + reject-empty are the load-bearing
// properties.
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
	raw := json.RawMessage(`{"vendor":"vmware","version":"9.0.0"}`)
	got, err := decodeFlatResult(raw)
	if err != nil {
		t.Fatalf("decodeFlatResult: %v", err)
	}
	if got["vendor"] != "vmware" || got["version"] != "9.0.0" {
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
	raw := json.RawMessage(`{"rows":[{"Id":"HoloPod-001","Name":"lab-A","State":"Running"}],"total":1}`)
	rows, err := decodeRowsResult(raw)
	if err != nil {
		t.Fatalf("decodeRowsResult: %v", err)
	}
	if len(rows) != 1 || rows[0]["Id"] != "HoloPod-001" {
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
// connector_id="holodeck-ssh-9.0" pre-baked. A regression here would
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
			if body.ConnectorID != "holodeck-ssh-9.0" {
				t.Errorf("connector_id: got %q want holodeck-ssh-9.0", body.ConnectorID)
			}
			if body.OpID != "holodeck.about" {
				t.Errorf("op_id: got %q want holodeck.about", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "holodeck.about",
				Result: json.RawMessage(`{"vendor":"vmware","product":"holodeck"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "holodeck.about", "holorouter-hetzner-dc", nil)
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
			if tgt == nil || tgt["name"] != "holorouter-hetzner-dc" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "holodeck.about"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "holodeck.about", "holorouter-hetzner-dc", nil); err != nil {
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
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "holodeck.about"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "holodeck.about", "", nil); err != nil {
		t.Fatalf("dispatchOp empty target: %v", err)
	}
}

// TestAllOpsUseCanonicalOpIDs — pin the 8 canonical Holodeck op_ids
// that the CLI dispatches. Any drift here surfaces as a test failure
// rather than a silent 404 from the backplane op registry.
func TestAllOpsUseCanonicalOpIDs(t *testing.T) {
	expectedOps := []string{
		"holodeck.about",
		"holodeck.config.show",
		"holodeck.pod.list",
		"holodeck.pod.info",
		"holodeck.service.list",
		"holodeck.k8s.exec",
		"holodeck.logs.tail",
		"holodeck.networking.show",
	}

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
				Result: json.RawMessage(`{}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	for _, opID := range expectedOps {
		if _, err := dispatchOp(context.Background(), srv.URL, opID, "holodeck-test", nil); err != nil {
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
// the expected verb names so `meho holodeck --help` lists them.
func TestNewRootCmdHasExpectedSubcommands(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"about":      false,
		"config":     false,
		"pod":        false,
		"service":    false,
		"k8s":        false,
		"logs":       false,
		"networking": false,
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

// TestPodHasListAndInfo — the `pod` sub-command must have `list` and
// `info` sub-verbs.
func TestPodHasListAndInfo(t *testing.T) {
	p := newPodCmd()
	subs := make(map[string]bool)
	for _, s := range p.Commands() {
		subs[s.Name()] = true
	}
	for _, name := range []string{"list", "info"} {
		if !subs[name] {
			t.Errorf("pod is missing sub-verb %q", name)
		}
	}
}

// TestK8sHasExec — the `k8s` sub-command must have an `exec` sub-verb.
func TestK8sHasExec(t *testing.T) {
	k := newK8sCmd()
	subs := make(map[string]bool)
	for _, s := range k.Commands() {
		subs[s.Name()] = true
	}
	if !subs["exec"] {
		t.Errorf("k8s is missing sub-verb 'exec'")
	}
}

// TestLogsHasTail — the `logs` sub-command must have a `tail` sub-verb.
func TestLogsHasTail(t *testing.T) {
	l := newLogsCmd()
	subs := make(map[string]bool)
	for _, s := range l.Commands() {
		subs[s.Name()] = true
	}
	if !subs["tail"] {
		t.Errorf("logs is missing sub-verb 'tail'")
	}
}

// TestNetworkingHasShow — the `networking` sub-command must have a
// `show` sub-verb.
func TestNetworkingHasShow(t *testing.T) {
	n := newNetworkingCmd()
	subs := make(map[string]bool)
	for _, s := range n.Commands() {
		subs[s.Name()] = true
	}
	if !subs["show"] {
		t.Errorf("networking is missing sub-verb 'show'")
	}
}

// TestServiceHasList — the `service` sub-command must have a `list`
// sub-verb.
func TestServiceHasList(t *testing.T) {
	s := newServiceCmd()
	subs := make(map[string]bool)
	for _, sc := range s.Commands() {
		subs[sc.Name()] = true
	}
	if !subs["list"] {
		t.Errorf("service is missing sub-verb 'list'")
	}
}

// TestConfigHasShow — the `config` sub-command must have a `show`
// sub-verb.
func TestConfigHasShow(t *testing.T) {
	c := newConfigCmd()
	subs := make(map[string]bool)
	for _, sc := range c.Commands() {
		subs[sc.Name()] = true
	}
	if !subs["show"] {
		t.Errorf("config is missing sub-verb 'show'")
	}
}

// ---------- safety-critical: k8s.exec forwards command verbatim ----------

// TestK8sExecForwardsCommandVerbatim — the most security-critical
// invariant in the package. The CLI must pass the operator-supplied
// kubectl command verbatim in params["command"], not pre-parse or
// pre-validate. The backend handler
// (`parse_kubectl_command` in ops_read.py) is the authoritative
// safety gate; duplicating the check here would risk drift between
// CLI and MCP code paths.
//
// This test pins three guarantees:
//
//  1. A safe kubectl command (`kubectl get pods`) lands on the wire
//     in params["command"] verbatim.
//  2. A shell-metacharacter payload (`kubectl get pods; rm -rf /`)
//     ALSO lands on the wire verbatim — the CLI doesn't filter or
//     refuse it client-side, the backend's metachar reject in
//     `_SHELL_METACHARS_RE` is what fails the call (with
//     `result_connector_error`). This is intentional: a client-side
//     reject would mask whether the backend gate is firing.
//  3. The connector_id is "holodeck-ssh-9.0" so the dispatch hits
//     the correct typed-op surface (where the safety gate lives).
func TestK8sExecForwardsCommandVerbatim(t *testing.T) {
	cases := []struct {
		name string
		cmd  string
	}{
		{"safe verb", "kubectl get pods -n holodeck"},
		{"metachar payload", "kubectl get pods; rm -rf /"},
		{"complex flags", "kubectl --context=foo get pods -o yaml"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var captured map[string]any
			srv := mockBackplane(t, map[string]mockHandler{
				"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
					if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
						t.Errorf("decode body: %v", err)
						w.WriteHeader(400)
						return
					}
					writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "holodeck.k8s.exec",
						Result: json.RawMessage(`{"stdout":"","stderr":"","exit_status":0}`)})
				},
			})
			defer srv.Close()
			primeToken(t, srv.URL)

			params := map[string]any{"command": tc.cmd}
			if _, err := dispatchOp(context.Background(), srv.URL, "holodeck.k8s.exec", "holorouter-test", params); err != nil {
				t.Fatalf("dispatchOp: %v", err)
			}

			if captured["connector_id"] != "holodeck-ssh-9.0" {
				t.Errorf("connector_id drift: %v", captured["connector_id"])
			}
			gotParams, _ := captured["params"].(map[string]any)
			if gotParams == nil {
				t.Fatalf("missing params on wire: %v", captured)
			}
			if gotParams["command"] != tc.cmd {
				t.Errorf("command not forwarded verbatim: got %q want %q",
					gotParams["command"], tc.cmd)
			}
		})
	}
}

// ---------- per-verb pretty printers ----------

// TestPrintAboutRendersFields — printAbout writes the canonical field
// set; regression against a future field-name drift.
func TestPrintAboutRendersFields(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "holodeck.about",
		Result:     json.RawMessage(`{"vendor":"vmware","product":"holodeck","version":"9.0.0","build":"VMware Photon Linux 5.0","photon_version":"5.0","pod_id":"HoloPod-Alpha"}`),
		DurationMs: 42,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"vmware", "holodeck", "9.0.0", "Photon", "HoloPod-Alpha"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout output missing %q; got:\n%s", want, out)
		}
	}
}

// TestPrintPodListRendersTable — printPodList writes the header row
// and one data row for a single-pod result.
func TestPrintPodListRendersTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "holodeck.pod.list",
		Result: json.RawMessage(`{"rows":[{"Id":"HoloPod-001","Name":"lab-A","State":"Running","Network":"NSX-T-Tier-0"}],"total":1}`),
	}
	var buf bytes.Buffer
	printPodList(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "POD-ID") || !strings.Contains(out, "HoloPod-001") {
		t.Errorf("printPodList output unexpected; got:\n%s", out)
	}
}

// TestPrintPodListTruncatesAt20 — printPodList caps the human render
// at 20 rows and appends a truncation note.
func TestPrintPodListTruncatesAt20(t *testing.T) {
	rows := make([]map[string]any, 25)
	for i := range rows {
		rows[i] = map[string]any{
			"Id":      "HoloPod-" + string(rune('A'+i)),
			"Name":    "pod",
			"State":   "Running",
			"Network": "net0",
		}
	}
	rowsJSON, _ := json.Marshal(map[string]any{"rows": rows, "total": 25})
	r := &CallResult{
		Status: "ok",
		OpID:   "holodeck.pod.list",
		Result: json.RawMessage(rowsJSON),
	}
	var buf bytes.Buffer
	printPodList(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "more rows") {
		t.Errorf("expected truncation note for 25 rows; got:\n%s", out)
	}
}

// TestPrintK8sExecSafetyRejectSurfacesError — when the backend's
// safety gate refuses the call, it returns status="ok" with an
// inline `error` string in the result envelope. The human render
// must surface that error string so the operator sees the rejection
// without needing --json.
func TestPrintK8sExecSafetyRejectSurfacesError(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "holodeck.k8s.exec",
		Result: json.RawMessage(`{"stdout":"","stderr":"","exit_status":null,"error":"k8s.exec safety check: kubectl command rejected: shell metacharacter detected"}`),
	}
	var buf bytes.Buffer
	printK8sExec(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "metacharacter") {
		t.Errorf("expected safety-reject error in human render; got:\n%s", out)
	}
}

// TestPrintLogsTailRendersFiles — printLogsTail surfaces each matching
// file under its own GNU-style header.
func TestPrintLogsTailRendersFiles(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "holodeck.logs.tail",
		Result: json.RawMessage(`{"files":[{"path":"/holodeck-runtime/logs/dhcp-server.log","lines":"event1\nevent2\n"}],"raw":"...","lines_requested":100}`),
	}
	var buf bytes.Buffer
	printLogsTail(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "==>") || !strings.Contains(out, "dhcp-server.log") {
		t.Errorf("expected file header in human render; got:\n%s", out)
	}
}

// TestPrintNetworkingShowRendersSections — printNetworkingShow surfaces
// each of the four sub-sections (bgp, routes, dns, dhcp) with ok flags.
func TestPrintNetworkingShowRendersSections(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "holodeck.networking.show",
		Result: json.RawMessage(`{"bgp":{"summary_text":"BGP peer 10.0.0.1 Established\n","ok":true},"routes":{"text":"","ok":false},"dns":{"zones":[{"ZoneName":"holodeck.local","ZoneType":"Primary"}],"total":1,"ok":true},"dhcp":{"leases_text":"","ok":false}}`),
	}
	var buf bytes.Buffer
	printNetworkingShow(&buf, r)
	out := buf.String()
	for _, want := range []string{"bgp:", "routes:", "dns:", "dhcp:", "holodeck.local"} {
		if !strings.Contains(out, want) {
			t.Errorf("printNetworkingShow output missing %q; got:\n%s", want, out)
		}
	}
}
