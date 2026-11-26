// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runnerprincipal

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// principalFields are the fields of one runner principal in an answer of the
// server, without its name and client id.
const principalFields = `"id":"11111111-1111-1111-1111-111111111111",` +
	`"tenant_id":"22222222-2222-2222-2222-222222222222","keycloak_internal_id":"kc-1",` +
	`"owner_sub":"op-sub","created_by_sub":"admin-sub","revoked":false,` +
	`"created_at":"2026-11-01T08:00:00Z","updated_at":"2026-11-02T09:15:00Z",` +
	`"last_seen_at":"2026-11-24T07:45:30Z","wire_version":"1",` +
	`"package_set_digest":"sha256:abcdef"`

// principalJSON is one runner principal as the server sends it. extra holds
// more fields with their leading comma, or is empty.
func principalJSON(name, extra string) string {
	return `{"name":"` + name + `","keycloak_client_id":"runner:` + name + `",` +
		principalFields + extra + `}`
}

// principalOutput is the --json output of the CLI for principalJSON(name, "")
// at the given indent, without the closing brace.
func principalOutput(name, indent string) string {
	lines := []string{
		`"created_at": "2026-11-01T08:00:00Z",`,
		`"created_by_sub": "admin-sub",`,
		`"id": "11111111-1111-1111-1111-111111111111",`,
		`"keycloak_client_id": "runner:` + name + `",`,
		`"keycloak_internal_id": "kc-1",`,
		`"last_seen_at": "2026-11-24T07:45:30Z",`,
		`"name": "` + name + `",`,
		`"owner_sub": "op-sub",`,
		`"package_set_digest": "sha256:abcdef",`,
		`"revoked": false,`,
		`"tenant_id": "22222222-2222-2222-2222-222222222222",`,
		`"updated_at": "2026-11-02T09:15:00Z",`,
		`"wire_version": "1"`,
	}
	return indent + strings.Join(lines, "\n"+indent) + "\n"
}

// standIn serves fixed bodies by method and path.
type standIn map[string]struct {
	status int
	body   string
}

func (s standIn) start(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		answer, ok := s[r.Method+" "+r.URL.Path]
		if !ok {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(answer.status)
		_, _ = w.Write([]byte(answer.body))
	}))
	t.Cleanup(srv.Close)
	seedLogin(t, srv.URL)
	return srv.URL
}

// seedLogin stores a login for backplaneURL in a test config folder.
func seedLogin(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
}

// runCommand runs a real runner-principal command and returns its output.
func runCommand(t *testing.T, cmd *cobra.Command, backplaneURL string, args ...string) string {
	t.Helper()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	cmd.SetArgs(append(args, "--backplane", backplaneURL))
	if err := cmd.Execute(); err != nil {
		t.Fatalf("%s %v: %v\nstderr=%s", cmd.Name(), args, err, stderr.String())
	}
	return stdout.String()
}

