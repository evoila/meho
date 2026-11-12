// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// profileBackplane serves /api/v1/health and records the bearer of each call.
type profileBackplane struct {
	mu      sync.Mutex
	bearers []string
}

func (p *profileBackplane) last(t *testing.T) string {
	t.Helper()
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.bearers) == 0 {
		t.Fatal("the backplane got no request")
	}
	return p.bearers[len(p.bearers)-1]
}

// setupProfiles starts a backplane and stores three logins on it: the
// default one and the "initiator" and "approver" profiles. The
// backplane refuses the token "tok-stale" as expired.
func setupProfiles(t *testing.T) (*httptest.Server, *profileBackplane) {
	t.Helper()
	rec := &profileBackplane{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/health" {
			rec.mu.Lock()
			rec.bearers = append(rec.bearers, r.Header.Get("Authorization"))
			rec.mu.Unlock()
		}
		if r.Header.Get("Authorization") == "Bearer tok-stale" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(healthBody())
	}))
	t.Cleanup(srv.Close)

	xdg := withTempXDG(t)
	t.Setenv(auth.ProfileEnv, "")
	seedCreds(t, xdg, srv.URL, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: "tok-default", TokenType: "Bearer", Expiry: time.Now().Add(time.Hour)})
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"initiator", "approver"} {
		svc, user := auth.KeyForProfile(srv.URL, name)
		if err := store.Save(svc, user, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: "tok-" + name, TokenType: "Bearer", Expiry: time.Now().Add(time.Hour)}); err != nil {
			t.Fatal(err)
		}
	}
	return srv, rec
}

func runRootOut(t *testing.T, args ...string) (stdout, stderr string, err error) {
	t.Helper()
	restore := setDynamicRegistrar(func(*cobra.Command) {})
	t.Cleanup(restore)
	t.Cleanup(func() { output.SetLoginProfile("") })
	root := newRootCmdFor(args)
	var out, errOut bytes.Buffer
	root.SetOut(&out)
	root.SetErr(&errOut)
	root.SetArgs(args)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	err = root.ExecuteContext(ctx)
	return out.String(), errOut.String(), err
}

func TestProfileSelectsTheStoredLogin(t *testing.T) {
	cases := []struct {
		name       string
		env        string
		args       []string
		wantBearer string
		wantName   string
	}{
		{"no flag, no env is the default login", "", []string{"status"}, "Bearer tok-default", "default"},
		{"flag", "", []string{"--profile", "initiator", "status"}, "Bearer tok-initiator", "initiator"},
		{"env", "approver", []string{"status"}, "Bearer tok-approver", "approver"},
		{"flag beats env", "approver", []string{"--profile", "initiator", "status"}, "Bearer tok-initiator", "initiator"},
		{"alternating needs no new login", "", []string{"--profile", "approver", "status"}, "Bearer tok-approver", "approver"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, rec := setupProfiles(t)
			t.Setenv(auth.ProfileEnv, tc.env)
			stdout, stderr, err := runRootOut(t, tc.args...)
			if err != nil {
				t.Fatalf("status failed: %v; stderr=%s", err, stderr)
			}
			if got := rec.last(t); got != tc.wantBearer {
				t.Errorf("bearer = %q; want %q", got, tc.wantBearer)
			}
			hasLine := strings.HasPrefix(stdout, "Profile: "+tc.wantName+"\n")
			if tc.wantName == "default" && strings.Contains(stdout, "Profile:") {
				t.Errorf("the default profile must print no profile line (old output unchanged); got %q", stdout)
			}
			if tc.wantName != "default" && !hasLine {
				t.Errorf("status output must start with the profile line; got %q", stdout)
			}
			if !strings.Contains(stdout, "alice-sub") {
				t.Errorf("status output must show the sub; got %q", stdout)
			}
		})
	}
}

func TestProfileWithoutLoginDoesNotUseAnotherLogin(t *testing.T) {
	_, rec := setupProfiles(t)
	_, stderr, err := runRootOut(t, "--profile", "never-logged-in", "status")
	if err == nil {
		t.Fatal("want an error: the profile has no login")
	}
	if len(rec.bearers) != 0 {
		t.Errorf("no request may be sent with another profile's token; got %v", rec.bearers)
	}
	if !strings.Contains(stderr, "no stored credentials") {
		t.Errorf("stderr = %q; want the no-credentials message", stderr)
	}
}

func TestInvalidProfileNameIsRefused(t *testing.T) {
	setupProfiles(t)
	_, stderr, err := runRootOut(t, "--profile", "../evil", "status")
	if err == nil || !strings.Contains(stderr, "not a valid profile name") {
		t.Fatalf("err=%v stderr=%q; want a refusal of the name", err, stderr)
	}
}

