// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package secret

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/dispatch"
)

// callRequestBody mirrors the on-the-wire OperationCall body so the
// wire-shape tests can decode and assert it.
type callRequestBody = dispatch.CallRequestBody

// cobraRequiredAnnotation is the pflag annotation key cobra sets via
// MarkFlagRequired. Pinned here so the required-flag tests don't import
// cobra internals.
const cobraRequiredAnnotation = "cobra_annotation_bash_completion_one_required_flag"

// ---------- mock backplane harness (mirrors cmd/keycloak) ----------

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

// ---------- connector_id is the synthetic broker triple ----------

// TestConnectorIDIsFrozen pins the pre-baked connector_id. It must match
// T1's (#1577) registered synthetic triple
// `(product="secret", version="1.x", impl_id="secret-broker")`, whose
// wire form `secret-broker-1.x` round-trips through parse_connector_id.
func TestConnectorIDIsFrozen(t *testing.T) {
	if ConnectorID != "secret-broker-1.x" {
		t.Fatalf("ConnectorID drifted: got %q want %q", ConnectorID, "secret-broker-1.x")
	}
}

// ---------- command-tree shape ----------

// TestNewRootCmdHasMove — the root command must expose the `move` verb so
// `meho secret --help` lists it.
func TestNewRootCmdHasMove(t *testing.T) {
	root := NewRootCmd()
	got := map[string]bool{}
	for _, sub := range root.Commands() {
		got[sub.Name()] = true
	}
	if !got["move"] {
		t.Errorf("secret root is missing the move sub-verb; has %v", got)
	}
}

// TestMoveHelpListsFlags — `meho secret move --help` must surface
// --from / --to / --reason (acceptance criterion (c)).
func TestMoveHelpListsFlags(t *testing.T) {
	cmd := newMoveCmd()
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	cmd.SetErr(&buf)
	cmd.SetArgs([]string{"--help"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("--help execute: %v", err)
	}
	out := buf.String()
	for _, want := range []string{"--from", "--to", "--reason"} {
		if !strings.Contains(out, want) {
			t.Errorf("move --help output missing %q\n%s", want, out)
		}
	}
}

// ---------- params contract + references-not-values invariant ----------

// TestSecretMovePassesRefsNotValues — the move verb must forward
// from / to / reason as opaque references AND must never carry an inline
// secret value anywhere in the request body. This is the load-bearing
// security invariant of #1580 (mirrors keycloak's
// TestUserCreatePassesSecretRefNotPassword).
func TestSecretMovePassesRefsNotValues(t *testing.T) {
	var captured map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "ok", OpID: opMove,
				Result: json.RawMessage(`{"status":"moved","value_sha256":"abc123","length":24}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newMoveCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{
		"--from", "vault:secret/db/prod#password",
		"--to", "vault:secret/db/standby#password",
		"--reason", "provision standby DB",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}

	if captured["connector_id"] != "secret-broker-1.x" {
		t.Errorf("connector_id: got %v want secret-broker-1.x", captured["connector_id"])
	}
	if captured["op_id"] != opMove {
		t.Errorf("op_id: got %v want %s", captured["op_id"], opMove)
	}
	params, _ := captured["params"].(map[string]any)
	if params == nil {
		t.Fatalf("no params captured: %v", captured)
	}
	if params["from"] != "vault:secret/db/prod#password" {
		t.Errorf("from not forwarded: %v", params["from"])
	}
	if params["to"] != "vault:secret/db/standby#password" {
		t.Errorf("to not forwarded: %v", params["to"])
	}
	if params["reason"] != "provision standby DB" {
		t.Errorf("reason not forwarded: %v", params["reason"])
	}
	// References-not-values: no inline-secret field may reach the wire.
	for _, leaked := range []string{"value", "secret", "password"} {
		if _, ok := params[leaked]; ok {
			t.Errorf("params must NEVER carry an inline %q field; got %v", leaked, params)
		}
	}
	blob, _ := json.Marshal(captured)
	for _, leaked := range []string{`"value"`, `"secret"`, `"password"`} {
		if strings.Contains(string(blob), leaked) {
			t.Errorf("request body carries an inline %s field: %s", leaked, blob)
		}
	}
}

// TestSecretMoveRequiresRefsAndReason — --from / --to / --reason are
// required, and the command exposes NO flag whose name implies a literal
// secret value (mirrors keycloak's TestUserResetPasswordRequiresSecretRef).
func TestSecretMoveRequiresRefsAndReason(t *testing.T) {
	cmd := newMoveCmd()
	for _, name := range []string{"from", "to", "reason"} {
		flag := cmd.Flags().Lookup(name)
		if flag == nil {
			t.Fatalf("move is missing the --%s flag", name)
		}
		if ann := flag.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
			t.Errorf("--%s should be marked required; annotations=%v", name, flag.Annotations)
		}
	}
	for _, banned := range []string{"value", "secret", "password"} {
		if cmd.Flags().Lookup(banned) != nil {
			t.Errorf("move must NOT expose an inline --%s flag", banned)
		}
	}
}

// TestSecretMoveRendersValueFreeResult — a successful move renders only
// the status / value SHA-256 / length, never a value (which the response
// never carries anyway).
func TestSecretMoveRendersValueFreeResult(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "ok", OpID: opMove, DurationMs: 12,
				Result: json.RawMessage(`{"status":"moved","value_sha256":"deadbeef","length":24}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newMoveCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{
		"--from", "vault:a#k", "--to", "vault:b#k", "--reason", "r",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	got := out.String()
	for _, want := range []string{"deadbeef", "24", "moved"} {
		if !strings.Contains(got, want) {
			t.Errorf("rendered output missing %q\n%s", want, got)
		}
	}
}

// TestSecretMoveAwaitingApproval — secret.move is requires_approval=True,
// so an unapproved dispatch returns status=awaiting_approval. The verb
// must render it (NOT classify it as an exit-4 invalid status, which the
// shared dispatch.Render would do) and exit 0 (parked, not failed).
func TestSecretMoveAwaitingApproval(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: statusAwaitingApproval, OpID: opMove, DurationMs: 3,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newMoveCmd()
	var out bytes.Buffer
	var errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--from", "vault:a#k", "--to", "vault:b#k", "--reason", "r",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("awaiting_approval must not be an error (parked, exit 0); got %v", err)
	}
	got := out.String()
	if !strings.Contains(got, statusAwaitingApproval) {
		t.Errorf("output should surface awaiting_approval verbatim\n%s", got)
	}
	if strings.Contains(errBuf.String(), "invalid OperationResult") {
		t.Errorf("awaiting_approval was wrongly rejected as an invalid status: %s", errBuf.String())
	}
}

// TestSecretMoveAwaitingApprovalJSON — with --json the awaiting_approval
// envelope round-trips as the full OperationResult JSON.
func TestSecretMoveAwaitingApprovalJSON(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: statusAwaitingApproval, OpID: opMove,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newMoveCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{
		"--from", "vault:a#k", "--to", "vault:b#k", "--reason", "r",
		"--json", "--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatalf("--json output is not valid JSON: %v\n%s", err, out.String())
	}
	if decoded["status"] != statusAwaitingApproval {
		t.Errorf("json envelope status: got %v want %s", decoded["status"], statusAwaitingApproval)
	}
}
