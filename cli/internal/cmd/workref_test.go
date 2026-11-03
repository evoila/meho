// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

const testWorkRef = "gh:evoila/meho#3804"

// workRefRecorder is a fake backplane. It answers every request with a
// 500 (the verbs under test only need to send their request) and keeps
// the Meho-Work-Ref header seen per "METHOD path".
type workRefRecorder struct {
	mu   sync.Mutex
	seen map[string]string
}

func newWorkRefBackplane(t *testing.T) (*httptest.Server, *workRefRecorder) {
	t.Helper()
	rec := &workRefRecorder{seen: map[string]string{}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.mu.Lock()
		rec.seen[r.Method+" "+r.URL.Path] = r.Header.Get(api.WorkRefHeader)
		rec.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"detail":"test backplane"}`))
	}))
	t.Cleanup(srv.Close)

	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	seedCreds(t, xdg, srv.URL, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(time.Hour),
	})
	return srv, rec
}

func (r *workRefRecorder) header(t *testing.T, methodPath string) string {
	t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()
	v, ok := r.seen[methodPath]
	if !ok {
		t.Fatalf("backplane never saw %q; saw %v", methodPath, r.seen)
	}
	return v
}

func runRoot(t *testing.T, args ...string) (string, error) {
	t.Helper()
	restore := setDynamicRegistrar(func(*cobra.Command) {})
	t.Cleanup(restore)
	root := newRootCmdFor(args)
	var out, errOut bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetArgs(args)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	err := root.ExecuteContext(ctx)
	return errOut.String(), err
}

// TestWorkRefHeaderOnControlPlaneVerbs runs each control-plane verb
// through the real root command with the global --work-ref flag and
// checks that the backplane got the Meho-Work-Ref header. The backplane
// audit middleware stamps audit_log.work_ref from that header.
func TestWorkRefHeaderOnControlPlaneVerbs(t *testing.T) {
	cases := []struct {
		name       string
		args       []string
		methodPath string
	}{
		{"targets add", []string{"targets", "add", "vault-a", "--product", "vault", "--host", "vault.example.test"}, "POST /api/v1/targets"},
		{"agent grant create", []string{"agent", "grant", "create", "--principal", "agent:a", "--op", "*", "--verdict", "deny"}, "POST /api/v1/agents/grants"},
		{"connector ingest", []string{"connector", "ingest", "--catalog", "vault/1.15", "--no-wait"}, "POST /api/v1/connectors/ingest"},
		{"connector edit-op", []string{"connector", "edit-op", "vault-1.15", "GET:/v1/sys/health", "--safety", "caution"}, "PATCH /api/v1/connectors/vault-1.15/operations/GET:/v1/sys/health"},
		{"connector enable", []string{"connector", "enable", "vault-1.15", "--confirm"}, "POST /api/v1/connectors/vault-1.15/enable"},
		{"connector disable", []string{"connector", "disable", "vault-1.15", "--confirm"}, "POST /api/v1/connectors/vault-1.15/disable"},
		{"connector enable-reads", []string{"connector", "enable-reads", "vault-1.15", "--confirm"}, "POST /api/v1/connectors/vault-1.15/enable-reads"},
		{"event-source list", []string{"event-source", "list"}, "GET /api/v1/event-sources"},
		{"status --watch", []string{"status", "--watch"}, "GET /api/v1/feed"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, rec := newWorkRefBackplane(t)
			args := append([]string{"--work-ref", testWorkRef}, tc.args...)
			if stderr, err := runRoot(t, args...); err == nil {
				t.Fatalf("want an error from the 500 backplane; stderr=%s", stderr)
			}
			if got := rec.header(t, tc.methodPath); got != testWorkRef {
				t.Errorf("%s header = %q; want %q", api.WorkRefHeader, got, testWorkRef)
			}
		})
	}
}

func TestWorkRefEnvFallbackAndFlagPrecedence(t *testing.T) {
	addArgs := []string{"targets", "add", "vault-a", "--product", "vault", "--host", "vault.example.test"}

	t.Run("env only", func(t *testing.T) {
		_, rec := newWorkRefBackplane(t)
		t.Setenv(api.WorkRefEnv, "gh:evoila/meho#1")
		_, _ = runRoot(t, addArgs...)
		if got := rec.header(t, "POST /api/v1/targets"); got != "gh:evoila/meho#1" {
			t.Errorf("header = %q; want the env value", got)
		}
	})
	t.Run("flag wins over env", func(t *testing.T) {
		_, rec := newWorkRefBackplane(t)
		t.Setenv(api.WorkRefEnv, "gh:evoila/meho#1")
		_, _ = runRoot(t, append([]string{"--work-ref", testWorkRef}, addArgs...)...)
		if got := rec.header(t, "POST /api/v1/targets"); got != testWorkRef {
			t.Errorf("header = %q; want the flag value", got)
		}
	})
	t.Run("neither set sends no header", func(t *testing.T) {
		_, rec := newWorkRefBackplane(t)
		t.Setenv(api.WorkRefEnv, "")
		_, _ = runRoot(t, addArgs...)
		if got := rec.header(t, "POST /api/v1/targets"); got != "" {
			t.Errorf("header = %q; want none", got)
		}
	})
}

func TestWorkRefMalformedRejectedBeforeAnyRequest(t *testing.T) {
	addArgs := []string{"targets", "add", "vault-a", "--product", "vault", "--host", "vault.example.test"}
	cases := []struct {
		name, flag, env, wantInErr string
	}{
		{"bad flag", "JIRA-12", "", "--work-ref must match"},
		{"bad env", "", "not-a-ref", api.WorkRefEnv + " must match"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, rec := newWorkRefBackplane(t)
			t.Setenv(api.WorkRefEnv, tc.env)
			args := addArgs
			if tc.flag != "" {
				args = append([]string{"--work-ref", tc.flag}, addArgs...)
			}
			stderr, err := runRoot(t, args...)
			if err == nil {
				t.Fatal("want an error for a malformed work-ref")
			}
			if !strings.Contains(stderr, tc.wantInErr) {
				t.Errorf("stderr = %q; want it to contain %q", stderr, tc.wantInErr)
			}
			rec.mu.Lock()
			defer rec.mu.Unlock()
			if len(rec.seen) != 0 {
				t.Errorf("no request should leave the CLI; backplane saw %v", rec.seen)
			}
		})
	}
}

// TestWorkRefHeaderOnOnlineDoctor runs an online `meho doctor` through
// the real root command against a fake backplane that answers the
// health route and the MCP handshake. The health request is an
// authenticated REST call the backplane audits, so it must carry the
// Meho-Work-Ref header from the global flag or from the environment.
func TestWorkRefHeaderOnOnlineDoctor(t *testing.T) {
	cases := []struct {
		name string
		flag string
		env  string
		want string
	}{
		{"flag", testWorkRef, "", testWorkRef},
		{"env", "", "gh:evoila/meho#1", "gh:evoila/meho#1"},
		{"neither", "", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("CLAUDE_PLUGIN_ROOT", "")
			t.Setenv(api.WorkRefEnv, tc.env)
			rec := &workRefRecorder{seen: map[string]string{}}
			stub := (&mcpStub{liveStatus: http.StatusOK}).handler(t)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				rec.mu.Lock()
				rec.seen[r.Method+" "+r.URL.Path] = r.Header.Get(api.WorkRefHeader)
				rec.mu.Unlock()
				stub.ServeHTTP(w, r)
			}))
			t.Cleanup(srv.Close)
			xdg := withTempXDG(t)
			seedCreds(t, xdg, srv.URL, auth.StoredToken{
				BackplaneURL: srv.URL,
				AccessToken:  jwtMarker,
				TokenType:    "Bearer",
				Expiry:       time.Now().Add(time.Hour),
			})

			args := []string{"doctor", "--json"}
			if tc.flag != "" {
				args = append([]string{"--work-ref", tc.flag}, args...)
			}
			t.Cleanup(setDynamicRegistrar(func(*cobra.Command) {}))
			root := newRootCmdFor(args)
			var stdout, stderr bytes.Buffer
			root.SetOut(&stdout)
			root.SetErr(&stderr)
			root.SetArgs(args)
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			t.Cleanup(cancel)
			_ = root.ExecuteContext(ctx)

			var report doctorReport
			if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
				t.Fatalf("not JSON: %v\n%s\n%s", err, stdout.String(), stderr.String())
			}
			if c := findCheck(t, &report, "token"); c.Status != doctorOK {
				t.Fatalf("the online layer did not run: %+v", c)
			}
			if got := rec.header(t, "GET /api/v1/health/live"); got != tc.want {
				t.Errorf("health %s header = %q; want %q", api.WorkRefHeader, got, tc.want)
			}
			if got := rec.header(t, "POST /mcp"); got != tc.want {
				t.Errorf("mcp %s header = %q; want %q", api.WorkRefHeader, got, tc.want)
			}
		})
	}
}

// TestWorkRefMalformedEnvSkippedForLocalCommands checks that a bad
// MEHO_WORK_REF does not break commands that send no backplane request.
// A verb that does send one is still refused before the request leaves.
func TestWorkRefMalformedEnvSkippedForLocalCommands(t *testing.T) {
	cases := []struct {
		name string
		args []string
	}{
		{"doctor", []string{"doctor", "--no-network"}},
		{"help", []string{"help", "targets"}},
		{"completion", []string{"completion", "bash"}},
		{"__complete", []string{cobra.ShellCompRequestCmd, "targ"}},
		{"__completeNoDesc", []string{cobra.ShellCompNoDescRequestCmd, "targ"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, rec := newWorkRefBackplane(t)
			t.Setenv(api.WorkRefEnv, "bad")
			if stderr, err := runRoot(t, tc.args...); err != nil {
				t.Fatalf("want success with a bad %s; err=%v stderr=%s", api.WorkRefEnv, err, stderr)
			}
			rec.mu.Lock()
			defer rec.mu.Unlock()
			if len(rec.seen) != 0 {
				t.Errorf("no request should leave the CLI; backplane saw %v", rec.seen)
			}
		})
	}

	// `meho version` reads the public GET /version route (#5036). That
	// route is not audited, so a bad work reference does not stop it,
	// and it is the only request that leaves.
	t.Run("version", func(t *testing.T) {
		_, rec := newWorkRefBackplane(t)
		t.Setenv(api.WorkRefEnv, "bad")
		if stderr, err := runRoot(t, "version"); err != nil {
			t.Fatalf("want success with a bad %s; err=%v stderr=%s", api.WorkRefEnv, err, stderr)
		}
		rec.mu.Lock()
		defer rec.mu.Unlock()
		if len(rec.seen) != 1 || rec.seen["GET /version"] != "" {
			t.Errorf("want only GET /version with no work reference; backplane saw %v", rec.seen)
		}
	})

	refused := []struct {
		name string
		args []string
	}{
		{"targets add still refused", []string{"targets", "add", "vault-a", "--product", "vault", "--host", "vault.example.test"}},
		{"online doctor still refused", []string{"doctor"}},
	}
	for _, tc := range refused {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("CLAUDE_PLUGIN_ROOT", "")
			_, rec := newWorkRefBackplane(t)
			t.Setenv(api.WorkRefEnv, "bad")
			stderr, err := runRoot(t, tc.args...)
			if err == nil {
				t.Fatal("want an error for a malformed work-ref")
			}
			if !strings.Contains(stderr, api.WorkRefEnv+" must match") {
				t.Errorf("stderr = %q; want the shape error", stderr)
			}
			rec.mu.Lock()
			defer rec.mu.Unlock()
			if len(rec.seen) != 0 {
				t.Errorf("no request should leave the CLI; backplane saw %v", rec.seen)
			}
		})
	}
}
