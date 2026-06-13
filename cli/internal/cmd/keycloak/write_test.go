// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ---------- command-tree shape tests for the write verbs (#1406) ----------

func TestRealmHasWriteVerbs(t *testing.T) {
	got := map[string]bool{}
	for _, s := range newRealmCmd().Commands() {
		got[s.Name()] = true
	}
	for _, name := range []string{"get", "create", "update"} {
		if !got[name] {
			t.Errorf("realm is missing sub-verb %q", name)
		}
	}
}

func TestClientHasWriteVerbs(t *testing.T) {
	got := map[string]bool{}
	for _, s := range newClientCmd().Commands() {
		got[s.Name()] = true
	}
	for _, name := range []string{"create", "update"} {
		if !got[name] {
			t.Errorf("client is missing sub-verb %q", name)
		}
	}
}

func TestUserHasWriteVerbs(t *testing.T) {
	got := map[string]bool{}
	for _, s := range newUserCmd().Commands() {
		got[s.Name()] = true
	}
	for _, name := range []string{"create", "reset-password"} {
		if !got[name] {
			t.Errorf("user is missing sub-verb %q", name)
		}
	}
}

func TestProtocolMapperHasCreate(t *testing.T) {
	got := map[string]bool{}
	for _, s := range newProtocolMapperCmd().Commands() {
		got[s.Name()] = true
	}
	if !got["create"] {
		t.Errorf("protocol-mapper is missing the create sub-verb")
	}
}

func TestRoleMappingHasAssign(t *testing.T) {
	got := map[string]bool{}
	for _, s := range newRoleMappingCmd().Commands() {
		got[s.Name()] = true
	}
	if !got["assign"] {
		t.Errorf("role-mapping is missing the assign sub-verb")
	}
}

// ---------- representation-file loader ----------

func TestLoadRepresentationHappy(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "rep.json")
	if err := os.WriteFile(path, []byte(`{"realm":"evba","enabled":true}`), 0o600); err != nil {
		t.Fatalf("write rep: %v", err)
	}
	rep, serr := loadRepresentation(path)
	if serr != nil {
		t.Fatalf("loadRepresentation: %v", serr)
	}
	if rep["realm"] != "evba" {
		t.Errorf("realm: got %v", rep["realm"])
	}
}

func TestLoadRepresentationRejectsNonObject(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "rep.json")
	if err := os.WriteFile(path, []byte(`[1,2,3]`), 0o600); err != nil {
		t.Fatalf("write rep: %v", err)
	}
	if _, serr := loadRepresentation(path); serr == nil {
		t.Errorf("loadRepresentation should reject a non-object JSON body")
	}
}

func TestLoadRepresentationMissingFile(t *testing.T) {
	if _, serr := loadRepresentation(filepath.Join(t.TempDir(), "nope.json")); serr == nil {
		t.Errorf("loadRepresentation should error on a missing file")
	}
}

// ---------- dispatch shape: every write op uses its canonical op_id ----------

