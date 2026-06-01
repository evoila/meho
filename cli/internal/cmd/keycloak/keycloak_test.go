// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
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
// `("keycloak", "26.x", "keycloak-admin")` per parse_connector_id's
// grammar.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "keycloak-admin-26.x" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "keycloak-admin-26.x")
	}
}

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

func TestJoinOrNone(t *testing.T) {
	if got := joinOrNone(nil); got != "(none)" {
		t.Fatalf("empty should render (none); got %q", got)
	}
	if got := joinOrNone([]string{"a", "b"}); got != "a, b" {
		t.Fatalf("join: got %q", got)
	}
}

func TestRoleNamesExtractsNameField(t *testing.T) {
	raw := []any{
		map[string]any{"id": "x", "name": "tenant_admin"},
		map[string]any{"id": "y", "name": "view-realm"},
		map[string]any{"id": "z"}, // missing name — skipped
	}
	got := roleNames(raw)
	if len(got) != 2 || got[0] != "tenant_admin" || got[1] != "view-realm" {
		t.Fatalf("roleNames: %+v", got)
	}
	if roleNames("not-an-array") != nil {
		t.Fatalf("roleNames of non-array should be nil")
	}
}

// ---------- decode helpers ----------

func TestDecodeRowsResultHappy(t *testing.T) {
	raw := json.RawMessage(`{"rows":[{"clientId":"meho-backplane","id":"uuid-1"}],"total":1}`)
	rows, total, err := decodeRowsResult(raw)
	if err != nil {
		t.Fatalf("decodeRowsResult: %v", err)
	}
	if total != 1 || len(rows) != 1 || rows[0]["clientId"] != "meho-backplane" {
		t.Fatalf("decoded: rows=%+v total=%d", rows, total)
	}
}

func TestDecodeRowsResultNullAndEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		rows, total, err := decodeRowsResult(raw)
		if err != nil {
			t.Fatalf("decodeRowsResult empty: %v", err)
		}
		if rows != nil || total != 0 {
			t.Fatalf("empty should be nil rows / 0 total; got %+v / %d", rows, total)
		}
	}
}

func TestDecodeWrappedObject(t *testing.T) {
	raw := json.RawMessage(`{"realm":{"realm":"evba","enabled":true}}`)
	obj, err := decodeWrappedObject(raw, "realm")
	if err != nil {
		t.Fatalf("decodeWrappedObject: %v", err)
	}
	if obj["realm"] != "evba" || obj["enabled"] != true {
		t.Fatalf("decoded: %+v", obj)
	}
	// Missing key → nil, no error.
	miss, err := decodeWrappedObject(raw, "client")
	if err != nil || miss != nil {
		t.Fatalf("missing key should be nil/nil; got %+v / %v", miss, err)
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
// connector_id="keycloak-admin-26.x" pre-baked.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "keycloak-admin-26.x" {
				t.Errorf("connector_id: got %q want keycloak-admin-26.x", body.ConnectorID)
			}
			if body.OpID != "keycloak.realm.get" {
				t.Errorf("op_id: got %q want keycloak.realm.get", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "keycloak.realm.get",
				Result: json.RawMessage(`{"realm":{"realm":"evba"}}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "keycloak.realm.get", "rdc-keycloak", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestDispatchOpTargetSlugWrappedAsName — a non-empty target slug must
// surface as `{"name": "<slug>"}` so the dispatcher's resolver pulls it
// under the canonical TargetRef shape.
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
			if tgt == nil || tgt["name"] != "rdc-keycloak" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "keycloak.realm.get"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "keycloak.realm.get", "rdc-keycloak", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchOpForwardsParams — the client.get / role_mapping.get verbs
// forward an `id` param; client.list forwards client_id + max. Assert
// the param map reaches the wire unchanged.
func TestDispatchOpForwardsParams(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.Params["id"] != "client-uuid-1" {
				t.Errorf("params.id: got %v want client-uuid-1", body.Params["id"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "keycloak.client.get",
				Result: json.RawMessage(`{"client":{"clientId":"meho-backplane"}}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "keycloak.client.get", "rdc-keycloak",
		map[string]any{"id": "client-uuid-1"})
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestAllOpsUseCanonicalOpIDs — pin the 6 canonical keycloak op_ids the
// CLI dispatches. Any drift here surfaces as a test failure rather than
// a silent 404 from the backplane op registry.
func TestAllOpsUseCanonicalOpIDs(t *testing.T) {
	expectedOps := []string{
		"keycloak.realm.get",
		"keycloak.client.list",
		"keycloak.client.get",
		"keycloak.client_scope.list",
		"keycloak.user.list",
		"keycloak.role_mapping.get",
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
				Result: json.RawMessage(`{"rows":[],"total":0}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	for _, opID := range expectedOps {
		if _, err := dispatchOp(context.Background(), srv.URL, opID, "rdc-keycloak", nil); err != nil {
			t.Fatalf("dispatchOp %s: %v", opID, err)
		}
	}
	for _, opID := range expectedOps {
		if !dispatched[opID] {
			t.Errorf("op_id %q was not dispatched", opID)
		}
	}
}

// ---------- command-tree shape tests ----------

// TestNewRootCmdHasExpectedSubcommands — the root command must expose
// the expected verb names so `meho keycloak --help` lists them.
func TestNewRootCmdHasExpectedSubcommands(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"realm":           false,
		"client":          false,
		"client-scope":    false,
		"protocol-mapper": false,
		"user":            false,
		"role-mapping":    false,
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

func TestClientHasListAndGet(t *testing.T) {
	c := newClientCmd()
	subs := make(map[string]bool)
	for _, s := range c.Commands() {
		subs[s.Name()] = true
	}
	for _, name := range []string{"list", "get"} {
		if !subs[name] {
			t.Errorf("client is missing sub-verb %q", name)
		}
	}
}

// TestClientGetRequiresID — `client get` must mark --id required so a
// missing UUID fails before dispatch rather than 404ing at the backplane.
func TestClientGetRequiresID(t *testing.T) {
	cmd := newClientGetCmd()
	flag := cmd.Flags().Lookup("id")
	if flag == nil {
		t.Fatalf("client get is missing the --id flag")
	}
	if ann := flag.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
		t.Errorf("client get --id should be marked required; annotations=%v", flag.Annotations)
	}
}

func TestRoleMappingGetRequiresID(t *testing.T) {
	cmd := newRoleMappingGetCmd()
	flag := cmd.Flags().Lookup("id")
	if flag == nil {
		t.Fatalf("role-mapping get is missing the --id flag")
	}
	if ann := flag.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
		t.Errorf("role-mapping get --id should be marked required; annotations=%v", flag.Annotations)
	}
}

// cobraRequiredAnnotation is the pflag annotation key cobra sets via
// MarkFlagRequired. Pinned here so the required-flag tests don't import
// cobra internals.
const cobraRequiredAnnotation = "cobra_annotation_bash_completion_one_required_flag"
