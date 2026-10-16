// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestUserHasLogoutVerb(t *testing.T) {
	for _, s := range newUserCmd().Commands() {
		if s.Name() == "logout" {
			return
		}
	}
	t.Errorf("user is missing the logout sub-verb")
}

// runLogout executes `meho keycloak user logout` against a fake backplane
// that answers with status and result, and returns the captured call body
// and stdout.
func runLogout(t *testing.T, status, result string, args ...string) (callRequestBody, string, error) {
	t.Helper()
	var captured callRequestBody
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, CallResult{
				Status: status, OpID: "keycloak.user.logout", DurationMs: 3,
				Result: json.RawMessage(result),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	var out bytes.Buffer
	cmd := newUserLogoutCmd()
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs(append([]string{"--target", "my-keycloak", "--backplane", srv.URL}, args...))
	err := cmd.Execute()
	return captured, out.String(), err
}

func TestUserLogoutByUsernameDispatchesCanonicalOp(t *testing.T) {
	body, out, err := runLogout(t, "ok",
		`{"id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","username":"event-guest","logged_out":true}`,
		"--username", "event-guest")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if body.ConnectorID != "keycloak-admin-26.x" || body.OpID != "keycloak.user.logout" {
		t.Errorf("dispatched %q / %q", body.ConnectorID, body.OpID)
	}
	if body.Target["name"] != "my-keycloak" {
		t.Errorf("target: got %v", body.Target)
	}
	if len(body.Params) != 1 || body.Params["username"] != "event-guest" {
		t.Errorf("params: got %v", body.Params)
	}
	if !strings.Contains(out, "logged_out:") || !strings.Contains(out, "true") {
		t.Errorf("output does not show logged_out: %s", out)
	}
}

func TestUserLogoutByIDSendsID(t *testing.T) {
	body, _, err := runLogout(t, "ok", `{"id":"u-1","logged_out":true}`, "--id", "u-1")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if len(body.Params) != 1 || body.Params["id"] != "u-1" {
		t.Errorf("params: got %v", body.Params)
	}
}

func TestUserLogoutAwaitingApprovalIsNotAnError(t *testing.T) {
	if _, _, err := runLogout(t, "awaiting_approval", `null`, "--username", "event-guest"); err != nil {
		t.Fatalf("awaiting_approval must exit 0; got %v", err)
	}
}

func TestUserLogoutRequiresIDOrUsername(t *testing.T) {
	cmd := newUserLogoutCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"--target", "my-keycloak"})
	if err := cmd.Execute(); err == nil {
		t.Fatalf("logout without --id or --username must fail")
	}
}