func TestWriteOpsDispatchCanonicalOpIDs(t *testing.T) {
	dispatched := make(map[string]bool)
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			var body callRequestBody
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			if body.ConnectorID != "keycloak-admin-26.x" {
				t.Errorf("connector_id: got %q", body.ConnectorID)
			}
			dispatched[body.OpID] = true
			writeJSON(t, w, 200, CallResult{
				Status: "ok", OpID: body.OpID,
				Result: json.RawMessage(`{"created":true,"conflict":false}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	writeOps := []string{
		"keycloak.realm.create",
		"keycloak.realm.update",
		"keycloak.client.create",
		"keycloak.client.update",
		"keycloak.client_scope.create",
		"keycloak.protocol_mapper.create",
		"keycloak.user.create",
		"keycloak.user.reset_password",
		"keycloak.role_mapping.assign",
	}
	for _, opID := range writeOps {
		if _, err := dispatchOp(context.Background(), srv.URL, opID, "rdc-keycloak", nil); err != nil {
			t.Fatalf("dispatchOp %s: %v", opID, err)
		}
	}
	for _, opID := range writeOps {
		if !dispatched[opID] {
			t.Errorf("write op_id %q was not dispatched", opID)
		}
	}
}

// ---------- password is never on the command line ----------

// TestUserCreatePassesSecretRefNotPassword — the user create verb must
// send password_secret_ref (a Vault path) in params and NEVER an inline
// password. This is the load-bearing security invariant of #1406.
func TestUserCreatePassesSecretRefNotPassword(t *testing.T) {
	var captured map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "keycloak.user.create",
				Result: json.RawMessage(`{"username":"operator-a","created":true}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	dir := t.TempDir()
	repPath := filepath.Join(dir, "user.json")
	if err := os.WriteFile(repPath, []byte(`{"username":"operator-a","enabled":true}`), 0o600); err != nil {
		t.Fatalf("write rep: %v", err)
	}

	cmd := newUserCreateCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{
		"--target", "rdc-keycloak",
		"--representation-file", repPath,
		"--password-secret-ref", "rdc/keycloak/operator-a",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}

	params, _ := captured["params"].(map[string]any)
	if params == nil {
		t.Fatalf("no params captured: %v", captured)
	}
	if params["password_secret_ref"] != "rdc/keycloak/operator-a" {
		t.Errorf("password_secret_ref not forwarded: %v", params["password_secret_ref"])
	}
	if _, leaked := params["password"]; leaked {
		t.Errorf("password must NEVER appear in op params; got %v", params)
	}
	// Belt-and-suspenders: the literal flag value must not appear as an
	// inline password anywhere in the request body.
	blob, _ := json.Marshal(captured)
	if strings.Contains(string(blob), `"password"`) {
		t.Errorf("request body carries an inline password field: %s", blob)
	}
}

// TestUserResetPasswordRequiresSecretRef — reset-password marks
// --password-secret-ref required so a missing Vault path fails before
// dispatch.
func TestUserResetPasswordRequiresSecretRef(t *testing.T) {
	cmd := newUserResetPasswordCmd()
	flag := cmd.Flags().Lookup("password-secret-ref")
	if flag == nil {
		t.Fatalf("reset-password is missing --password-secret-ref")
	}
	if ann := flag.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
		t.Errorf("--password-secret-ref should be required; annotations=%v", flag.Annotations)
	}
	// And there must be no inline --password flag at all.
	if cmd.Flags().Lookup("password") != nil {
		t.Errorf("reset-password must NOT expose an inline --password flag")
	}
}

func TestRoleMappingAssignForwardsRoles(t *testing.T) {
	var captured map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, CallResult{Status: "ok", OpID: "keycloak.role_mapping.assign",
				Result: json.RawMessage(`{"assigned_roles":["tenant_admin"]}`)})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newRoleMappingAssignCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{
		"--target", "rdc-keycloak",
		"--username", "operator-a",
		"--role", "tenant_admin",
		"--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	params, _ := captured["params"].(map[string]any)
	if params == nil {
		t.Fatalf("no params captured")
	}
	roles, _ := params["roles"].([]any)
	if len(roles) != 1 || roles[0] != "tenant_admin" {
		t.Errorf("roles not forwarded: %v", params["roles"])
	}
	if params["username"] != "operator-a" {
		t.Errorf("username not forwarded: %v", params["username"])
	}
}

// TestRoleMappingAssignAwaitingApprovalRealPath — drives the REAL
// end-to-end path (fake backplane returns status=awaiting_approval →
// dispatchWrite → renderCallResult → conn.Render). The shared
// dispatch.Render intercepts the park as a non-error outcome: the
// command exits 0, stdout carries the parked hint, and stderr never
// carries the invalid-status diagnostic.
func TestRoleMappingAssignAwaitingApprovalRealPath(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, CallResult{
				Status: "awaiting_approval", OpID: "keycloak.role_mapping.assign", DurationMs: 4,
				Extras: json.RawMessage(`{"approval_request_id":"ar-kc-1"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := newRoleMappingAssignCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "rdc-keycloak", "--username", "operator-a",
		"--role", "tenant_admin", "--backplane", srv.URL,
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("awaiting_approval must not be an error (parked, exit 0); got %v", err)
	}
	if !strings.Contains(out.String(), "parked for human approval") {
		t.Errorf("expected parked hint on stdout; got %q", out.String())
	}
	if strings.Contains(errBuf.String(), "invalid OperationResult") {
		t.Errorf("awaiting_approval was wrongly rejected as invalid status: %s", errBuf.String())
	}
}
