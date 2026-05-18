// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

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
// `("bind9", "9.x", "bind9-ssh")` per parse_connector_id's grammar.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "bind9-ssh-9.x" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "bind9-ssh-9.x")
	}
}

// TestTruncatePassthroughAndCut covers the rune-aware truncate
// helper. Same shape as the vmware sibling — duplicated to avoid
// import cycle.
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

// TestNormaliseURLBasic mirrors the vmware sibling — trailing-slash
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
	raw := json.RawMessage(`{"vendor":"isc","version":"9.18.24"}`)
	got, err := decodeFlatResult(raw)
	if err != nil {
		t.Fatalf("decodeFlatResult: %v", err)
	}
	if got["vendor"] != "isc" || got["version"] != "9.18.24" {
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
	raw := json.RawMessage(`{"rows":[{"name":"evba.lab","type":"master","file":"/etc/bind/db.evba.lab"}],"total":1}`)
	rows, err := decodeRowsResult(raw)
	if err != nil {
		t.Fatalf("decodeRowsResult: %v", err)
	}
	if len(rows) != 1 || rows[0]["name"] != "evba.lab" {
		t.Fatalf("decoded: %+v", rows)
	}
}

func TestStringFieldMissingAndWrongType(t *testing.T) {
	e := map[string]any{"name": "x", "ttl": 3600.0}
	if got := stringField(e, "name"); got != "x" {
		t.Errorf("stringField(name): %q", got)
	}
	if got := stringField(e, "ttl"); got != "" {
		t.Errorf("stringField(ttl, float): expected empty; got %q", got)
	}
	if got := stringField(e, "missing"); got != "" {
		t.Errorf("stringField(missing): expected empty; got %q", got)
	}
}

// ---------- assembleViewsBundle ----------

// TestAssembleViewsBundleHappy — the views.conf maps to
// named.conf.local; every regular file under zones-dir maps to its
// relative path.
func TestAssembleViewsBundleHappy(t *testing.T) {
	dir := t.TempDir()
	viewsConf := filepath.Join(dir, "views.conf")
	if err := os.WriteFile(viewsConf, []byte("// views fragment\n"), 0o644); err != nil {
		t.Fatalf("write views: %v", err)
	}
	zonesDir := filepath.Join(dir, "zones")
	if err := os.MkdirAll(zonesDir, 0o755); err != nil {
		t.Fatalf("mkdir zones: %v", err)
	}
	if err := os.WriteFile(filepath.Join(zonesDir, "db.evba.lab"), []byte("zone content"), 0o644); err != nil {
		t.Fatalf("write zone: %v", err)
	}
	bundle, err := assembleViewsBundle(viewsConf, zonesDir)
	if err != nil {
		t.Fatalf("assembleViewsBundle: %v", err)
	}
	if bundle["named.conf.local"] != "// views fragment\n" {
		t.Errorf("views fragment not mapped to named.conf.local: %+v", bundle)
	}
	if bundle["db.evba.lab"] != "zone content" {
		t.Errorf("zone file not mapped: %+v", bundle)
	}
}

// TestAssembleViewsBundleRejectsCollidingNamedConfLocal — a local
// `named.conf.local` under zonesDir would silently win over the
// views-conf arg; we reject it to keep the operator wire shape clear.
func TestAssembleViewsBundleRejectsCollidingNamedConfLocal(t *testing.T) {
	dir := t.TempDir()
	viewsConf := filepath.Join(dir, "views.conf")
	if err := os.WriteFile(viewsConf, []byte("// views\n"), 0o644); err != nil {
		t.Fatalf("write views: %v", err)
	}
	zonesDir := filepath.Join(dir, "zones")
	if err := os.MkdirAll(zonesDir, 0o755); err != nil {
		t.Fatalf("mkdir zones: %v", err)
	}
	if err := os.WriteFile(filepath.Join(zonesDir, "named.conf.local"), []byte("collide"), 0o644); err != nil {
		t.Fatalf("write conflict: %v", err)
	}
	_, err := assembleViewsBundle(viewsConf, zonesDir)
	if err == nil || !strings.Contains(err.Error(), "named.conf.local") {
		t.Fatalf("expected collision error; got %v", err)
	}
}

