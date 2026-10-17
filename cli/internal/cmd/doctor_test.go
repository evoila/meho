// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/pem"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"github.com/zalando/go-keyring"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// failTransport fails the test if any HTTP request is made.
type failTransport struct{ t *testing.T }

func (f failTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	f.t.Errorf("unexpected network call to %s", r.URL)
	return nil, errors.New("no network allowed")
}

// offlineDeps returns doctor deps that fail the test on any network use.
func offlineDeps(t *testing.T, home string) doctorDeps {
	t.Helper()
	return doctorDeps{
		getenv:     os.Getenv,
		executable: func() (string, error) { return "/opt/meho/bin/meho", nil },
		homeDir:    func() (string, error) { return home, nil },
		keyringGet: func(string, string) (string, error) { return "", keyring.ErrNotFound },
		httpClient: &http.Client{Transport: failTransport{t}},
		lookupHost: func(context.Context, string) ([]string, error) {
			t.Error("unexpected DNS lookup")
			return nil, errors.New("no network allowed")
		},
	}
}

const testLock = `{"lockfileVersion":3,"packages":{"node_modules/mcp-remote":{"version":"0.1.39"}}}`

// fakePlugin lays out an installed plugin and a vendored cache.
func fakePlugin(t *testing.T, root, cache, installed string) {
	t.Helper()
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.MkdirAll(filepath.Join(root, "bin"), 0o755))
	must(os.WriteFile(filepath.Join(root, "bin", "meho-mcp-remote"),
		[]byte("#!/usr/bin/env bash\ndefault_scopes=\"mcp:read mcp:execute offline_access\"\n"), 0o755))
	must(os.WriteFile(filepath.Join(root, "package-lock.json"), []byte(testLock), 0o644))
	must(os.MkdirAll(filepath.Join(cache, "node_modules", "mcp-remote"), 0o755))
	must(os.WriteFile(filepath.Join(cache, "package-lock.json"), []byte(testLock), 0o644))
	must(os.WriteFile(filepath.Join(cache, "node_modules", "mcp-remote", "package.json"),
		[]byte(`{"version":"`+installed+`"}`), 0o644))
}

func writeTestCA(t *testing.T, path string) {
	t.Helper()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()
	block := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	if err := os.WriteFile(path, block, 0o644); err != nil {
		t.Fatal(err)
	}
}

