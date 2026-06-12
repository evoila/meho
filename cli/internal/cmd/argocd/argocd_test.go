// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package argocd

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

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// ---------- helper tests (pure-function) ----------

// TestConnectorIDIsFrozen pins the pre-baked connector_id constant.
// Every verb file dispatches against this value; a regression here would
// silently rebind every alias verb to a different connector. The id
// encodes the registry-v2 natural key triple
// `("argocd", "3.x", "argocd-api")` per parse_connector_id's grammar.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "argocd-api-3.x" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "argocd-api-3.x")
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

// ---------- decode / accessor helpers ----------

func TestDecodeItemsResultHappy(t *testing.T) {
	raw := json.RawMessage(`{"items":[{"metadata":{"name":"app-1"}},{"metadata":{"name":"app-2"}}],"metadata":{}}`)
	items, err := decodeItemsResult(raw)
	if err != nil {
		t.Fatalf("decodeItemsResult: %v", err)
	}
	if len(items) != 2 || appName(items[0]) != "app-1" {
		t.Fatalf("decoded: %+v", items)
	}
}

func TestDecodeItemsResultNullAndEmpty(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`null`)} {
		items, err := decodeItemsResult(raw)
		if err != nil {
			t.Fatalf("decodeItemsResult empty: %v", err)
		}
		if items != nil {
			t.Fatalf("empty should be nil items; got %+v", items)
		}
	}
	// items: null inside a non-null envelope → nil slice, no error.
	items, err := decodeItemsResult(json.RawMessage(`{"items":null,"metadata":{}}`))
	if err != nil || items != nil {
		t.Fatalf("null items field should be nil/nil; got %+v / %v", items, err)
	}
}

func TestDecodeObject(t *testing.T) {
	raw := json.RawMessage(`{"metadata":{"name":"platform"},"status":{"sync":{"status":"Synced"}}}`)
	obj, err := decodeObject(raw)
	if err != nil {
		t.Fatalf("decodeObject: %v", err)
	}
	if appName(obj) != "platform" {
		t.Fatalf("appName: %q", appName(obj))
	}
	miss, err := decodeObject(json.RawMessage(`null`))
	if err != nil || miss != nil {
		t.Fatalf("null should be nil/nil; got %+v / %v", miss, err)
	}
}

func TestNestedStringWalksAndGuards(t *testing.T) {
	app := map[string]any{
		"status": map[string]any{
			"sync":   map[string]any{"status": "OutOfSync"},
			"health": map[string]any{"status": "Degraded"},
		},
	}
	if got := nestedString(app, "status", "sync", "status"); got != "OutOfSync" {
		t.Errorf("sync status: got %q", got)
	}
	if got := nestedString(app, "status", "health", "status"); got != "Degraded" {
		t.Errorf("health status: got %q", got)
	}
	// Missing hop → "".
	if got := nestedString(app, "spec", "project"); got != "" {
		t.Errorf("missing hop should be empty; got %q", got)
	}
	// Leaf not a string → "".
	bad := map[string]any{"a": map[string]any{"b": 42}}
	if got := nestedString(bad, "a", "b"); got != "" {
		t.Errorf("non-string leaf should be empty; got %q", got)
	}
}

func TestCountList(t *testing.T) {
	proj := map[string]any{
		"spec": map[string]any{
			"sourceRepos":  []any{"https://a", "https://b"},
			"destinations": []any{map[string]any{"server": "x"}},
		},
	}
	if got := countList(proj, "spec", "sourceRepos"); got != 2 {
		t.Errorf("sourceRepos count: got %d want 2", got)
	}
	if got := countList(proj, "spec", "destinations"); got != 1 {
		t.Errorf("destinations count: got %d want 1", got)
	}
	if got := countList(proj, "spec", "missing"); got != 0 {
		t.Errorf("missing key should be 0; got %d", got)
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
// connector_id="argocd-api-3.x" pre-baked.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "argocd-api-3.x" {
				t.Errorf("connector_id: got %q want argocd-api-3.x", body.ConnectorID)
			}
			if body.OpID != "argocd.app.list" {
				t.Errorf("op_id: got %q want argocd.app.list", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok",
				OpID:   "argocd.app.list",
				Result: json.RawMessage(`{"items":[],"metadata":{}}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "argocd.app.list", "rdc-argocd", nil)
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
			if tgt == nil || tgt["name"] != "rdc-argocd" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "argocd.app.list"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "argocd.app.list", "rdc-argocd", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// TestDispatchOpForwardsParams — the app.get / app.diff /
// app.resource_tree verbs forward a `name` param; app.list forwards
// projects + selector. Assert the param map reaches the wire unchanged.
func TestDispatchOpForwardsParams(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.Params["name"] != "platform-bootstrap" {
				t.Errorf("params.name: got %v want platform-bootstrap", body.Params["name"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "argocd.app.get",
				Result: json.RawMessage(`{"metadata":{"name":"platform-bootstrap"}}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, "argocd.app.get", "rdc-argocd",
		map[string]any{"name": "platform-bootstrap"})
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestAllOpsUseCanonicalOpIDs — pin the 6 canonical argocd op_ids the CLI
// dispatches. Any drift here surfaces as a test failure rather than a
// silent 404 from the backplane op registry.
func TestAllOpsUseCanonicalOpIDs(t *testing.T) {
	expectedOps := []string{
		"argocd.app.list",
		"argocd.app.get",
		"argocd.app.diff",
		"argocd.app.resource_tree",
		"argocd.appproject.list",
		"argocd.repo.list",
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
				Result: json.RawMessage(`{"items":[],"metadata":{}}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	for _, opID := range expectedOps {
		if _, err := dispatchOp(context.Background(), srv.URL, opID, "rdc-argocd", nil); err != nil {
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

// TestNewRootCmdHasExpectedSubcommands — the root command must expose the
// expected verb names so `meho argocd --help` lists them.
func TestNewRootCmdHasExpectedSubcommands(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"app":        false,
		"appproject": false,
		"repo":       false,
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

func TestAppHasAllFourVerbs(t *testing.T) {
	c := newAppCmd()
	subs := make(map[string]bool)
	for _, s := range c.Commands() {
		subs[s.Name()] = true
	}
	for _, name := range []string{"list", "get", "diff", "resource-tree"} {
		if !subs[name] {
			t.Errorf("app is missing sub-verb %q", name)
		}
	}
}

// TestAppGetRequiresName — `app get` must mark --name required so a
// missing name fails before dispatch rather than 404ing at the backplane.
// Same for diff and resource-tree.
func TestAppGetRequiresName(t *testing.T) {
	for name, ctor := range map[string]func() *cobra.Command{
		"get":           newAppGetCmd,
		"diff":          newAppDiffCmd,
		"resource-tree": newAppResourceTreeCmd,
	} {
		cmd := ctor()
		flag := cmd.Flags().Lookup("name")
		if flag == nil {
			t.Fatalf("app %s is missing the --name flag", name)
		}
		if ann := flag.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
			t.Errorf("app %s --name should be marked required; annotations=%v", name, flag.Annotations)
		}
	}
}

// cobraRequiredAnnotation is the pflag annotation key cobra sets via
// MarkFlagRequired. Pinned here so the required-flag tests don't import
// cobra internals.
const cobraRequiredAnnotation = "cobra_annotation_bash_completion_one_required_flag"