// TestAssembleViewsBundleRejectsMissingZonesDir — non-existent
// zonesDir surfaces clearly client-side.
func TestAssembleViewsBundleRejectsMissingZonesDir(t *testing.T) {
	dir := t.TempDir()
	viewsConf := filepath.Join(dir, "views.conf")
	if err := os.WriteFile(viewsConf, []byte("// views\n"), 0o644); err != nil {
		t.Fatalf("write views: %v", err)
	}
	_, err := assembleViewsBundle(viewsConf, filepath.Join(dir, "nonexistent"))
	if err == nil {
		t.Fatalf("expected error for missing zonesDir")
	}
}

// ---------- renderers ----------

// TestPrintAboutHumanFormat — happy-path render with vendor/product/
// version present.
func TestPrintAboutHumanFormat(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "bind9.about",
		Result:     json.RawMessage(`{"vendor":"isc","product":"bind9","version":"9.18.24","os":"debian 12"}`),
		DurationMs: 42.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=ok", "bind9-ssh-9.x", "isc", "bind9", "9.18.24", "debian 12"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintAboutErrorRendersErrorString — status=error with a
// non-nil Error pointer surfaces the error string + extras.
func TestPrintAboutErrorRendersErrorString(t *testing.T) {
	errMsg := "ssh handshake failed"
	r := &CallResult{
		Status:     "error",
		OpID:       "bind9.about",
		Error:      &errMsg,
		Extras:     json.RawMessage(`{"reason":"auth_failed"}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printAbout(&buf, r)
	out := buf.String()
	for _, want := range []string{"status=error", "ssh handshake failed", "extras:", "auth_failed"} {
		if !strings.Contains(out, want) {
			t.Errorf("printAbout error missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintZoneListTable — happy-path render with 2 zones.
func TestPrintZoneListTable(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "bind9.zone.list",
		Result:     json.RawMessage(`{"rows":[{"name":"evba.lab","type":"master","file":"/etc/bind/db.evba.lab"},{"name":"50.5.10.in-addr.arpa","type":"master","file":"/etc/bind/db.10.5.50"}],"total":2}`),
		DurationMs: 12.0,
	}
	var buf bytes.Buffer
	printZoneList(&buf, r)
	out := buf.String()
	for _, want := range []string{"evba.lab", "master", "/etc/bind/db.evba.lab", "50.5.10.in-addr.arpa"} {
		if !strings.Contains(out, want) {
			t.Errorf("printZoneList missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintZoneListEmpty — zero zones renders the count line.
func TestPrintZoneListEmpty(t *testing.T) {
	r := &CallResult{Status: "ok", OpID: "bind9.zone.list", Result: json.RawMessage(`{"rows":[],"total":0}`)}
	var buf bytes.Buffer
	printZoneList(&buf, r)
	if !strings.Contains(buf.String(), "(0 zones)") {
		t.Errorf("empty list should announce 0 zones; got:\n%s", buf.String())
	}
}

// TestPrintRecordGetTable — happy-path render with one A row.
func TestPrintRecordGetTable(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "bind9.record.get",
		Result: json.RawMessage(`{"fqdn":"www.evba.lab.","type":"A","rows":[{"name":"www.evba.lab.","ttl":3600,"class":"IN","type":"A","rdata":"10.5.50.2"}],"total":1}`),
	}
	var buf bytes.Buffer
	printRecordGet(&buf, r)
	out := buf.String()
	for _, want := range []string{"www.evba.lab.", "10.5.50.2", "A"} {
		if !strings.Contains(out, want) {
			t.Errorf("printRecordGet missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintWriteResultHumanFormat — happy-path render of the write
// envelope (record.add / record.remove / config.apply_*).
func TestPrintWriteResultHumanFormat(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "bind9.record.add",
		Result: json.RawMessage(`{"op_class":"write","fqdn":"api.evba.lab","zone":"evba.lab","file":"/etc/bind/db.evba.lab","type":"A","ip":"10.5.50.99","state_before":"snapshot before","state_after":"snapshot after"}`),
	}
	var buf bytes.Buffer
	printWriteResult(&buf, r)
	out := buf.String()
	for _, want := range []string{"op_class: write", "api.evba.lab", "evba.lab", "10.5.50.99", "state_before", "state_after"} {
		if !strings.Contains(out, want) {
			t.Errorf("printWriteResult missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintConfigBackup — happy-path render with two backups in
// listing.
func TestPrintConfigBackup(t *testing.T) {
	r := &CallResult{
		Status: "ok",
		OpID:   "bind9.config.backup",
		Result: json.RawMessage(`{"backup_id":"20260517T120000Z-pre-migration","path":"/var/backups/meho-bind9/20260517T120000Z-pre-migration.tar.gz","rows":[{"id":"20260516T100000Z-foo","size_bytes":1234,"mtime":"2026-05-16T10:00:00Z"},{"id":"20260517T120000Z-pre-migration","size_bytes":5678,"mtime":"2026-05-17T12:00:00Z"}]}`),
	}
	var buf bytes.Buffer
	printConfigBackup(&buf, r)
	out := buf.String()
	for _, want := range []string{"20260517T120000Z-pre-migration", "/var/backups/meho-bind9", "20260516T100000Z-foo"} {
		if !strings.Contains(out, want) {
			t.Errorf("printConfigBackup missing %q in:\n%s", want, out)
		}
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
// shape as the vmware sibling's helper.
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
// for the mocked backplane URL. Mirrors the vmware sibling.
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
// connector_id="bind9-ssh-9.x" pre-baked. This test pins that the
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
			if body.ConnectorID != "bind9-ssh-9.x" {
				t.Errorf("connector_id: got %q want bind9-ssh-9.x", body.ConnectorID)
			}
			if body.OpID != "bind9.about" {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "bind9.about",
				Result: json.RawMessage(`{"vendor":"isc"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "bind9.about", "vcf-router-bind9", nil)
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
			if tgt == nil || tgt["name"] != "vcf-router-bind9" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "bind9.about"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "bind9.about", "vcf-router-bind9", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchOpRecordAddSendsExpectedParams — pins the record.add
// wire shape: fqdn + ip + zone + type all land in params under their
// canonical keys.
func TestDispatchOpRecordAddSendsExpectedParams(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != "bind9.record.add" {
				t.Errorf("op_id: got %q want bind9.record.add", body.OpID)
			}
			if body.Params["fqdn"] != "api.evba.lab" {
				t.Errorf("fqdn: got %v", body.Params["fqdn"])
			}
			if body.Params["ip"] != "10.5.50.99" {
				t.Errorf("ip: got %v", body.Params["ip"])
			}
			if body.Params["zone"] != "evba.lab" {
				t.Errorf("zone: got %v", body.Params["zone"])
			}
			if body.Params["type"] != "A" {
				t.Errorf("type: got %v", body.Params["type"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "bind9.record.add"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	params := map[string]any{
		"fqdn": "api.evba.lab",
		"ip":   "10.5.50.99",
		"zone": "evba.lab",
		"type": "A",
	}
	if _, err := dispatchOp(context.Background(), srv.URL, "bind9.record.add", "vcf-router-bind9", params); err != nil {
		t.Fatalf("dispatchOp record.add: %v", err)
	}
}

// TestReadLocalFileHappyAndMissing — happy + error path.
func TestReadLocalFileHappyAndMissing(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "a.conf")
	if err := os.WriteFile(path, []byte("hello world"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	got, err := readLocalFile(path)
	if err != nil {
		t.Fatalf("readLocalFile: %v", err)
	}
	if got != "hello world" {
		t.Fatalf("content: %q", got)
	}
	if _, err := readLocalFile(filepath.Join(dir, "nope")); err == nil {
		t.Fatalf("missing path should error")
	}
}

// TestJSONUnmarshalStrictHappy — thin wrapper test (regression
// against signature drift).
func TestJSONUnmarshalStrictHappy(t *testing.T) {
	var out map[string]int
	if err := jsonUnmarshalStrict([]byte(`{"k":1}`), &out); err != nil {
		t.Fatalf("jsonUnmarshalStrict: %v", err)
	}
	if out["k"] != 1 {
		t.Fatalf("decoded: %+v", out)
	}
}