func findCheck(t *testing.T, r *doctorReport, name string) doctorCheck {
	t.Helper()
	for _, c := range r.Checks {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("check %q not in report: %+v", name, r.Checks)
	return doctorCheck{}
}

func TestDoctor_OfflineLayer(t *testing.T) {
	xdg := withTempXDG(t)
	home := t.TempDir()
	root := filepath.Join(home, ".claude", "plugins", "cache", "meho", "meho", "1.0.0")
	cache := filepath.Join(t.TempDir(), "mcp-remote")
	fakePlugin(t, root, cache, "0.1.39")
	ca := filepath.Join(t.TempDir(), "ca.pem")
	writeTestCA(t, ca)
	if err := os.MkdirAll(filepath.Join(xdg, "meho"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(xdg, "meho", "plugin.env"), []byte(
		"# plugin settings\nexport MEHO_MCP_URL=\"https://backplane.example/mcp\"\nMEHO_CA_CERT="+ca+"\nMEHO_PLUGIN_CACHE="+cache+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CLAUDE_PLUGIN_ROOT", "")
	t.Setenv("NODE_EXTRA_CA_CERTS", "")
	t.Setenv("SSL_CERT_FILE", "")
	seedCreds(t, xdg, "https://backplane.example", auth.StoredToken{
		BackplaneURL: "https://backplane.example", AccessToken: jwtMarker, Expiry: time.Now().Add(time.Hour),
	})

	r := &doctorReport{}
	got := runOfflineChecks(r, doctorOptions{}, offlineDeps(t, home))
	if got != "https://backplane.example" {
		t.Fatalf("backplane URL = %q", got)
	}
	want := map[string]string{
		"binary":           "/opt/meho/bin/meho",
		"config":           "backplane https://backplane.example",
		"credential store": "store=file-forced",
		"plugin shim":      filepath.Join(root, "bin", "meho-mcp-remote"),
		"plugin settings":  "scopes: mcp:read mcp:execute offline_access (shim default)",
		"vendored lock":    "mcp-remote 0.1.39 pinned; cached lock matches",
		"cache":            "node_modules/mcp-remote present",
		"CA bundle":        "MEHO_CA_CERT=" + ca + " (1 certificates)",
	}
	for name, sub := range want {
		c := findCheck(t, r, name)
		if c.Status != doctorOK || c.Layer != layerOffline {
			t.Errorf("%s: status=%s layer=%s detail=%q hint=%q", name, c.Status, c.Layer, c.Detail, c.Hint)
		}
		if !strings.Contains(c.Detail, sub) {
			t.Errorf("%s: detail %q does not contain %q", name, c.Detail, sub)
		}
	}
	if !strings.Contains(findCheck(t, r, "plugin settings").Detail, "MCP URL https://backplane.example/mcp (from "+filepath.Join(xdg, "meho", "plugin.env")+")") {
		t.Errorf("plugin settings does not name the origin: %q", findCheck(t, r, "plugin settings").Detail)
	}
}

func TestDoctor_OfflineLayer_VendoredDrift(t *testing.T) {
	withTempXDG(t)
	home := t.TempDir()
	root := filepath.Join(t.TempDir(), "plugin")
	cache := filepath.Join(t.TempDir(), "mcp-remote")
	fakePlugin(t, root, cache, "0.1.30")
	t.Setenv("MEHO_PLUGIN_CACHE", cache)

	r := &doctorReport{}
	runOfflineChecks(r, doctorOptions{pluginRoot: root}, offlineDeps(t, home))
	if c := findCheck(t, r, "vendored lock"); c.Status != doctorFail || !strings.Contains(c.Detail, "0.1.30 is installed") {
		t.Errorf("installed-version drift not reported: %+v", c)
	}

	if err := os.WriteFile(filepath.Join(cache, "package-lock.json"), []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}
	r = &doctorReport{}
	runOfflineChecks(r, doctorOptions{pluginRoot: root}, offlineDeps(t, home))
	if c := findCheck(t, r, "vendored lock"); c.Status != doctorWarn || !strings.Contains(c.Detail, "differs") {
		t.Errorf("lock drift not reported: %+v", c)
	}
}

func TestDoctor_OfflineLayer_BadConfigAndCA(t *testing.T) {
	withTempXDG(t)
	bad := filepath.Join(t.TempDir(), "bad.pem")
	if err := os.WriteFile(bad, []byte("not a cert"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SSL_CERT_FILE", bad)
	r := &doctorReport{}
	got := runOfflineChecks(r, doctorOptions{backplaneOverride: "not a url"}, offlineDeps(t, t.TempDir()))
	if got != "" {
		t.Errorf("bad URL should give no backplane, got %q", got)
	}
	if c := findCheck(t, r, "config"); c.Status != doctorFail {
		t.Errorf("config: %+v", c)
	}
	if c := findCheck(t, r, "CA bundle"); c.Status != doctorFail || !strings.Contains(c.Detail, "no PEM certificate") {
		t.Errorf("CA bundle: %+v", c)
	}
	if c := findCheck(t, r, "plugin shim"); c.Status != doctorSkip {
		t.Errorf("plugin shim should be skipped: %+v", c)
	}
}

func TestDoctor_CredentialStore(t *testing.T) {
	const bp = "https://backplane.example"
	cases := []struct {
		name       string
		disable    string
		keyringErr error
		fileCred   bool
		wantState  string
		wantStatus string
		wantText   string
	}{
		{"keyring selected", "", nil, false, "store=keyring:", doctorOK, "a login is stored"},
		{"forced file fallback", "1", nil, true, "store=file-forced:", doctorOK, "MEHO_KEYRING_DISABLE is set"},
		{"oversize file fallback", "", keyring.ErrNotFound, true, "store=file-fallback:", doctorOK, "too big"},
		{"keyring backend unavailable", "", errors.New("dbus: no session bus"), true, "store=keyring-unavailable:", doctorOK, "no OS keyring backend"},
		{"keyring has no credential", "", keyring.ErrNotFound, false, "store=keyring:", doctorWarn, "no stored credential"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			xdg := withTempXDG(t)
			t.Setenv("MEHO_KEYRING_DISABLE", tc.disable)
			if tc.fileCred {
				seedCreds(t, xdg, bp, auth.StoredToken{BackplaneURL: bp, AccessToken: jwtMarker})
			}
			deps := offlineDeps(t, t.TempDir())
			deps.keyringGet = func(string, string) (string, error) {
				if tc.keyringErr != nil {
					return "", tc.keyringErr
				}
				return "secret-value-never-shown", nil
			}
			c := checkCredentialStore(deps, bp)
			if !strings.HasPrefix(c.Detail, tc.wantState) {
				t.Errorf("detail %q, want state %q", c.Detail, tc.wantState)
			}
			if c.Status != tc.wantStatus || !strings.Contains(c.Detail, tc.wantText) {
				t.Errorf("status=%s detail=%q; want %s containing %q", c.Status, c.Detail, tc.wantStatus, tc.wantText)
			}
			if strings.Contains(c.Detail, "secret-value-never-shown") || strings.Contains(c.Detail, jwtMarker) {
				t.Errorf("credential leaked: %q", c.Detail)
			}
			if errors.Is(tc.keyringErr, keyring.ErrNotFound) && strings.Contains(c.Detail, "unavailable") {
				t.Errorf("ErrNotFound must not read as unavailable: %q", c.Detail)
			}
		})
	}
}

func TestDoctor_RedactsSecrets(t *testing.T) {
	markers := []string{
		"eyJhbGciOiJSUzI1NiJ9.PLANTED.SIG",
		"PLANTED-OAUTH-CODE",
		"PLANTED-COOKIE",
		"PLANTED-AUTH-HEADER",
		"PLANTED-REDIRECT",
		"PLANTED-STATE",
	}
	r := &doctorReport{MCPSessionID: "0b6c1f7e-1111-2222-3333-444455556666"}
	r.add(doctorCheck{Layer: layerAuthed, Name: "token", Status: doctorFail, Class: classUnexpected,
		Detail: "refresh failed: token eyJhbGciOiJSUzI1NiJ9.PLANTED.SIG rejected; callback http://127.0.0.1:3334/cb?code=PLANTED-OAUTH-CODE&state=PLANTED-STATE",
		Hint:   "Set-Cookie: KEYCLOAK_SESSION=PLANTED-COOKIE; Path=/"})
	r.add(doctorCheck{Layer: layerAuthed, Name: "mcp initialize", Status: doctorFail, Class: classUnexpected,
		Detail: "request had Authorization: Bearer PLANTED-AUTH-HEADER and got Location: https://idp.example/auth?redirect_uri=https%3A%2F%2FPLANTED-REDIRECT"})
	r.add(doctorCheck{Layer: layerAuthed, Name: "other", Status: doctorWarn,
		Detail: "redirect_uri=https://PLANTED-REDIRECT/x code=PLANTED-OAUTH-CODE"})

	for _, jsonOut := range []bool{false, true} {
		var buf bytes.Buffer
		_ = renderDoctor(&buf, r, jsonOut)
		out := buf.String()
		for _, m := range markers {
			if strings.Contains(out, m) {
				t.Errorf("json=%t: output leaked %q:\n%s", jsonOut, m, out)
			}
		}
		if !strings.Contains(out, "0b6c1f7e-1111-2222-3333-444455556666") {
			t.Errorf("json=%t: the MCP session id (a safe correlation id) should be shown", jsonOut)
		}
	}
}

// mcpStub is a fake backplane: the liveness route plus a minimal /mcp.
type mcpStub struct {
	liveStatus int
	tools      []string
	listed     []string
	scopes     []string
	gotSession []string
}

func (s *mcpStub) handler(t *testing.T) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health/live", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(s.liveStatus)
		_, _ = w.Write([]byte(`{"operator":{"sub":"alice-sub","name":"Alice","email":null,"tenant_id":"00000000-0000-0000-0000-000000000001","tenant_role":"operator"},"db":{"migrated":true}}`))
	})
	mux.HandleFunc("/mcp", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ID     *int   `json:"id"`
			Method string `json:"method"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		if req.ID == nil {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		if req.Method != "initialize" {
			s.gotSession = append(s.gotSession, r.Header.Get("Mcp-Session-Id"))
		}
		var result any
		switch req.Method {
		case "initialize":
			w.Header().Set("Mcp-Session-Id", "0b6c1f7e-aaaa-bbbb-cccc-ddddeeeeffff")
			result = map[string]any{"protocolVersion": "2025-06-18", "serverInfo": map[string]any{"name": "meho", "version": "test"}}
		case "resources/list":
			result = map[string]any{"resources": []any{map[string]any{"uri": "meho://x"}}}
		case "tools/list":
			var tools []any
			for _, n := range s.tools {
				tools = append(tools, map[string]any{"name": n})
			}
			result = map[string]any{"tools": tools}
		case "tools/call":
			payload, _ := json.Marshal(map[string]any{
				"operator":               map[string]any{"sub": "alice-sub", "name": "Alice", "email": nil, "tenant_id": "00000000-0000-0000-0000-000000000001", "tenant_role": "operator"},
				"vault":                  map[string]any{"reachable": true, "read_ok": true, "detail": nil},
				"db":                     map[string]any{"migrated": true},
				"mcp_session_id_capture": "when_negotiated",
				"granted_scopes":         s.scopes,
				"surface_summary": map[string]any{
					"expected_working_tools":  append(append([]string{}, s.listed...), "search_docs"),
					"listed_working_tools":    s.listed,
					"missing_working_tools":   []string{"search_docs"},
					"operator_surface_listed": false,
				},
			})
			result = map[string]any{"isError": false, "content": []any{map[string]any{"type": "text", "text": string(payload)}}}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": *req.ID, "result": result})
	})
	return mux
}

func onlineDeps(t *testing.T) doctorDeps {
	d := offlineDeps(t, t.TempDir())
	d.httpClient = http.DefaultClient
	d.lookupHost = func(context.Context, string) ([]string, error) { return []string{"127.0.0.1"}, nil }
	return d
}

func runAuthed(t *testing.T, url string, deps doctorDeps) *doctorReport {
	t.Helper()
	r := &doctorReport{}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	runAuthenticatedChecks(ctx, r, url, deps)
	return r
}

func failedClass(r *doctorReport) string {
	for _, c := range r.Checks {
		if c.Status == doctorFail {
			return c.Class
		}
	}
	return ""
}

func TestDoctor_AuthenticatedLayer(t *testing.T) {
	t.Run("happy path", func(t *testing.T) {
		xdg := withTempXDG(t)
		stub := &mcpStub{liveStatus: 200, tools: []string{"meho_status", "call_operation"}, listed: []string{"call_operation", "meho_status"}, scopes: []string{"mcp:read", "openid"}}
		srv := httptest.NewServer(stub.handler(t))
		defer srv.Close()
		seedCreds(t, xdg, srv.URL, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: jwtMarker, Expiry: time.Now().Add(time.Hour)})

		r := runAuthed(t, srv.URL, onlineDeps(t))
		if cls := failedClass(r); cls != "" {
			t.Fatalf("unexpected failure %q: %+v", cls, r.Checks)
		}
		if got := strings.Join(r.GrantedScopes, " "); got != "mcp:read openid" {
			t.Errorf("granted scopes = %q", got)
		}
		if r.MCPSessionID != "0b6c1f7e-aaaa-bbbb-cccc-ddddeeeeffff" {
			t.Errorf("session id = %q", r.MCPSessionID)
		}
		for _, sid := range stub.gotSession {
			if sid != r.MCPSessionID {
				t.Errorf("session id not echoed: %q", sid)
			}
		}
		if r.Surface == nil || len(r.Surface.Gap) != 0 || strings.Join(r.Surface.HiddenByServer, ",") != "search_docs" {
			t.Errorf("surface = %+v", r.Surface)
		}
	})

	t.Run("surface gap", func(t *testing.T) {
		xdg := withTempXDG(t)
		stub := &mcpStub{liveStatus: 200, tools: []string{"meho_status"}, listed: []string{"meho_status", "search_operations"}}
		srv := httptest.NewServer(stub.handler(t))
		defer srv.Close()
		seedCreds(t, xdg, srv.URL, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: jwtMarker, Expiry: time.Now().Add(time.Hour)})

		r := runAuthed(t, srv.URL, onlineDeps(t))
		c := findCheck(t, r, "working surface")
		if c.Status != doctorFail || c.Class != classSurfaceGap || !strings.Contains(c.Detail, "search_operations") {
			t.Errorf("surface gap not reported: %+v", c)
		}
		if doctorExitCode(r) != output.ExitUnexpected {
			t.Errorf("exit code = %d", doctorExitCode(r))
		}
	})

	classes := map[string]string{}

	t.Run("dns failure", func(t *testing.T) {
		withTempXDG(t)
		deps := onlineDeps(t)
		deps.lookupHost = func(context.Context, string) ([]string, error) {
			return nil, &net.DNSError{Err: "no such host", Name: "backplane.invalid", IsNotFound: true}
		}
		r := runAuthed(t, "https://backplane.invalid", deps)
		classes["dns"] = failedClass(r)
		if doctorExitCode(r) != output.ExitUnreachable {
			t.Errorf("exit code = %d", doctorExitCode(r))
		}
	})

	t.Run("tls failure", func(t *testing.T) {
		xdg := withTempXDG(t)
		srv := httptest.NewTLSServer((&mcpStub{liveStatus: 200}).handler(t))
		defer srv.Close()
		seedCreds(t, xdg, srv.URL, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: jwtMarker, Expiry: time.Now().Add(time.Hour)})
		r := runAuthed(t, srv.URL, onlineDeps(t))
		classes["tls"] = failedClass(r)
	})

	t.Run("rejected refresh", func(t *testing.T) {
		xdg := withTempXDG(t)
		mux := http.NewServeMux()
		mux.Handle("/", (&mcpStub{liveStatus: http.StatusUnauthorized}).handler(t))
		mux.HandleFunc("/token", func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":"invalid_grant","error_description":"Token is not active"}`))
		})
		srv := httptest.NewServer(mux)
		defer srv.Close()
		seedCreds(t, xdg, srv.URL, auth.StoredToken{
			BackplaneURL: srv.URL, AccessToken: jwtMarker, RefreshToken: jwtMarker + ".REFRESH",
			Issuer: srv.URL, ClientID: "meho-cli", Expiry: time.Now().Add(-time.Minute),
		})
		deps := onlineDeps(t)
		deps.authedOpts.RefreshDiscoverer = func(context.Context, *http.Client, string) (*auth.DiscoveryDocument, error) {
			return &auth.DiscoveryDocument{TokenEndpoint: srv.URL + "/token"}, nil
		}
		r := runAuthed(t, srv.URL, deps)
		classes["refresh"] = failedClass(r)
		if doctorExitCode(r) != output.ExitAuthExpired {
			t.Errorf("exit code = %d", doctorExitCode(r))
		}
	})

	want := map[string]string{"dns": "dns", "tls": "tls", "refresh": classRefreshRejected}
	for k, v := range want {
		if classes[k] != v {
			t.Errorf("%s classified as %q, want %q", k, classes[k], v)
		}
	}
}

func TestDoctor_CommandNoNetworkJSON(t *testing.T) {
	withTempXDG(t)
	t.Setenv("CLAUDE_PLUGIN_ROOT", "")
	cmd := newDoctorCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"--no-network", "--json", "--backplane", "https://backplane.example"})
	cmd.SetContext(context.Background())
	_ = cmd.Execute()
	var report doctorReport
	if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, stdout.String())
	}
	if c := findCheck(t, &report, "authenticated layer"); c.Status != doctorSkip {
		t.Errorf("authenticated layer should be skipped: %+v", c)
	}
}

func TestDoctor_RegisteredOnRoot(t *testing.T) {
	t.Cleanup(setDynamicRegistrar(func(*cobra.Command) {}))
	root := newRootCmd()
	if c, _, err := root.Find([]string{"doctor"}); err != nil || c.Name() != "doctor" {
		t.Fatalf("doctor not registered: %v", err)
	}
}
