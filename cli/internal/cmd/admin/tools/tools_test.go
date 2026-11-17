// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package tools

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

// primeToken writes a backplane config and a stored token, so the verb
// can call the fake backplane.
func primeToken(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cfg := filepath.Join(dir, "meho", "config.json")
	if err := os.MkdirAll(filepath.Dir(cfg), 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	blob, _ := json.Marshal(map[string]string{"backplane_url": backplaneURL})
	if err := os.WriteFile(cfg, blob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken: "test-bearer", BackplaneURL: backplaneURL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
}

// runSignOut runs `meho admin tools sign-out` against a fake backplane
// that answers with *answer* (or *httpStatus* when it is not 200).
func runSignOut(
	t *testing.T, httpStatus int, answer dispatch.CallResult, args ...string,
) (dispatch.CallRequestBody, string, string, error) {
	t.Helper()
	var body dispatch.CallRequestBody
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/operations/call" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(httpStatus)
		_ = json.NewEncoder(w).Encode(answer)
	}))
	defer srv.Close()
	primeToken(t, srv.URL)

	var out, errOut bytes.Buffer
	cmd := NewRootCmd()
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	cmd.SetArgs(append([]string{"sign-out", "--backplane", srv.URL}, args...))
	err := cmd.Execute()
	return body, out.String(), errOut.String(), err
}

func TestSignOutSendsTheOperation(t *testing.T) {
	body, out, _, err := runSignOut(t, http.StatusOK, dispatch.CallResult{
		Status: "ok", OpID: SignOutOpID,
		Result: json.RawMessage(`{"user_id":"u-1","username":"bob","outcome":"signed_out"}`),
	}, "--user", "bob", "--tool", "claude-desktop")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if body.ConnectorID != "tools-admin-1.x" || body.OpID != "tools.sign_out" {
		t.Errorf("dispatched %q / %q", body.ConnectorID, body.OpID)
	}
	if body.Target != nil {
		t.Errorf("target must be empty, got %v", body.Target)
	}
	if len(body.Params) != 2 || body.Params["user"] != "bob" || body.Params["tool"] != "claude-desktop" {
		t.Errorf("params: got %v", body.Params)
	}
	if !strings.Contains(out, "signed_out") {
		t.Errorf("output does not show the outcome: %s", out)
	}
}

func TestSignOutWaitingForApprovalIsNotAnError(t *testing.T) {
	_, out, _, err := runSignOut(t, http.StatusOK, dispatch.CallResult{
		Status: "awaiting_approval", OpID: SignOutOpID,
		Extras: json.RawMessage(`{"approval_request_id":"r-1"}`),
	}, "--user", "bob", "--tool", "claude-desktop")
	if err != nil {
		t.Fatalf("awaiting_approval must exit 0; got %v", err)
	}
	if !strings.Contains(out, "meho approvals show r-1") {
		t.Errorf("output does not name the approval: %s", out)
	}
}

func TestSignOutErrorIsAnError(t *testing.T) {
	msg := "tools.sign_out: MEHO's sign-in realm has no person 'nobody' in your tenant"
	_, out, _, err := runSignOut(t, http.StatusOK, dispatch.CallResult{
		Status: "error", OpID: SignOutOpID, Error: &msg,
	}, "--user", "nobody", "--tool", "claude-desktop")
	if err == nil {
		t.Fatalf("an error result must fail the command")
	}
	if !strings.Contains(out, "in your tenant") {
		t.Errorf("output does not show the error: %s", out)
	}
}

func TestSignOutHTTPFailureIsAnError(t *testing.T) {
	_, _, errOut, err := runSignOut(t, http.StatusInternalServerError, dispatch.CallResult{},
		"--user", "bob", "--tool", "cli")
	if err == nil {
		t.Fatalf("an HTTP 500 must fail the command")
	}
	if !strings.Contains(errOut, "HTTP 500") {
		t.Errorf("stderr does not name the status: %s", errOut)
	}
}

func TestSignOutNeedsUserAndTool(t *testing.T) {
	for _, args := range [][]string{{"--user", "bob"}, {"--tool", "cli"}, {}} {
		cmd := NewRootCmd()
		cmd.SetOut(&bytes.Buffer{})
		cmd.SetErr(&bytes.Buffer{})
		cmd.SetArgs(append([]string{"sign-out", "--backplane", "https://meho.example.com"}, args...))
		if err := cmd.Execute(); err == nil {
			t.Errorf("sign-out %v must fail", args)
		}
	}
}
