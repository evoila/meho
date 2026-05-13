// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
)

// TestParseOpArgs verifies that parseOpArgs correctly extracts --target,
// --json, and arbitrary --key=value params from the raw args cobra passes
// when DisableFlagParsing = true.
func TestParseOpArgs(t *testing.T) {
	tests := []struct {
		name       string
		args       []string
		wantTarget string
		wantJSON   bool
		wantParams map[string]interface{}
	}{
		{
			name:       "target and path",
			args:       []string{"--target", "vault-test", "--path", "secret/meho/test/federation"},
			wantTarget: "vault-test",
			wantParams: map[string]interface{}{"path": "secret/meho/test/federation"},
		},
		{
			name:       "equals form",
			args:       []string{"--target=vault-test", "--path=secret/x"},
			wantTarget: "vault-test",
			wantParams: map[string]interface{}{"path": "secret/x"},
		},
		{
			name:       "json flag",
			args:       []string{"--target", "t", "--json"},
			wantTarget: "t",
			wantJSON:   true,
			wantParams: map[string]interface{}{},
		},
		{
			name:       "multiple params",
			args:       []string{"--target", "t", "--key1", "v1", "--key2", "v2"},
			wantTarget: "t",
			wantParams: map[string]interface{}{"key1": "v1", "key2": "v2"},
		},
		{
			name:       "empty args",
			args:       []string{},
			wantTarget: "",
			wantParams: map[string]interface{}{},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseOpArgs(tc.args)
			if err != nil {
				t.Fatalf("parseOpArgs: unexpected error: %v", err)
			}

			if got.target != tc.wantTarget {
				t.Errorf("target: got %q want %q", got.target, tc.wantTarget)
			}
			if got.jsonOut != tc.wantJSON {
				t.Errorf("jsonOut: got %v want %v", got.jsonOut, tc.wantJSON)
			}
			if tc.wantParams != nil {
				for k, v := range tc.wantParams {
					if got.params[k] != v {
						t.Errorf("params[%q]: got %v want %v", k, got.params[k], v)
					}
				}
			}
		})
	}
}

// TestParseOpArgsReservedFlagsMissingValue verifies that reserved flags
// (--target, --backplane, --params) return an error when no value is provided.
func TestParseOpArgsReservedFlagsMissingValue(t *testing.T) {
	for _, flag := range []string{"target", "backplane", "params"} {
		t.Run(flag, func(t *testing.T) {
			_, err := parseOpArgs([]string{"--" + flag})
			if err == nil {
				t.Errorf("--%s with no value: expected error, got nil", flag)
			}
		})
	}
}

// TestConnectorCmdURLConstruction verifies that the connector command
// builds the correct URL path and sends the right JSON body.
func TestConnectorCmdURLConstruction(t *testing.T) {
	var capturedPath string
	var capturedBody []byte

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capturedPath = r.URL.Path
		capturedBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		result := map[string]interface{}{
			"status":      "ok",
			"op_id":       "vault.kv.read",
			"result":      map[string]interface{}{"api_key": "s3cr3t"},
			"duration_ms": 12.3,
			"extras":      map[string]interface{}{},
		}
		_ = json.NewEncoder(w).Encode(result)
	}))
	defer srv.Close()

	xdg := withTempXDG(t)
	const testToken = "eyJ.TEST-CONNECTOR.TOKEN"
	seedCreds(t, xdg, srv.URL, auth.StoredToken{
		AccessToken:  testToken,
		RefreshToken: "rt",
	})

	var out, errOut bytes.Buffer
	root := newRootCmd()
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetContext(context.Background())

	root.SetArgs([]string{
		"vault", "kv.read",
		"--target", "vault-test",
		"--path", "secret/meho/test/federation",
	})
	if err := root.Execute(); err != nil {
		t.Fatalf("Execute: %v (stderr: %s)", err, errOut.String())
	}

	if capturedPath != "/api/v1/connectors/vault/kv.read" {
		t.Errorf("URL path: got %q want %q", capturedPath, "/api/v1/connectors/vault/kv.read")
	}

	var reqBody struct {
		Target string                 `json:"target"`
		Params map[string]interface{} `json:"params"`
	}
	if err := json.Unmarshal(capturedBody, &reqBody); err != nil {
		t.Fatalf("decode captured body: %v", err)
	}
	if reqBody.Target != "vault-test" {
		t.Errorf("body.target: got %q want %q", reqBody.Target, "vault-test")
	}
	if reqBody.Params["path"] != "secret/meho/test/federation" {
		t.Errorf("body.params.path: got %v", reqBody.Params["path"])
	}

	// Human output should contain the secret value.
	if !bytes.Contains(out.Bytes(), []byte("s3cr3t")) {
		t.Errorf("stdout should contain secret value; got: %s", out.String())
	}
}

// TestConnectorCmdUnknownProduct verifies the 404 handler.
func TestConnectorCmdUnknownProduct(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail": "unknown product: bad-product"}`))
	}))
	defer srv.Close()

	xdg := withTempXDG(t)
	seedCreds(t, xdg, srv.URL, auth.StoredToken{
		AccessToken:  "eyJ.TEST.TOKEN",
		RefreshToken: "rt",
	})

	// Temporarily register a "bad-product" connector so cobra routes the command.
	knownConnectors = append(knownConnectors, connectorSpec{product: "bad-product", ops: []string{"kv.read"}})
	t.Cleanup(func() {
		knownConnectors = knownConnectors[:len(knownConnectors)-1]
	})

	var out, errOut bytes.Buffer
	root := newRootCmd()
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetContext(context.Background())
	root.SetArgs([]string{"bad-product", "kv.read", "--target", "x"})

	err := root.Execute()
	if err == nil {
		t.Fatal("expected error for 404 response")
	}
	if !bytes.Contains(errOut.Bytes(), []byte("unknown connector product")) {
		t.Errorf("expected 'unknown connector product' in stderr; got: %s", errOut.String())
	}
}

// TestConnectorCmdUnknownOp verifies the 400 handler surfaces the known_ops list.
func TestConnectorCmdUnknownOp(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"detail": {"error": "unknown_op", "op_id": "vault.bad.op", "known_ops": ["vault.kv.read"]}}`))
	}))
	defer srv.Close()

	xdg := withTempXDG(t)
	seedCreds(t, xdg, srv.URL, auth.StoredToken{
		AccessToken:  "eyJ.TEST.TOKEN",
		RefreshToken: "rt",
	})

	// Register a test op "bad.op" on vault temporarily.
	knownConnectors[0].ops = append(knownConnectors[0].ops, "bad.op")
	t.Cleanup(func() {
		knownConnectors[0].ops = knownConnectors[0].ops[:len(knownConnectors[0].ops)-1]
	})

	var out, errOut bytes.Buffer
	root := newRootCmd()
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetContext(context.Background())
	root.SetArgs([]string{"vault", "bad.op", "--target", "x"})

	err := root.Execute()
	if err == nil {
		t.Fatal("expected error for 400 response")
	}
	if !bytes.Contains(errOut.Bytes(), []byte("vault.kv.read")) {
		t.Errorf("expected known_ops in stderr; got: %s", errOut.String())
	}
}