func TestLoginWithProfileFillsOnlyThatSlot(t *testing.T) {
	withTempXDG(t)
	t.Setenv(auth.ProfileEnv, "")
	var idp *httptest.Server
	idp = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/.well-known/openid-configuration"):
			_, _ = w.Write([]byte(`{"issuer":"` + idp.URL + `","device_authorization_endpoint":"` + idp.URL + `/device","token_endpoint":"` + idp.URL + `/token"}`))
		case r.URL.Path == "/device":
			_, _ = w.Write([]byte(`{"device_code":"dc","user_code":"ABCD-EFGH","verification_uri":"https://kc.example/device","expires_in":60,"interval":1}`))
		case r.URL.Path == "/token":
			_, _ = w.Write([]byte(`{"access_token":"tok-from-login","token_type":"Bearer","expires_in":300}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(idp.Close)

	stdout, stderr, err := runRootOut(t, "--profile", "approver", "login", "--insecure-allow-http",
		"--issuer", idp.URL, "--client-id", "meho-cli", idp.URL)
	if err != nil {
		t.Fatalf("login failed: %v; stderr=%s", err, stderr)
	}
	if !strings.Contains(stdout, "Profile: approver") {
		t.Errorf("login output should name the profile; got %q", stdout)
	}

	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatal(err)
	}
	svc, user := auth.KeyForProfile(idp.URL, "approver")
	if tok, err := store.Load(svc, user); err != nil || tok.AccessToken != "tok-from-login" {
		t.Errorf("approver slot: %q, %v; want the new token", tok.AccessToken, err)
	}
	svc, user = auth.KeyForBackplane(idp.URL)
	if _, err := store.Load(svc, user); err == nil {
		t.Error("the default slot must stay empty after a login under a profile")
	}
}

func TestLoginHintNamesTheProfile(t *testing.T) {
	cases := []struct {
		name     string
		env      string
		args     []string
		wantHint string
	}{
		{"missing login, flag", "", []string{"--profile", "never-logged-in", "status"}, "run `meho --profile never-logged-in login "},
		{"missing login, flag beats env", "approver", []string{"--profile", "never-logged-in", "status"}, "run `meho --profile never-logged-in login "},
		{"missing login, env", "never-logged-in", []string{"status"}, "run `meho --profile never-logged-in login "},
		{"expired login, flag beats env", "approver", []string{"--profile", "stale", "status"}, "run `meho --profile stale login "},
		{"expired login, json", "", []string{"--profile", "stale", "status", "--json"}, "run `meho --profile stale login "},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv, _ := setupProfiles(t)
			store, err := auth.NewFileStore()
			if err != nil {
				t.Fatal(err)
			}
			svc, user := auth.KeyForProfile(srv.URL, "stale")
			if err := store.Save(svc, user, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: "tok-stale", TokenType: "Bearer", Expiry: time.Now().Add(-time.Hour)}); err != nil {
				t.Fatal(err)
			}
			t.Setenv(auth.ProfileEnv, tc.env)
			_, stderr, err := runRootOut(t, tc.args...)
			if err == nil {
				t.Fatal("want an auth error")
			}
			if !strings.Contains(stderr, tc.wantHint) {
				t.Errorf("stderr = %q; want the hint %q", stderr, tc.wantHint)
			}
			if strings.Contains(stderr, "`meho login") {
				t.Errorf("the hint must not point at the default login; stderr = %q", stderr)
			}
		})
	}
}

func TestLoginHintForTheDefaultProfileIsUnchanged(t *testing.T) {
	withTempXDG(t)
	t.Setenv(auth.ProfileEnv, "")
	srv := httptest.NewServer(http.NotFoundHandler())
	t.Cleanup(srv.Close)
	_, stderr, err := runRootOut(t, "status", "--backplane", srv.URL)
	if err == nil {
		t.Fatal("want an auth error")
	}
	if !strings.Contains(stderr, "run `meho login "+srv.URL+"`") {
		t.Errorf("stderr = %q; want the plain login hint", stderr)
	}
}

// The doctor checks the default profile in both layers, also when
// MEHO_PROFILE names a profile with no login.
func TestDoctorChecksTheDefaultProfileUnderMEHOPROFILE(t *testing.T) {
	xdg := withTempXDG(t)
	stub := &mcpStub{liveStatus: 200, tools: []string{"meho_status", "call_operation"}, listed: []string{"call_operation", "meho_status"}, scopes: []string{"mcp:read", "openid"}}
	srv := httptest.NewServer(stub.handler(t))
	t.Cleanup(srv.Close)
	seedCreds(t, xdg, srv.URL, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: jwtMarker, Expiry: time.Now().Add(time.Hour)})

	t.Setenv(auth.ProfileEnv, "approver")
	t.Cleanup(func() { output.SetLoginProfile("") })
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	if err := applyProfile(cmd); err != nil {
		t.Fatal(err)
	}
	if got := auth.ProfileFrom(cmd.Context()); got != "approver" {
		t.Fatalf("profile = %q; want approver", got)
	}

	deps := onlineDeps(t)
	deps.stat = func(string) (fs.FileInfo, error) { return nil, fs.ErrNotExist }
	r := runDoctor(cmd, doctorOptions{backplaneOverride: srv.URL}, deps)
	for _, c := range r.Checks {
		if c.Class == classNotLoggedIn || strings.Contains(c.Detail, "no stored login") {
			t.Errorf("the doctor used the approver profile in a check: %+v", c)
		}
	}
	if cls := failedClass(r); cls != "" {
		t.Errorf("unexpected failure %q: %+v", cls, r.Checks)
	}
}
