// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// ---------- the governed delete verbs (#3944) ----------

func TestDeleteVerbsExist(t *testing.T) {
	parents := map[string]*cobra.Command{
		"user":   newUserCmd(),
		"group":  newGroupCmd(),
		"client": newClientCmd(),
	}
	for noun, parent := range parents {
		found := false
		for _, sub := range parent.Commands() {
			if sub.Name() == "delete" {
				found = true
			}
		}
		if !found {
			t.Errorf("%s is missing the delete sub-verb", noun)
		}
	}
}

// runCmd executes cmd with args and returns stdout, stderr and the error.
func runCmd(cmd *cobra.Command, args ...string) (string, string, error) {
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs(args)
	err := cmd.Execute()
	return out.String(), errBuf.String(), err
}

// TestUserDeleteWithoutHashOnlyPreviews — step 1 calls the preview route,
// never the call route, and prints the hash plus the next step.
func TestUserDeleteWithoutHashOnlyPreviews(t *testing.T) {
	var previewBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&previewBody); err != nil {
				t.Errorf("decode: %v", err)
			}
			writeJSON(t, w, 200, map[string]any{
				"status": "ok", "op_id": "keycloak.user.delete", "preview_hash": "hash-1",
			})
		},
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			t.Errorf("step 1 must not call the operation")
			w.WriteHeader(500)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, _, err := runCmd(newUserDeleteCmd(),
		"--target", "kc", "--username", "event-guest", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("preview step should exit 0; got %v", err)
	}
	if previewBody["connector_id"] != ConnectorID || previewBody["op_id"] != "keycloak.user.delete" {
		t.Errorf("preview identity wrong: %v", previewBody)
	}
	params, _ := previewBody["params"].(map[string]any)
	if params["username"] != "event-guest" {
		t.Errorf("username not sent: %v", previewBody["params"])
	}
	for _, want := range []string{"preview_hash: hash-1", "--preview-hash hash-1", "Nothing was deleted"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q; got %q", want, out)
		}
	}
}

// TestUserDeleteWithHashAsksForApproval — step 2 sends the hash on the
// call; a request that waits for approval exits 0 with the parked hint.
func TestUserDeleteWithHashAsksForApproval(t *testing.T) {
	var callBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&callBody); err != nil {
				t.Errorf("decode: %v", err)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "awaiting_approval", OpID: "keycloak.user.delete",
				Extras: json.RawMessage(`{"approval_request_id":"ar-del-1"}`),
			})
		},
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, _ *http.Request) {
			t.Errorf("step 2 must not preview again")
			w.WriteHeader(500)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, _, err := runCmd(newUserDeleteCmd(),
		"--target", "kc", "--id", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
		"--preview-hash", "hash-1", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("waiting for approval is not an error; got %v", err)
	}
	if callBody["preview_hash"] != "hash-1" {
		t.Errorf("preview_hash not sent: %v", callBody["preview_hash"])
	}
	params, _ := callBody["params"].(map[string]any)
	if params["id"] != "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" {
		t.Errorf("id not sent: %v", callBody["params"])
	}
	if !strings.Contains(out, "parked for human approval") || !strings.Contains(out, "ar-del-1") {
		t.Errorf("expected the parked hint and approval id; got %q", out)
	}
}

