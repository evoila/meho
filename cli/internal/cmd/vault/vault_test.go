// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vault

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// ---------- pure-function helpers ----------

// TestTruncatePassthroughAndCut covers the rune-aware truncate helper.
// Same shape as the vmware sibling — duplicated because cmd/vault
// can't import it without an import cycle.
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

// TestNormaliseURLBasic — trailing-slash trimming + reject-empty are
// the load-bearing properties.
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

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or any
// wrapping error) maps to AuthExpired; everything else to Unexpected.
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

// TestConnectorIDIsFrozen pins the pre-baked connector_id. Every verb
// dispatches against this value; a regression would silently rebind
// the entire vault verb tree to a different connector. The string form
// is the backend's natural-key encoding for
// (product="vault", version="1.x", impl_id="vault").
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "vault-1.x" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "vault-1.x")
	}
}

// ---------- loadJSONFlag ----------

func TestLoadJSONFlagEmpty(t *testing.T) {
	got, err := loadJSONFlag("")
	if err != nil {
		t.Fatalf("loadJSONFlag(\"\"): %v", err)
	}
	if got != nil {
		t.Fatalf("loadJSONFlag(\"\") should be nil; got %v", got)
	}
}

func TestLoadJSONFlagInline(t *testing.T) {
	got, err := loadJSONFlag(`{"password":"s3cr3t","user":"svc"}`)
	if err != nil {
		t.Fatalf("loadJSONFlag: %v", err)
	}
	if got["password"] != "s3cr3t" || got["user"] != "svc" {
		t.Fatalf("inline JSON not parsed; got %v", got)
	}
}

func TestLoadJSONFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "secret.json")
	if err := os.WriteFile(path, []byte(`{"k":"v"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadJSONFlag("@" + path)
	if err != nil {
		t.Fatalf("loadJSONFlag @file: %v", err)
	}
	if got["k"] != "v" {
		t.Fatalf("file JSON not parsed; got %v", got)
	}
}

func TestLoadJSONFlagInvalidJSON(t *testing.T) {
	if _, err := loadJSONFlag(`{not-json`); err == nil {
		t.Fatalf("invalid JSON should error")
	}
}

// ---------- parseVersionList ----------

func TestParseVersionList(t *testing.T) {
	tests := []struct {
		name    string
		in      string
		want    []int
		wantErr bool
	}{
		{"single", "3", []int{3}, false},
		{"multi", "3,4,5", []int{3, 4, 5}, false},
		{"whitespace tolerated", " 3 , 4 ", []int{3, 4}, false},
		{"empty element", "3,,4", nil, true},
		{"non-integer", "3,x", nil, true},
		{"empty string", "", nil, true},
	}
	for _, tt := range tests {
		got, err := parseVersionList(tt.in)
		if tt.wantErr {
			if err == nil {
				t.Errorf("%s: expected error for %q", tt.name, tt.in)
			}
			continue
		}
		if err != nil {
			t.Errorf("%s: unexpected error: %v", tt.name, err)
			continue
		}
		if !reflect.DeepEqual(got, tt.want) {
			t.Errorf("%s: got %v want %v", tt.name, got, tt.want)
		}
	}
}

// TestKVPathParamsAlwaysSendsMount — the CLI must send mount
// explicitly so the operator's positional choice is authoritative and
// never silently falls back to the handler's "secret" default.
func TestKVPathParamsAlwaysSendsMount(t *testing.T) {
	p := kvPathParams("kv2", "app/db")
	if p["mount"] != "kv2" || p["path"] != "app/db" {
		t.Fatalf("kvPathParams: got %v", p)
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
// `<METHOD> <path>` keys. Same shape as the vmware sibling's helper.
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
// connector_id="vault-1.x" pre-baked.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "vault-1.x" {
				t.Errorf("connector_id: got %q want vault-1.x", body.ConnectorID)
			}
			if body.OpID != opKVRead {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opKVRead,
				Result: json.RawMessage(`{"data":{"k":"v"},"version":1}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := conn.Call(context.Background(), srv.URL, opKVRead, "rdc-vault", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestDispatchOpEmptyTargetSendsNullTarget — empty target slug must
// surface as `null` so the dispatcher's resolver can fall through.
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
				t.Errorf("target should be null when slug empty; got %v", raw["target"])
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

// TestDispatchOpTargetSlugWrappedAsName — a non-empty target slug must
// surface as `{"name": "<slug>"}` so the dispatcher's resolver pulls
// it under the canonical TargetRef shape.
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
			if tgt == nil || tgt["name"] != "rdc-vault" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "x"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := conn.Call(context.Background(), srv.URL, "x", "rdc-vault", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// ---------- verb-tree wiring ----------

// subnames returns the set of child command names of the named
// top-level verb under the vault root.
func subnames(t *testing.T, parent string) map[string]bool {
	t.Helper()
	root := NewRootCmd()
	for _, c := range root.Commands() {
		if c.Name() == parent {
			s := map[string]bool{}
			for _, sub := range c.Commands() {
				s[sub.Name()] = true
			}
			return s
		}
	}
	t.Fatalf("%q sub-tree not found under vault root", parent)
	return nil
}

// TestNewRootCmdAssemblesTopLevelVerbs pins the kv/sys/auth groups.
func TestNewRootCmdAssemblesTopLevelVerbs(t *testing.T) {
	root := NewRootCmd()
	if root.Name() != "vault" {
		t.Fatalf("root command name: got %q want vault", root.Name())
	}
	want := map[string]bool{"kv": true, "sys": true, "auth": true}
	for _, c := range root.Commands() {
		delete(want, c.Name())
	}
	if len(want) > 0 {
		t.Errorf("vault tree missing top-level groups: %v", want)
	}
}

func TestKVSubtreeAssemblesAllVerbs(t *testing.T) {
	got := subnames(t, "kv")
	for _, want := range []string{"read", "list", "put", "versions", "delete"} {
		if !got[want] {
			t.Errorf("vault kv sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestSysSubtreeAssemblesAllVerbs(t *testing.T) {
	got := subnames(t, "sys")
	for _, want := range []string{"health", "seal-status", "mounts-list", "auth-list"} {
		if !got[want] {
			t.Errorf("vault sys sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestAuthSubtreeAssemblesAllVerbs(t *testing.T) {
	got := subnames(t, "auth")
	for _, want := range []string{"userpass-list", "userpass-read", "approle-list", "approle-read"} {
		if !got[want] {
			t.Errorf("vault auth sub-tree missing %q; got %v", want, got)
		}
	}
}

// ---------- flag parsing / arg arity ----------

// TestKVReadRequiresTwoArgs — `kv read` takes exactly <mount> <path>.
func TestKVReadRequiresTwoArgs(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"kv", "read", "only-one"})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("kv read with one arg should error on arity")
	}
}

// TestKVPutRequiresDataFlag — --data is MarkFlagRequired.
func TestKVPutRequiresDataFlag(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"kv", "put", "secret", "app/db"})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("kv put without --data should error on required flag")
	}
}

// TestKVDeleteRequiresVersionsFlag — --versions is MarkFlagRequired.
func TestKVDeleteRequiresVersionsFlag(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"kv", "delete", "secret", "app/db"})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("kv delete without --versions should error on required flag")
	}
}

// ---------- end-to-end: flag → params wire shape ----------

// TestKVReadE2E pins the full `meho vault kv read` path: positional
// <mount> <path> map to params.mount/params.path, op_id is
// vault.kv.read, connector_id pre-baked, target wrapped.
func TestKVReadE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opKVRead {
				t.Errorf("op_id: got %q want %q", body.OpID, opKVRead)
			}
			if body.Params["mount"] != "secret" || body.Params["path"] != "meho/test/fed" {
				t.Errorf("params: got %v", body.Params)
			}
			if body.Target["name"] != "rdc-vault" {
				t.Errorf("target: got %v", body.Target)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opKVRead,
				Result: json.RawMessage(`{"data":{"token":"abc"},"version":2}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{
		"kv", "read", "secret", "meho/test/fed",
		"--target", "rdc-vault", "--backplane", srv.URL,
	})
	if err := root.Execute(); err != nil {
		t.Fatalf("kv read e2e: %v\noutput:\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "vault-1.x") || !strings.Contains(out.String(), "token") {
		t.Errorf("kv read render missing connector id / payload; got:\n%s", out.String())
	}
}

// TestKVPutE2E pins --data parsing + --cas binding into params.
func TestKVPutE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opKVPut {
				t.Errorf("op_id: got %q", body.OpID)
			}
			data, _ := body.Params["data"].(map[string]any)
			if data == nil || data["password"] != "s3cr3t" {
				t.Errorf("params.data: got %v", body.Params["data"])
			}
			// JSON numbers decode as float64 into map[string]any.
			if body.Params["cas"] != float64(3) {
				t.Errorf("params.cas: got %v (%T)", body.Params["cas"], body.Params["cas"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opKVPut,
				Result: json.RawMessage(`{"version":4}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{
		"kv", "put", "secret", "app/db",
		"--data", `{"password":"s3cr3t"}`, "--cas", "3",
		"--target", "rdc-vault", "--backplane", srv.URL,
	})
	if err := root.Execute(); err != nil {
		t.Fatalf("kv put e2e: %v\noutput:\n%s", err, out.String())
	}
}

// TestKVPutOmitsCasWhenNotPassed — without --cas the params map must
// not carry a "cas" key (so the handler's must-not-exist / no-cas
// path isn't accidentally triggered by a zero default).
func TestKVPutOmitsCasWhenNotPassed(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if _, present := body.Params["cas"]; present {
				t.Errorf("cas must be absent when --cas not passed; got %v", body.Params["cas"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opKVPut})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{
		"kv", "put", "secret", "app/db",
		"--data", `{"k":"v"}`,
		"--target", "rdc-vault", "--backplane", srv.URL,
	})
	if err := root.Execute(); err != nil {
		t.Fatalf("kv put (no cas) e2e: %v\noutput:\n%s", err, out.String())
	}
}

// TestKVDeleteE2E pins --versions "3,4,5" → params.versions []int.
func TestKVDeleteE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opKVDelete {
				t.Errorf("op_id: got %q", body.OpID)
			}
			vs, _ := body.Params["versions"].([]any)
			if len(vs) != 3 || vs[0] != float64(3) || vs[2] != float64(5) {
				t.Errorf("params.versions: got %v", body.Params["versions"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opKVDelete})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{
		"kv", "delete", "secret", "app/db",
		"--versions", "3,4,5",
		"--target", "rdc-vault", "--backplane", srv.URL,
	})
	if err := root.Execute(); err != nil {
		t.Fatalf("kv delete e2e: %v\noutput:\n%s", err, out.String())
	}
}

// TestSysHealthE2E pins a no-param sys verb dispatches the right
// op_id with no params field.
func TestSysHealthE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opSysHealth {
				t.Errorf("op_id: got %q want %q", body.OpID, opSysHealth)
			}
			if body.Params != nil {
				t.Errorf("sys health should send no params; got %v", body.Params)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opSysHealth,
				Result: json.RawMessage(`{"ok":true,"detail":"healthy"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{"sys", "health", "--target", "rdc-vault", "--backplane", srv.URL})
	if err := root.Execute(); err != nil {
		t.Fatalf("sys health e2e: %v\noutput:\n%s", err, out.String())
	}
}

// TestAuthUserpassReadE2E pins <user> → params.username and the
// vault.auth.userpass.read op_id.
func TestAuthUserpassReadE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opAuthUserpassRead {
				t.Errorf("op_id: got %q want %q", body.OpID, opAuthUserpassRead)
			}
			if body.Params["username"] != "svc-deploy" {
				t.Errorf("params.username: got %v", body.Params["username"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opAuthUserpassRead,
				Result: json.RawMessage(`{"policies":["default"]}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{
		"auth", "userpass-read", "svc-deploy",
		"--target", "rdc-vault", "--backplane", srv.URL,
	})
	if err := root.Execute(); err != nil {
		t.Fatalf("auth userpass-read e2e: %v\noutput:\n%s", err, out.String())
	}
}

// TestAuthApproleReadMapsRoleNameParam — approle-read uses the
// `role_name` schema key (not `username`).
func TestAuthApproleReadMapsRoleNameParam(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opAuthApproleRead {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["role_name"] != "ci-runner" {
				t.Errorf("params.role_name: got %v", body.Params)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opAuthApproleRead})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{
		"auth", "approle-read", "ci-runner",
		"--target", "rdc-vault", "--backplane", srv.URL,
	})
	if err := root.Execute(); err != nil {
		t.Fatalf("auth approle-read e2e: %v\noutput:\n%s", err, out.String())
	}
}

// TestErrorStatusExitsNonZero — a dispatcher status=error envelope
// surfaces as errOpError so main exits non-zero.
func TestErrorStatusExitsNonZero(t *testing.T) {
	errStr := "permission denied"
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "error", OpID: opKVRead, Error: &errStr,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{
		"kv", "read", "secret", "x",
		"--target", "rdc-vault", "--backplane", srv.URL,
	})
	if err := root.Execute(); err == nil {
		t.Fatalf("status=error should propagate a non-nil RunE error")
	}
}

// TestHelpListsTree — `meho vault --help` documents every group, an
// explicit acceptance criterion.
func TestHelpListsTree(t *testing.T) {
	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("vault --help: %v", err)
	}
	for _, want := range []string{"kv", "sys", "auth", "vault-1.x"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("vault --help missing %q; got:\n%s", want, out.String())
		}
	}
}