// TestJSONOutputWithRunnerChecksOffHasNoSetAsideKey runs list, show,
// register and revoke with --json against answers of a server with
// RUNNER_CHECKS_ENABLED off. Each output must be the fixed text that the CLI
// prints for that answer without any set-aside field.
func TestJSONOutputWithRunnerChecksOffHasNoSetAsideKey(t *testing.T) {
	// No t.Parallel(): seedLogin calls t.Setenv.
	raw := make([]byte, 8)
	if _, err := rand.Read(raw); err != nil {
		t.Fatalf("rand: %v", err)
	}
	code := "enroll-" + hex.EncodeToString(raw)
	registered := `{"name":"site-c","keycloak_client_id":"runner:site-c",` + principalFields +
		`,"enrollment_code":"` + code + `","enrollment_code_expires_at":"2026-11-25T12:00:00Z"}`
	revoked := strings.Replace(principalJSON("site-a", ""), `"revoked":false`, `"revoked":true`, 1)

	url := standIn{
		"GET /api/v1/runner-principals": {http.StatusOK, `{"principals":[` +
			principalJSON("site-a", "") + `,` + principalJSON("site-b", "") + `]}`},
		"GET /api/v1/runner-principals/site-a":           {http.StatusOK, principalJSON("site-a", "")},
		"POST /api/v1/runner-principals":                 {http.StatusCreated, registered},
		"DELETE /api/v1/runner-principals/site-a/revoke": {http.StatusOK, revoked},
	}.start(t)

	registeredWant := "{\n" + strings.Replace(principalOutput("site-c", "  "),
		`  "created_by_sub": "admin-sub",`+"\n",
		`  "created_by_sub": "admin-sub",`+"\n"+
			`  "enrollment_code": "`+code+`",`+"\n"+
			`  "enrollment_code_expires_at": "2026-11-25T12:00:00Z",`+"\n", 1) + "}\n"
	revokedWant := "{\n" + strings.Replace(principalOutput("site-a", "  "),
		`"revoked": false`, `"revoked": true`, 1) + "}\n"

	for _, tc := range []struct {
		name string
		cmd  *cobra.Command
		args []string
		want string
	}{
		{"list", newListCmd(), []string{"--json"}, "{\n  \"principals\": [\n    {\n" +
			principalOutput("site-a", "      ") + "    },\n    {\n" +
			principalOutput("site-b", "      ") + "    }\n  ]\n}\n"},
		{"show", newShowCmd(), []string{"site-a", "--json"}, "{\n" + principalOutput("site-a", "  ") + "}\n"},
		{"register", newRegisterCmd(), []string{"site-c", "--json"}, registeredWant},
		{"revoke", newRevokeCmd(), []string{"site-a", "--json"}, revokedWant},
	} {
		if got := runCommand(t, tc.cmd, url, tc.args...); got != tc.want {
			t.Errorf("%s --json output differs\n got:\n%s\nwant:\n%s", tc.name, got, tc.want)
		}
	}
}

// TestOutputWithRunnerChecksOnHoldsTheKeysOfTheAnswer runs list and show
// against answers of a server with RUNNER_CHECKS_ENABLED on: runner A has a
// count and a time, runner B has the count 0 and no time key.
func TestOutputWithRunnerChecksOnHoldsTheKeysOfTheAnswer(t *testing.T) {
	// No t.Parallel(): seedLogin calls t.Setenv.
	runnerA := principalJSON("runner-a", `,"set_aside_count":3,"set_aside_last_at":"2026-11-20T10:30:00Z"`)
	runnerB := principalJSON("runner-b", `,"set_aside_count":0`)
	url := standIn{
		"GET /api/v1/runner-principals":          {http.StatusOK, `{"principals":[` + runnerA + `,` + runnerB + `]}`},
		"GET /api/v1/runner-principals/runner-b": {http.StatusOK, runnerB},
	}.start(t)

	var listed struct {
		Principals []map[string]any `json:"principals"`
	}
	if err := json.Unmarshal([]byte(runCommand(t, newListCmd(), url, "--json")), &listed); err != nil {
		t.Fatalf("list --json is no JSON: %v", err)
	}
	if len(listed.Principals) != 2 {
		t.Fatalf("want two runners, got %d", len(listed.Principals))
	}
	a, b := listed.Principals[0], listed.Principals[1]
	if a["set_aside_count"] != float64(3) || a["set_aside_last_at"] != "2026-11-20T10:30:00Z" {
		t.Errorf("runner A lacks its count or time: %v", a)
	}
	var shown map[string]any
	if err := json.Unmarshal([]byte(runCommand(t, newShowCmd(), url, "runner-b", "--json")), &shown); err != nil {
		t.Fatalf("show --json is no JSON: %v", err)
	}
	for source, entry := range map[string]map[string]any{"list": b, "show": shown} {
		if entry["set_aside_count"] != float64(0) {
			t.Errorf("%s: runner B lacks the count 0: %v", source, entry)
		}
		if _, ok := entry["set_aside_last_at"]; ok {
			t.Errorf("%s: runner B has a set_aside_last_at key: %v", source, entry)
		}
	}

	table := strings.Split(strings.TrimSpace(runCommand(t, newListCmd(), url)), "\n")
	if got := strings.Fields(table[2]); got[5] != "0" || got[6] != "-" {
		t.Errorf("list: runner B shows 0 and -: %q", table[2])
	}
	summary := runCommand(t, newShowCmd(), url, "runner-b")
	for _, line := range []string{"set_aside_count:     0\n", "set_aside_last_at:   -\n"} {
		if !strings.Contains(summary, line) {
			t.Errorf("show: output lacks %q:\n%s", line, summary)
		}
	}
}
