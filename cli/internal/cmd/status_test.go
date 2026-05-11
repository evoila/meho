// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// jwtMarker is the load-bearing marker the sensitive-data test
// scans for. The "eyJ" prefix is the base64-URL header every JWT
// emits — if the bearer token ever leaks into output, this
// substring is what will appear.
const jwtMarker = "eyJ.TEST-DUMMY-TOKEN-MARKER.SHOULD-NEVER-APPEAR"

// withTempXDG redirects XDG_CONFIG_HOME / MEHO_KEYRING_DISABLE so
// the test exercises the file-backed token store + config in an
// isolated tmpdir. Returns the tmpdir for direct file inspection.
func withTempXDG(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	return dir
}

// seedCreds persists a single stored token + config at the
// supplied XDG dir.
func seedCreds(t *testing.T, xdg, backplaneURL string, stored auth.StoredToken) {
	t.Helper()
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, stored); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(filepath.Join(xdg, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
}

// fakeBackplane stands up an httptest server that serves the
// `/api/v1/health` endpoint and records every received Authorization
// header for inspection. Returns the URL and a slice the caller
// can read after Execute returns.
func fakeBackplane(t *testing.T, body []byte, status int) (string, *[]string) {
	t.Helper()
	var auths []string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health", func(w http.ResponseWriter, r *http.Request) {
		auths = append(auths, r.Header.Get("Authorization"))
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		if len(body) > 0 {
			_, _ = w.Write(body)
		}
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv.URL, &auths
}

// runStatus executes a fresh status command against the supplied
// argv. Returns stdout, stderr, and the cobra-returned error.
//
// Constructs the cobra tree directly (newStatusCmd) rather than
// going through newRootCmd so tests don't have to disable the
// startup-time discovery fetch on every invocation.
func runStatus(t *testing.T, argv ...string) (stdout, stderr *bytes.Buffer, err error) {
	t.Helper()
	cmd := newStatusCmd()
	stdout = &bytes.Buffer{}
	stderr = &bytes.Buffer{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	cmd.SetArgs(argv)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd.SetContext(ctx)
	err = cmd.Execute()
	return
}

// healthBody is the canonical happy-path response body.
func healthBody() []byte {
	migrated := true
	detail := "version=42"
	return mustJSON(map[string]any{
		"operator": map[string]any{"sub": "alice-sub", "email": "alice@example.com", "name": "Alice Example"},
		"vault":    map[string]any{"reachable": true, "read_ok": true, "detail": detail},
		"db":       map[string]any{"migrated": migrated},
	})
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

func TestStatus_HumanHappyPath(t *testing.T) {
	xdg := withTempXDG(t)
	url, auths := fakeBackplane(t, healthBody(), http.StatusOK)
	seedCreds(t, xdg, url, auth.StoredToken{
		BackplaneURL: url,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	})

	stdout, stderr, err := runStatus(t)
	if err != nil {
		t.Fatalf("status returned error: %v\nstderr:\n%s", err, stderr.String())
	}

	out := stdout.String()
	for _, want := range []string{
		"Logged in as alice@example.com (sub: alice-sub)",
		"Vault: reachable, read OK (version=42)",
		"DB:    migrated",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in stdout:\n%s", want, out)
		}
	}
	// Sensitive-data discipline: the bearer token marker MUST NOT
	// appear in any stdout/stderr.
	if strings.Contains(out, jwtMarker) {
		t.Errorf("JWT marker leaked into stdout:\n%s", out)
	}
	if strings.Contains(stderr.String(), jwtMarker) {
		t.Errorf("JWT marker leaked into stderr:\n%s", stderr.String())
	}
	// The backplane saw the Bearer token verbatim — confirm the
	// editor wired it in.
	if len(*auths) == 0 || (*auths)[0] != "Bearer "+jwtMarker {
		t.Errorf("expected backplane to receive Bearer marker; got %v", *auths)
	}
}

func TestStatus_JSONHappyPath(t *testing.T) {
	xdg := withTempXDG(t)
	url, _ := fakeBackplane(t, healthBody(), http.StatusOK)
	seedCreds(t, xdg, url, auth.StoredToken{
		BackplaneURL: url,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	})

	stdout, stderr, err := runStatus(t, "--json")
	if err != nil {
		t.Fatalf("status --json returned error: %v\nstderr:\n%s", err, stderr.String())
	}
	// Output must be valid JSON.
	var decoded map[string]any
	body := strings.TrimSpace(stdout.String())
	if jerr := json.Unmarshal([]byte(body), &decoded); jerr != nil {
		t.Fatalf("--json output is not valid JSON: %v\noutput:\n%s", jerr, stdout.String())
	}
	op, ok := decoded["operator"].(map[string]any)
	if !ok {
		t.Fatalf("operator block missing or wrong type: %v", decoded)
	}
	if op["sub"] != "alice-sub" {
		t.Errorf("operator.sub = %v, want alice-sub", op["sub"])
	}
	// Sensitive-data discipline must still hold for --json.
	if strings.Contains(stdout.String(), jwtMarker) {
		t.Errorf("JWT marker leaked into --json stdout:\n%s", stdout.String())
	}
}

func TestStatus_NoCreds_ExitsAuthExpired(t *testing.T) {
	_ = withTempXDG(t) // no seeded creds

	stdout, stderr, err := runStatus(t)
	if err == nil {
		t.Fatal("expected error for no-creds path")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != output.ExitAuthExpired {
		t.Errorf("expected exit %d, got %d", output.ExitAuthExpired, coder.ExitCode())
	}
	if stdout.Len() != 0 {
		t.Errorf("stdout should be empty on error: %s", stdout.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("stderr should hint at `meho login`, got: %q", stderr.String())
	}
}

func TestStatus_NoCreds_JSON_EmitsEnvelope(t *testing.T) {
	_ = withTempXDG(t)

	stdout, stderr, err := runStatus(t, "--json")
	if err == nil {
		t.Fatal("expected error for no-creds --json path")
	}
	if stdout.Len() != 0 {
		t.Errorf("stdout should be empty on error: %s", stdout.String())
	}
	// The JSON envelope went to stderr; parse it.
	var envelope map[string]any
	if jerr := json.Unmarshal(bytes.TrimSpace(stderr.Bytes()), &envelope); jerr != nil {
		t.Fatalf("stderr is not valid JSON envelope: %v\nstderr:\n%s", jerr, stderr.String())
	}
	if envelope["error"] != "auth_expired" {
		t.Errorf("expected error=auth_expired, got %v", envelope["error"])
	}
	if envelope["exit_code"] != float64(output.ExitAuthExpired) {
		t.Errorf("expected exit_code=%d, got %v", output.ExitAuthExpired, envelope["exit_code"])
	}
}

func TestStatus_BackplaneUnreachable_ExitsUnreachable(t *testing.T) {
	xdg := withTempXDG(t)
	// Point the config at an unrouteable URL.
	dead := "http://192.0.2.1:1"
	seedCreds(t, xdg, dead, auth.StoredToken{
		BackplaneURL: dead,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	})

	_, _, err := runStatus(t)
	if err == nil {
		t.Fatal("expected unreachable error")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != output.ExitUnreachable {
		t.Errorf("expected exit %d, got %d (err=%v)", output.ExitUnreachable, coder.ExitCode(), err)
	}
}

func TestStatus_BackplaneRejects401_ExitsAuthExpired(t *testing.T) {
	xdg := withTempXDG(t)
	url, _ := fakeBackplane(t, []byte(`{"detail":"unauthorized"}`), http.StatusUnauthorized)
	// No refresh token → cannot recover; the 401 path surfaces
	// auth_expired immediately.
	seedCreds(t, xdg, url, auth.StoredToken{
		BackplaneURL: url,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	})
	_, stderr, err := runStatus(t)
	if err == nil {
		t.Fatal("expected auth_expired error on 401 with no refresh_token")
	}
	var coder output.ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != output.ExitAuthExpired {
		t.Errorf("expected exit %d, got %d", output.ExitAuthExpired, coder.ExitCode())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint in stderr, got: %q", stderr.String())
	}
}

func TestStatus_RedactsLeakedBearer(t *testing.T) {
	// Simulate a transport-layer error whose .Error() embeds the
	// bearer string. The CLI's unreachable-path wrapper must scrub
	// the marker before surfacing.
	leaky := errors.New("dial tcp eyJ.LEAKED-MARKER: connection refused")
	redacted := redactedError(leaky)
	if strings.Contains(redacted, "eyJ.LEAKED-MARKER") {
		t.Errorf("redactedError leaked the marker: %q", redacted)
	}
	if !strings.Contains(redacted, "[redacted-token]") {
		t.Errorf("expected [redacted-token] placeholder, got %q", redacted)
	}
}

// TestStatus_BackplaneOverride confirms --backplane bypasses the
// config-file resolution path and that the override URL is what
// gets contacted.
func TestStatus_BackplaneOverride(t *testing.T) {
	xdg := withTempXDG(t)
	// Seed a config pointing at a dead URL...
	deadURL := "http://192.0.2.1:1"
	url, _ := fakeBackplane(t, healthBody(), http.StatusOK)
	seedCreds(t, xdg, deadURL, auth.StoredToken{
		BackplaneURL: deadURL,
		AccessToken:  jwtMarker,
		Expiry:       time.Now().Add(time.Hour),
	})
	// ...also seed a credential for the live URL so the override
	// path has a token to use.
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(url)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: url,
		AccessToken:  jwtMarker + ".OVERRIDE",
		Expiry:       time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}

	stdout, stderr, err := runStatus(t, "--backplane", url)
	if err != nil {
		t.Fatalf("override path returned error: %v\nstderr:\n%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "Logged in as") {
		t.Errorf("expected success output, got:\n%s", stdout.String())
	}
}

// guarantee XDG_CONFIG_HOME isn't leaking out of one test into
// another even when the process aborts mid-test.
func TestMain(m *testing.M) {
	// Belt-and-braces: make sure the process default for
	// XDG_CONFIG_HOME doesn't accidentally pick up some prior
	// test's tmpdir on a flake. t.Setenv resets per-test; this is
	// just defense-in-depth in case a panic prevents cleanup.
	_ = os.Unsetenv("XDG_CONFIG_HOME")
	os.Exit(m.Run())
}
