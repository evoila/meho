// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package k8s

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

// ---------- pure-function helpers ----------

// TestTruncatePassthroughAndCut covers the rune-aware truncate helper.
// Same shape as the vault sibling.
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

// TestNormaliseURLBasic — trailing-slash trimming + reject-empty.
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

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or any
// wrapping error) maps to AuthExpired; everything else to Unexpected.
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

// TestConnectorIDIsFrozen pins the pre-baked connector_id. Every K8s
// verb dispatches against this value; a regression would silently
// rebind the entire k8s verb tree to a different connector. The
// string form is the backend's natural-key encoding for
// (product="k8s", version="1.x", impl_id="k8s") after the G3.2-T6
// precursor substrate fix.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "k8s-1.x" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "k8s-1.x")
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
	got, err := loadJSONFlag(`{"a":1,"b":"x"}`)
	if err != nil {
		t.Fatalf("loadJSONFlag inline: %v", err)
	}
	// JSON numbers decode as float64 into map[string]any.
	if got["a"] != float64(1) || got["b"] != "x" {
		t.Fatalf("loadJSONFlag inline: got %v", got)
	}
}

// ---------- errOpError sentinel ----------

func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatalf("errOpError should be non-nil")
	}
}

// ---------- HTTP wire shape (mocked) ----------

type mockHandler = http.HandlerFunc

// mockBackplane stands up an httptest.Server that routes by
// `<METHOD> <path>` keys. Same shape as the vault sibling's helper.
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
// for the mocked backplane URL. Mirrors the vault sibling.
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
// connector_id="k8s-1.x" pre-baked.
func TestDispatchOpBakesConnectorID(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "k8s-1.x" {
				t.Errorf("connector_id: got %q want k8s-1.x", body.ConnectorID)
			}
			if body.OpID != opAbout {
				t.Errorf("op_id: got %q", body.OpID)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opAbout,
				Result: json.RawMessage(`{"product":"k3s","git_version":"v1.32.5+k3s1"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	r, err := dispatchOp(context.Background(), srv.URL, opAbout, "rke2-meho", nil)
	if err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
	if r.Status != "ok" {
		t.Fatalf("dispatch status: %s", r.Status)
	}
}

// TestDispatchOpTargetSlugWrappedAsName — non-empty target slug must
// surface as `{"name": "<slug>"}`.
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
			if tgt == nil || tgt["name"] != "rke2-meho" {
				t.Errorf("target should wrap slug as {name: ...}; got %v", raw["target"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "x"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := dispatchOp(context.Background(), srv.URL, "x", "rke2-meho", nil); err != nil {
		t.Fatalf("dispatchOp: %v", err)
	}
}

// ---------- verb-tree wiring ----------

// subnames returns the set of child command names of the named
// top-level verb under the k8s root.
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
	t.Fatalf("%q sub-tree not found under k8s root", parent)
	return nil
}

// TestNewRootCmdAssemblesAllVerbs pins every top-level verb / group
// the k8s tree must expose. Covers acceptance criterion: "CLI verb
// tree complete; meho k8s --help shows reasonable help".
func TestNewRootCmdAssemblesAllVerbs(t *testing.T) {
	root := NewRootCmd()
	if root.Name() != "k8s" {
		t.Fatalf("root command name: got %q want k8s", root.Name())
	}
	want := map[string]bool{
		"about":      true, // top-level discovery
		"ls":         true, // top-level discovery
		"namespace":  true, // inventory group
		"node":       true, // inventory group
		"pod":        true, // workload group
		"deployment": true, // workload group
		"service":    true, // network group
		"ingress":    true, // network group
		"configmap":  true, // config group
		"event":      true, // observability group
		"logs":       true, // top-level observability verb
	}
	for _, c := range root.Commands() {
		delete(want, c.Name())
	}
	if len(want) > 0 {
		t.Errorf("k8s tree missing top-level verbs/groups: %v", want)
	}
}

func TestNamespaceSubtree(t *testing.T) {
	got := subnames(t, "namespace")
	for _, want := range []string{"list"} {
		if !got[want] {
			t.Errorf("k8s namespace sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestNodeSubtree(t *testing.T) {
	got := subnames(t, "node")
	for _, want := range []string{"list"} {
		if !got[want] {
			t.Errorf("k8s node sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestPodSubtree(t *testing.T) {
	got := subnames(t, "pod")
	for _, want := range []string{"list", "info"} {
		if !got[want] {
			t.Errorf("k8s pod sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestDeploymentSubtree(t *testing.T) {
	got := subnames(t, "deployment")
	for _, want := range []string{"list", "info"} {
		if !got[want] {
			t.Errorf("k8s deployment sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestServiceSubtree(t *testing.T) {
	got := subnames(t, "service")
	for _, want := range []string{"list"} {
		if !got[want] {
			t.Errorf("k8s service sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestIngressSubtree(t *testing.T) {
	got := subnames(t, "ingress")
	for _, want := range []string{"list"} {
		if !got[want] {
			t.Errorf("k8s ingress sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestConfigmapSubtree(t *testing.T) {
	got := subnames(t, "configmap")
	for _, want := range []string{"list", "info"} {
		if !got[want] {
			t.Errorf("k8s configmap sub-tree missing %q; got %v", want, got)
		}
	}
}

func TestEventSubtree(t *testing.T) {
	got := subnames(t, "event")
	for _, want := range []string{"list"} {
		if !got[want] {
			t.Errorf("k8s event sub-tree missing %q; got %v", want, got)
		}
	}
}

// ---------- arg arity ----------

func TestPodInfoRequiresOneArg(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"pod", "info", "--namespace", "ns"})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("pod info with zero args should error on arity")
	}
}

func TestPodInfoRequiresNamespaceFlag(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"pod", "info", "x"})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("pod info without --namespace should error on required flag")
	}
}

func TestLogsRequiresOneArg(t *testing.T) {
	root := NewRootCmd()
	root.SetArgs([]string{"logs", "--namespace", "ns"})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("logs with zero args should error on arity")
	}
}

func TestPodListRequiresNamespaceSelector(t *testing.T) {
	// Need a primed token + backplane so dispatch failure surfaces
	// the listParams selector check rather than the auth path.
	srv := mockBackplane(t, map[string]mockHandler{})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{"pod", "list", "--target", "x", "--backplane", srv.URL})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	err := root.Execute()
	if err == nil {
		t.Fatalf("pod list without --namespace/--all-namespaces should error")
	}
	if !strings.Contains(err.Error(), "namespace") {
		t.Fatalf("expected namespace-related error; got %v", err)
	}
}

func TestPodListRejectsBothNamespaceAndAll(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"pod", "list",
		"--namespace", "argocd",
		"--all-namespaces",
		"--target", "x", "--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("pod list with both selectors should error")
	}
}

// ---------- end-to-end: flag → params wire shape ----------

// TestAboutE2E pins the no-param canary path.
func TestAboutE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opAbout {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params != nil {
				t.Errorf("about should send no params; got %v", body.Params)
			}
			if body.Target["name"] != "rke2-meho" {
				t.Errorf("target: got %v", body.Target)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opAbout,
				Result: json.RawMessage(`{"product":"k3s","git_version":"v1.32.5+k3s1"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{"about", "--target", "rke2-meho", "--backplane", srv.URL})
	if err := root.Execute(); err != nil {
		t.Fatalf("about e2e: %v\noutput:\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "k8s-1.x") || !strings.Contains(out.String(), "k3s") {
		t.Errorf("about render missing connector id / payload; got:\n%s", out.String())
	}
}

// TestLsOmitsPathWhenAbsent — `meho k8s ls` (no positional) must omit
// the `path` key so the dispatcher applies the schema default.
func TestLsOmitsPathWhenAbsent(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.Params != nil {
				t.Errorf("ls without arg should omit params; got %v", body.Params)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opLs})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{"ls", "--target", "rke2-meho", "--backplane", srv.URL})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("ls (no arg) e2e: %v", err)
	}
}

