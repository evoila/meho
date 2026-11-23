// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

const (
	previewMark   = "***REDACTED***"
	previewSecret = "FAKE-made-up-pw-4417"
)

// previewMarkServer answers every preview with the envelope the backplane
// sends for an ingested credential write: each secret input is the fixed
// mark, in `redacted_body` and in `query`. It counts the requests it gets.
func previewMarkServer(t *testing.T, requests *int) *httptest.Server {
	t.Helper()
	const envelope = `{"status":"ok","op_id":"PUT:/repos/{owner}/{repo}/actions/secrets/{name}",` +
		`"connector_id":"github-rest-1.x","source_kind":"ingested","method":"PUT",` +
		`"resolved_path":"/repos/evoila/meho/actions/secrets/DEPLOY_KEY",` +
		`"query":{"api_key":"***REDACTED***","visibility":"private"},` +
		`"redacted_body":{"password":"***REDACTED***","name":"svc"},` +
		`"preview_hash":"abc123","safety_level":"caution","requires_approval":false,` +
		`"policy_verdict":"auto-execute","policy_reason":null}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/operations/preview" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), previewSecret) {
			t.Errorf("the request body does not carry the --params value: %s", body)
		}
		*requests++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(envelope))
	}))
	t.Cleanup(srv.Close)
	return srv
}

// TestPreviewOfCredentialWritePrintsTheMarkNotTheValue runs
// `meho operation preview` against a stand-in server. The plain and the
// JSON output show the mark the server sends, and never the value given
// in --params.
func TestPreviewOfCredentialWritePrintsTheMarkNotTheValue(t *testing.T) {
	for _, jsonOut := range []bool{false, true} {
		name := "plain"
		if jsonOut {
			name = "json"
		}
		t.Run(name, func(t *testing.T) {
			requests := 0
			srv := previewMarkServer(t, &requests)
			gen, err := api.NewClientWithResponses(srv.URL)
			if err != nil {
				t.Fatalf("new client: %v", err)
			}
			withFakeClient(t, realClient{gen})

			cmd := newPreviewCmd()
			var out, errOut bytes.Buffer
			cmd.SetOut(&out)
			cmd.SetErr(&errOut)
			args := []string{
				"github-rest-1.x", "PUT:/repos/{owner}/{repo}/actions/secrets/{name}",
				"--backplane", srv.URL,
				"--params", `{"owner":"evoila","repo":"meho","name":"DEPLOY_KEY",` +
					`"api_key":"` + previewSecret + `","visibility":"private",` +
					`"password":"` + previewSecret + `"}`,
			}
			if jsonOut {
				args = append(args, "--json")
			}
			cmd.SetArgs(args)
			cmd.SetContext(context.Background())

			if err := cmd.Execute(); err != nil {
				t.Fatalf("preview failed: %v (stderr: %s)", err, errOut.String())
			}
			if requests != 1 {
				t.Fatalf("server got %d requests, want 1", requests)
			}
			printed := out.String() + errOut.String()
			if strings.Contains(printed, previewSecret) {
				t.Errorf("output shows the --params value:\n%s", printed)
			}
			if got := strings.Count(out.String(), previewMark); got != 2 {
				t.Errorf("output shows the mark %d times, want 2 (query and body):\n%s", got, out.String())
			}
			for _, want := range []string{`"api_key"`, `"password"`, "abc123"} {
				if !strings.Contains(out.String(), want) {
					t.Errorf("output misses %q:\n%s", want, out.String())
				}
			}
		})
	}
}