// TestGroupDeleteSendsDeleteSubgroups — the flag becomes the op param.
func TestGroupDeleteSendsDeleteSubgroups(t *testing.T) {
	var params map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
			}
			params, _ = body["params"].(map[string]any)
			writeJSON(t, w, 200, map[string]any{"status": "ok", "preview_hash": "h"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, _, err := runCmd(newGroupDeleteCmd(), "--target", "kc", "--name", "event-team",
		"--delete-subgroups", "--backplane", srv.URL); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if params["name"] != "event-team" || params["delete_subgroups"] != true {
		t.Errorf("group params wrong: %v", params)
	}
}

// TestClientDeleteMapsClientIDFlag — --client-id fills the client_id param.
func TestClientDeleteMapsClientIDFlag(t *testing.T) {
	var params map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("decode: %v", err)
			}
			params, _ = body["params"].(map[string]any)
			writeJSON(t, w, 200, map[string]any{"status": "ok", "preview_hash": "h"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, _, err := runCmd(newClientDeleteCmd(), "--target", "kc", "--client-id", "event-app",
		"--backplane", srv.URL); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if params["client_id"] != "event-app" {
		t.Errorf("client_id not sent: %v", params)
	}
	if _, has := params["delete_subgroups"]; has {
		t.Errorf("client delete must not send delete_subgroups: %v", params)
	}
	if newClientDeleteCmd().Flags().Lookup("delete-subgroups") != nil {
		t.Errorf("--delete-subgroups belongs to group delete only")
	}
}

// TestDeleteNeedsIDOrName — neither flag fails before any request.
func TestDeleteNeedsIDOrName(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"": func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("no request expected; got %s %s", r.Method, r.URL.Path)
			w.WriteHeader(500)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	_, stderr, err := runCmd(newUserDeleteCmd(), "--target", "kc", "--backplane", srv.URL)
	if err == nil {
		t.Fatalf("expected an error without --id or --username")
	}
	if !strings.Contains(stderr, "--username") {
		t.Errorf("error should name the flags; got %q", stderr)
	}
}

// TestDeleteRefusalShowsTheReason — a guard refusal (status=denied) exits
// non-zero and prints the reason from the backplane.
func TestDeleteRefusalShowsTheReason(t *testing.T) {
	reason := "keycloak.client.delete refused (builtin_client): 'admin-cli' is a client Keycloak creates in every realm."
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			errMsg := "denied: destructive op requires a blast-radius statement"
			extras, _ := json.Marshal(map[string]any{"error_code": "blast_radius_required", "reason": reason})
			writeJSON(t, w, 200, CallResult{
				Status: "denied", OpID: "keycloak.client.delete", Error: &errMsg, Extras: extras,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, _, err := runCmd(newClientDeleteCmd(), "--target", "kc", "--client-id", "admin-cli",
		"--preview-hash", "h", "--backplane", srv.URL)
	if err == nil {
		t.Fatalf("a refusal must exit non-zero")
	}
	if !strings.Contains(out, "builtin_client") {
		t.Errorf("output should carry the refusal reason; got %q", out)
	}
}

// TestDeletePreviewErrorExitsNonZero — a preview the backplane cannot make
// (for example invalid params) exits 1 with the error.
func TestDeletePreviewErrorExitsNonZero(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, map[string]any{
				"status": "error", "error": "invalid_params: 'id' does not match pattern",
				"extras": map[string]any{"error_code": "invalid_params"},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, _, err := runCmd(newUserDeleteCmd(), "--target", "kc", "--id", "not-a-uuid",
		"--backplane", srv.URL)
	if err == nil {
		t.Fatalf("a failed preview must exit non-zero")
	}
	if !strings.Contains(out, "invalid_params") || strings.Contains(out, "preview_hash:") {
		t.Errorf("expected the error and no hash; got %q", out)
	}
}

// TestDeletePreviewJSONPrintsTheEnvelope — --json on step 1 prints the
// preview envelope, including the hash.
func TestDeletePreviewJSONPrintsTheEnvelope(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, map[string]any{
				"status": "ok", "preview_hash": "hash-json", "method": "COMPOSITE",
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, _, err := runCmd(newGroupDeleteCmd(), "--target", "kc", "--id",
		"77777777-7777-7777-7777-777777777777", "--json", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	var env map[string]any
	if err := json.Unmarshal([]byte(out), &env); err != nil {
		t.Fatalf("--json output must be one JSON object; got %q (%v)", out, err)
	}
	if env["preview_hash"] != "hash-json" || env["method"] != "COMPOSITE" {
		t.Errorf("envelope not printed in full: %v", env)
	}
}

// ---------- keycloak realm delete (#3886) ----------

// TestRealmDeleteVerbExists — `realm delete` hangs under `realm`.
func TestRealmDeleteVerbExists(t *testing.T) {
	found := false
	for _, sub := range newRealmCmd().Commands() {
		if sub.Name() == "delete" {
			found = true
		}
	}
	if !found {
		t.Errorf("realm is missing the delete sub-verb")
	}
}

// TestRealmDeletePreviewThenApproval — step 1 previews with the realm param;
// step 2 sends the same params plus the hash on the call route.
func TestRealmDeletePreviewThenApproval(t *testing.T) {
	var previewBody, callBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&previewBody); err != nil {
				t.Errorf("decode: %v", err)
			}
			writeJSON(t, w, 200, map[string]any{
				"status": "ok", "op_id": "keycloak.realm.delete", "preview_hash": "hash-r",
			})
		},
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&callBody); err != nil {
				t.Errorf("decode: %v", err)
			}
			writeJSON(t, w, 200, CallResult{
				Status: "awaiting_approval", OpID: "keycloak.realm.delete",
				Extras: json.RawMessage(`{"approval_request_id":"ar-realm-1"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, _, err := runCmd(newRealmDeleteCmd(),
		"--target", "kc", "--realm", "test-run-7", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("preview step should exit 0; got %v", err)
	}
	if previewBody["op_id"] != "keycloak.realm.delete" || previewBody["connector_id"] != ConnectorID {
		t.Errorf("preview identity wrong: %v", previewBody)
	}
	params, _ := previewBody["params"].(map[string]any)
	if len(params) != 1 || params["realm"] != "test-run-7" {
		t.Errorf("preview params wrong: %v", previewBody["params"])
	}
	if !strings.Contains(out, "--preview-hash hash-r") {
		t.Errorf("output should name the next step; got %q", out)
	}
	if callBody != nil {
		t.Errorf("step 1 must not call the operation")
	}

	out, _, err = runCmd(newRealmDeleteCmd(), "--target", "kc", "--realm", "test-run-7",
		"--preview-hash", "hash-r", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("waiting for approval is not an error; got %v", err)
	}
	if callBody["preview_hash"] != "hash-r" {
		t.Errorf("preview_hash not sent: %v", callBody["preview_hash"])
	}
	params, _ = callBody["params"].(map[string]any)
	if params["realm"] != "test-run-7" {
		t.Errorf("realm not sent: %v", callBody["params"])
	}
	if !strings.Contains(out, "ar-realm-1") {
		t.Errorf("expected the approval id; got %q", out)
	}
}

// TestRealmDeleteNeedsRealm — no --realm fails before any request; the verb
// never falls back to the target's managed realm.
func TestRealmDeleteNeedsRealm(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"": func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("no request expected; got %s %s", r.Method, r.URL.Path)
			w.WriteHeader(500)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	_, stderr, err := runCmd(newRealmDeleteCmd(), "--target", "kc", "--backplane", srv.URL)
	if err == nil {
		t.Fatalf("expected an error without --realm")
	}
	if !strings.Contains(stderr, "--realm") {
		t.Errorf("error should name --realm; got %q", stderr)
	}
}