// TestLsSendsPathWhenPresent.
func TestLsSendsPathWhenPresent(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.Params["path"] != "/argocd" {
				t.Errorf("ls path: got %v", body.Params)
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opLs})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{"ls", "/argocd", "--target", "rke2-meho", "--backplane", srv.URL})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("ls /argocd e2e: %v", err)
	}
}

// TestPodListE2E pins the full pod-list flag → params shape (namespace
// + label-selector + limit).
func TestPodListE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opPodList {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["namespace"] != "argocd" {
				t.Errorf("namespace: got %v", body.Params["namespace"])
			}
			if body.Params["label_selector"] != "app=argocd-server" {
				t.Errorf("label_selector: got %v", body.Params["label_selector"])
			}
			if body.Params["limit"] != float64(50) {
				t.Errorf("limit: got %v (%T)", body.Params["limit"], body.Params["limit"])
			}
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: opPodList,
				Result: json.RawMessage(`{"rows":[],"total":0}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"pod", "list",
		"--target", "rke2-meho",
		"--namespace", "argocd",
		"--label-selector", "app=argocd-server",
		"--limit", "50",
		"--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("pod list e2e: %v", err)
	}
}

// TestPodListAllNamespacesShape — --all-namespaces surfaces as
// {"all_namespaces": true}.
func TestPodListAllNamespacesShape(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if _, present := body.Params["namespace"]; present {
				t.Errorf("namespace key must be absent under --all-namespaces; got %v", body.Params)
			}
			if body.Params["all_namespaces"] != true {
				t.Errorf("all_namespaces: got %v", body.Params["all_namespaces"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opPodList})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"pod", "list",
		"--target", "rke2-meho",
		"--all-namespaces",
		"--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("pod list --all-namespaces e2e: %v", err)
	}
}

// TestPodInfoE2E pins <name> + --namespace -> params.
func TestPodInfoE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opPodInfo {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["pod_name"] != "argocd-server-7c4d8f6b6-abcde" {
				t.Errorf("pod_name: got %v", body.Params["pod_name"])
			}
			if body.Params["namespace"] != "argocd" {
				t.Errorf("namespace: got %v", body.Params["namespace"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opPodInfo})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"pod", "info", "argocd-server-7c4d8f6b6-abcde",
		"--namespace", "argocd",
		"--target", "rke2-meho", "--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("pod info e2e: %v", err)
	}
}

// TestConfigmapInfoE2E pins <name> + --namespace -> params (the keys
// here are name/namespace, not pod_name).
func TestConfigmapInfoE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opConfigmapInfo {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["name"] != "argocd-cm" {
				t.Errorf("name: got %v", body.Params["name"])
			}
			if body.Params["namespace"] != "argocd" {
				t.Errorf("namespace: got %v", body.Params["namespace"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opConfigmapInfo})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"configmap", "info", "argocd-cm",
		"--namespace", "argocd",
		"--target", "rke2-meho", "--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("configmap info e2e: %v", err)
	}
}

// TestEventListE2E pins --field-selector + --limit -> params shape.
func TestEventListE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opEventList {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["namespace"] != "argocd" {
				t.Errorf("namespace: got %v", body.Params["namespace"])
			}
			if body.Params["field_selector"] != "type=Warning" {
				t.Errorf("field_selector: got %v", body.Params["field_selector"])
			}
			if body.Params["limit"] != float64(25) {
				t.Errorf("limit: got %v", body.Params["limit"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opEventList})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"event", "list",
		"--target", "rke2-meho",
		"--namespace", "argocd",
		"--field-selector", "type=Warning",
		"--limit", "25",
		"--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("event list e2e: %v", err)
	}
}

// TestLogsE2E pins <pod> + every optional flag → params shape.
func TestLogsE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opLogs {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["pod_name"] != "argocd-server" {
				t.Errorf("pod_name: got %v", body.Params["pod_name"])
			}
			if body.Params["namespace"] != "argocd" {
				t.Errorf("namespace: got %v", body.Params["namespace"])
			}
			if body.Params["container"] != "argocd-server" {
				t.Errorf("container: got %v", body.Params["container"])
			}
			if body.Params["tail"] != float64(500) {
				t.Errorf("tail: got %v", body.Params["tail"])
			}
			if body.Params["since"] != "15m" {
				t.Errorf("since: got %v", body.Params["since"])
			}
			if body.Params["previous"] != true {
				t.Errorf("previous: got %v", body.Params["previous"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opLogs})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"logs", "argocd-server",
		"--namespace", "argocd",
		"--container", "argocd-server",
		"--tail", "500",
		"--since", "15m",
		"--previous",
		"--target", "rke2-meho", "--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("logs e2e: %v", err)
	}
}

// TestServiceListE2E pins service list -> namespace param shape.
func TestServiceListE2E(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.OpID != opServiceList {
				t.Errorf("op_id: got %q", body.OpID)
			}
			if body.Params["namespace"] != "argocd" {
				t.Errorf("namespace: got %v", body.Params["namespace"])
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: opServiceList})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{
		"service", "list",
		"--target", "rke2-meho",
		"--namespace", "argocd",
		"--backplane", srv.URL,
	})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err != nil {
		t.Fatalf("service list e2e: %v", err)
	}
}

// ---------- error propagation ----------

// TestErrorStatusExitsNonZero — dispatcher status=error envelope
// surfaces as errOpError so main exits non-zero.
func TestErrorStatusExitsNonZero(t *testing.T) {
	errStr := "permission denied"
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "error", OpID: opAbout, Error: &errStr,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	root := NewRootCmd()
	root.SetArgs([]string{"about", "--target", "x", "--backplane", srv.URL})
	root.SetOut(&bytes.Buffer{})
	root.SetErr(&bytes.Buffer{})
	if err := root.Execute(); err == nil {
		t.Fatalf("status=error should propagate a non-nil RunE error")
	}
}

// TestHelpListsTree — `meho k8s --help` documents every group, an
// explicit acceptance criterion.
func TestHelpListsTree(t *testing.T) {
	root := NewRootCmd()
	var out bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&out)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("k8s --help: %v", err)
	}
	for _, want := range []string{
		"about", "ls", "namespace", "node", "pod", "deployment",
		"service", "ingress", "configmap", "event", "logs", "k8s-1.x",
	} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("k8s --help missing %q; got:\n%s", want, out.String())
		}
	}
}
