// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/migrate"
	"github.com/evoila/meho/cli/internal/version"
)

// TestResolveAuthConfigUsesOverrides skips backplane discovery when
// both override flags are supplied. This is the documented fallback
// while the backplane's /api/v1/auth-config endpoint is still being
// wired up (G2.2 coordination note in the Task body).
func TestResolveAuthConfigUsesOverrides(t *testing.T) {
	// Backplane URL pointing at a non-listening port — if discovery
	// were attempted, the test would either hang or fail with a
	// connection-refused error. Reaching the assertions below proves
	// the override fast-path skipped the HTTP call.
	cfg, err := resolveAuthConfig(context.Background(), http.DefaultClient,
		"http://127.0.0.1:1", "https://kc/realms/meho", "meho-cli")
	if err != nil {
		t.Fatalf("resolveAuthConfig: %v", err)
	}
	if cfg.Issuer != "https://kc/realms/meho" {
		t.Errorf("issuer: %q", cfg.Issuer)
	}
	if cfg.ClientID != "meho-cli" {
		t.Errorf("client id: %q", cfg.ClientID)
	}
}

// TestResolveAuthConfigDiscoveryHappyPath drives the
// /api/v1/auth-config endpoint shape codified by G0.9.1-T9 / Signal #16
// after the 2026-05-21 RDC dogfood: cli_client_id is the public
// device-code client, audience is the confidential resource-server
// identifier. The CLI must map cli_client_id (NOT audience) to
// ClientID; mapping audience would deadlock device-code initiation
// with `401 unauthorized_client` (the v0.3.1 regression this Task
// fixes).
func TestResolveAuthConfigDiscoveryHappyPath(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/auth-config" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"keycloak_issuer": "https://kc/realms/meho",
			"audience":        "meho-backplane",
			"cli_client_id":   "meho-cli",
		})
	}))
	defer srv.Close()

	cfg, err := resolveAuthConfig(context.Background(), srv.Client(), srv.URL, "", "")
	if err != nil {
		t.Fatalf("resolveAuthConfig: %v", err)
	}
	if cfg.Issuer != "https://kc/realms/meho" {
		t.Errorf("issuer: %q", cfg.Issuer)
	}
	if cfg.ClientID != "meho-cli" {
		t.Errorf("client id should be cli_client_id, got: %q", cfg.ClientID)
	}
}

// TestResolveAuthConfigDiscoveryRejectsAbsentCliClientID surfaces the
// actionable public-client error when the backplane returns the v0.3.1
// shape (issuer + audience only, no cli_client_id) or has been deployed
// without `KEYCLOAK_CLI_CLIENT_ID` wired. Silently falling back to
// audience as ClientID is what shipped on v0.3.1 and is the exact
// regression Signal #16 reported — pinning the "no silent fallback"
// behaviour here stops that regression returning.
func TestResolveAuthConfigDiscoveryRejectsAbsentCliClientID(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// v0.3.1-era shape: no cli_client_id field at all.
		_ = json.NewEncoder(w).Encode(map[string]string{
			"keycloak_issuer": "https://kc/realms/meho",
			"audience":        "meho-backplane",
		})
	}))
	defer srv.Close()

	_, err := resolveAuthConfig(context.Background(), srv.Client(), srv.URL, "", "")
	if err == nil {
		t.Fatalf("expected actionable error when cli_client_id is absent")
	}
	for _, want := range []string{"cli_client_id", "public", "--client-id"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error should mention %q so the operator can recover; got: %v", want, err)
		}
	}
}

// TestResolveAuthConfigDiscoveryRejectsEmptyCliClientID covers the
// matched case where the backplane has been upgraded to v0.3.2 but
// the deployer never wired `KEYCLOAK_CLI_CLIENT_ID` — the field
// appears on the response as `""`. Treating it identically to absent
// keeps the operator-facing message stable regardless of whether the
// backplane omits the key or sends an empty value.
func TestResolveAuthConfigDiscoveryRejectsEmptyCliClientID(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"keycloak_issuer": "https://kc/realms/meho",
			"audience":        "meho-backplane",
			"cli_client_id":   "",
		})
	}))
	defer srv.Close()

	_, err := resolveAuthConfig(context.Background(), srv.Client(), srv.URL, "", "")
	if err == nil {
		t.Fatalf("expected actionable error when cli_client_id is empty")
	}
	if !strings.Contains(err.Error(), "cli_client_id") {
		t.Errorf("error should mention cli_client_id; got: %v", err)
	}
}

// TestResolveAuthConfigOverrideBypassesAbsentCliClientID confirms the
// escape hatch: an operator who already knows the public client_id
// can pass `--client-id` and bypass the auth-config endpoint entirely.
// This is the recovery path operators on older backplanes (or those
// who haven't wired KEYCLOAK_CLI_CLIENT_ID yet) take.
func TestResolveAuthConfigOverrideBypassesAbsentCliClientID(t *testing.T) {
	// httptest server is created but never hit — both override flags
	// supplied means discovery is skipped entirely. Pointing the
	// helper at a non-listening port (1) would also work, but a real
	// server makes the failure mode obvious if discovery accidentally
	// fires.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Errorf("discovery should not be called when both overrides are set")
		http.Error(w, "should not be called", http.StatusInternalServerError)
	}))
	defer srv.Close()

	cfg, err := resolveAuthConfig(context.Background(), srv.Client(), srv.URL, "https://kc/realms/meho", "meho-cli")
	if err != nil {
		t.Fatalf("resolveAuthConfig: %v", err)
	}
	if cfg.ClientID != "meho-cli" {
		t.Errorf("client id should be the override: %q", cfg.ClientID)
	}
}

// TestResolveAuthConfigPartialOverride lets operators pin one half
// (e.g. a custom client_id) while still auto-discovering the other.
func TestResolveAuthConfigPartialOverride(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{
			"keycloak_issuer": "https://discovered-issuer",
			"audience":        "discovered-audience",
			"cli_client_id":   "discovered-cli-client",
		})
	}))
	defer srv.Close()

	cfg, err := resolveAuthConfig(context.Background(), srv.Client(), srv.URL, "", "operator-pinned-client")
	if err != nil {
		t.Fatalf("resolveAuthConfig: %v", err)
	}
	if cfg.Issuer != "https://discovered-issuer" {
		t.Errorf("issuer should come from discovery: %q", cfg.Issuer)
	}
	if cfg.ClientID != "operator-pinned-client" {
		t.Errorf("client id should be the override: %q", cfg.ClientID)
	}
}

// TestResolveAuthConfigDiscoveryFailureMentionsFlags surfaces a
// helpful hint when the backplane endpoint isn't reachable and no
// overrides were supplied. The hint is operator-facing prose so
// touching it is a UX change — pinning it here makes that an
// intentional decision rather than an oversight. The root-CA-trust
// breadcrumb covers internal-CA deployments where the operator's
// system trust store doesn't know the deployment's CA (RDC Signal #16,
// 2026-05-21); the flag-override breadcrumb covers everything else
// (404, refused, etc.).
func TestResolveAuthConfigDiscoveryFailureMentionsFlags(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "auth-config not implemented", http.StatusNotFound)
	}))
	defer srv.Close()

	_, err := resolveAuthConfig(context.Background(), srv.Client(), srv.URL, "", "")
	if err == nil {
		t.Fatalf("expected error")
	}
	for _, want := range []string{"--issuer", "--client-id", "root CA"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error should mention %q so the operator has a remediation; got: %v", want, err)
		}
	}
}

// TestLoginCommandHelpListsFlags is a static-surface check: the help
// output (which is what install scripts and integration tests parse)
// must include the override flags. Catches accidental flag rename
// during refactors.
func TestLoginCommandHelpListsFlags(t *testing.T) {
	cmd := newLoginCmd()
	out := cmd.UsageString()
	for _, want := range []string{"--issuer", "--client-id", "--scope", "--resolve", "--print-token"} {
		if !strings.Contains(out, want) {
			t.Errorf("usage missing flag %s:\n%s", want, out)
		}
	}
}

// TestStdoutPrompterShowsCodeLifetime: the waiting line tells the
// operator how long the code is valid and until when (local time).
// Without expires_in from the IdP the line stays as it was.
func TestStdoutPrompterShowsCodeLifetime(t *testing.T) {
	expiry := time.Now().Add(10 * time.Minute)
	var buf bytes.Buffer
	err := stdoutPrompter(&buf)(context.Background(), &oauth2.DeviceAuthResponse{
		UserCode:        "ABCD-EFGH",
		VerificationURI: "https://kc.example/device",
		Expiry:          expiry,
	})
	if err != nil {
		t.Fatalf("prompter: %v", err)
	}
	out := buf.String()
	if !strings.Contains(out, "Waiting for authorisation… The code is valid for ") {
		t.Errorf("waiting line missing the lifetime: %q", out)
	}
	if want := ", until " + expiry.Local().Format("15:04") + ".\n"; !strings.Contains(out, want) {
		t.Errorf("waiting line missing %q: %q", want, out)
	}
	if !strings.Contains(out, "valid for 10m0s") && !strings.Contains(out, "valid for 9m59s") {
		t.Errorf("waiting line should say about 10m: %q", out)
	}

	buf.Reset()
	err = stdoutPrompter(&buf)(context.Background(), &oauth2.DeviceAuthResponse{
		UserCode:        "ABCD-EFGH",
		VerificationURI: "https://kc.example/device",
	})
	if err != nil {
		t.Fatalf("prompter: %v", err)
	}
	if !strings.HasSuffix(buf.String(), "Waiting for authorisation…\n") {
		t.Errorf("without expiry the line must stay plain: %q", buf.String())
	}
}

// TestLoginPrintTokenEmitsExactlyTheToken pins the --print-token stdout
// contract (#2782 AC #1 / #3): on the print path stdout must carry the
// access token and nothing else, so `TOKEN=$(meho login --print-token
// <url>)` captures the bare credential and `... | wc -l` is exactly 1.
// printAccessToken is the sole writer to stdout on that path, so pinning
// its output pins the contract — mirroring status_test.go's redaction
// test, which pins the isolated redaction helper rather than driving the
// whole command.
func TestLoginPrintTokenEmitsExactlyTheToken(t *testing.T) {
	const token = "eyJ.PRINT-TOKEN-TEST.SIGNATURE"
	var stdout bytes.Buffer
	printAccessToken(&stdout, token)

	got := stdout.String()
	// `$(...)` strips trailing newlines — the captured value must be
	// the token verbatim.
	if capd := strings.TrimRight(got, "\n"); capd != token {
		t.Errorf("subshell-captured stdout = %q, want %q", capd, token)
	}
	// `... | wc -l` must be exactly 1.
	if n := strings.Count(got, "\n"); n != 1 {
		t.Errorf("stdout newline count = %d, want 1", n)
	}
	// Nothing but the token + one newline may reach stdout.
	if got != token+"\n" {
		t.Errorf("stdout = %q, want exactly the token followed by one newline", got)
	}
}

// TestLoginHumanWriterDivertsChromeUnderPrintToken pins the other half
// of the stdout contract: every operator-facing line (prompt, success
// message, migration nudge, warnings) is routed through humanWriter,
// which must send it to stderr under --print-token so it never
// contaminates the captured token — and to stdout otherwise, preserving
// the default login UX.
func TestLoginHumanWriterDivertsChromeUnderPrintToken(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if w := humanWriter(&stdout, &stderr, true); w != &stderr {
		t.Error("--print-token must divert chrome to stderr so stdout carries only the token")
	}
	if w := humanWriter(&stdout, &stderr, false); w != &stdout {
		t.Error("default login must keep operator-facing output on stdout")
	}
}

// TestLoginCommandHelpDocumentsResolve pins the discoverability half of
// #2107 (AC #5): the operator-facing help (both --help usage and the
// long text) must name the --resolve escape hatch so a dogfooder hitting
// the split-DNS failure grepping the help output finds the workaround.
func TestLoginCommandHelpDocumentsResolve(t *testing.T) {
	cmd := newLoginCmd()
	if out := cmd.UsageString(); !strings.Contains(out, "--resolve") {
		t.Errorf("usage missing --resolve flag:\n%s", out)
	}
	long := cmd.Long
	for _, want := range []string{"--resolve", "split-DNS", "curl --resolve"} {
		if !strings.Contains(long, want) {
			t.Errorf("login long help should mention %q; got:\n%s", want, long)
		}
	}
}

// TestHintKeycloakResolutionNamesHostAndFlag is the AC #3 guard: when the
// Keycloak host fails to resolve, the surfaced error names the Keycloak
// host (not the backplane) and points at the --resolve knob. The message
// must be distinct from the backplane-side auth-config discovery failure
// (which is worded around --issuer / --client-id).
func TestHintKeycloakResolutionNamesHostAndFlag(t *testing.T) {
	dnsErr := &net.DNSError{Err: "no such host", Name: "kc.example.com", IsNotFound: true}
	err := hintKeycloakResolution(dnsErr, "https://kc.example.com/realms/meho")
	if err == nil {
		t.Fatal("expected a wrapped error for a DNS resolution failure")
	}
	got := err.Error()
	for _, want := range []string{"kc.example.com", "--resolve", "split-DNS"} {
		if !strings.Contains(got, want) {
			t.Errorf("error should mention %q so the operator can recover; got: %v", want, got)
		}
	}
	// Distinct from the backplane discovery failure: it must NOT steer
	// the operator at --issuer / --client-id (that's the backplane path).
	if strings.Contains(got, "--issuer") {
		t.Errorf("Keycloak-resolution hint should not mention --issuer (that's the backplane path); got: %v", got)
	}
	// The underlying DNS error stays unwrappable for callers/tests.
	if !errors.Is(err, dnsErr) {
		t.Errorf("hintKeycloakResolution should wrap the original error; errors.Is failed")
	}
}

// TestHintKeycloakResolutionPassesThroughNonDNSErrors confirms the hint
// is surgical: a non-resolution error (expired device code, timeout,
// access_denied) is returned unchanged so the existing device-flow
// classification still reaches the operator verbatim.
func TestHintKeycloakResolutionPassesThroughNonDNSErrors(t *testing.T) {
	orig := errors.New("meho: device code expired before authorisation; rerun `meho login`")
	if got := hintKeycloakResolution(orig, "https://kc.example.com/realms/meho"); got != orig {
		t.Errorf("non-DNS error should pass through unchanged; got: %v", got)
	}
	if got := hintKeycloakResolution(nil, "https://kc"); got != nil {
		t.Errorf("nil error should pass through as nil; got: %v", got)
	}
}

// TestLoginCommandHelpDocumentsKeyringEscape pins the discoverability
// half of G0.9.1-T14 / Wall #5: the operator-facing help must name
// MEHO_KEYRING_DISABLE so a dogfooder grepping the help output for
// "keyring" finds the escape hatch without having to read the source.
// The auto-fallback covers the first-time-it-happens path; the
// documented env var covers the "force the file backend on every
// subsequent run" path.
func TestLoginCommandHelpDocumentsKeyringEscape(t *testing.T) {
	cmd := newLoginCmd()
	out := cmd.Long
	if !strings.Contains(out, "MEHO_KEYRING_DISABLE") {
		t.Errorf("login help must document MEHO_KEYRING_DISABLE; got:\n%s", out)
	}
	// Auto-fallback breadcrumb so an operator hitting the macOS size
	// error sees in --help that the CLI handled it for them.
	if !strings.Contains(out, "keyring rejects the token by size") {
		t.Errorf("login help should mention the keyring-size auto-fallback; got:\n%s", out)
	}
}

// TestLoginCommandHelpDoesNotClaimEndpointUnshipped pins the breadcrumb
// fix from G0.9.1-T9 / Signal #16. The v0.3.1 help text claimed
// `/api/v1/auth-config` was still on the way ("Until that endpoint
// ships"); the endpoint shipped in v0.3.1 (incompletely), and Signal
// #16 flagged the stale text as misleading. This test fails-loud if
// anyone reintroduces a "not shipped yet" / "until that endpoint
// ships" phrasing.
func TestLoginCommandHelpDoesNotClaimEndpointUnshipped(t *testing.T) {
	cmd := newLoginCmd()
	out := cmd.Long
	for _, forbidden := range []string{
		"Until that endpoint ships",
		"endpoint doesn't exist yet",
		"endpoint isn't shipped",
		"endpoint hasn't shipped",
	} {
		if strings.Contains(out, forbidden) {
			t.Errorf("login help should not claim auth-config endpoint is unshipped; contained %q", forbidden)
		}
	}
}

// TestLoginCommandRejectsMissingArg confirms that omitting the
// backplane URL surfaces the standard cobra arg-count error rather
// than panicking inside the RunE.
func TestLoginCommandRejectsMissingArg(t *testing.T) {
	root := newRootCmd()
	root.SetArgs([]string{"login"})
	var stderr strings.Builder
	root.SetErr(&stderr)
	root.SetOut(&stderr)
	if err := root.Execute(); err == nil {
		t.Fatalf("expected error for missing positional arg")
	}
}

// TestLoginRejectsPlaintextHTTPArg confirms the login arg-parse refuses
// a plaintext http:// backplane URL by default — the bearer token would
// otherwise be sent in the clear (#101 L17). The error fires before any
// network call, so no httptest server is needed.
func TestLoginRejectsPlaintextHTTPArg(t *testing.T) {
	cmd := newLoginCmd()
	cmd.SetArgs([]string{"http://meho.test"})
	var out strings.Builder
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	err := cmd.Execute()
	if err == nil || !strings.Contains(err.Error(), "routed host") {
		t.Fatalf("expected routed-host plaintext rejection, got %v", err)
	}
}

// TestLoginRejectsLoopbackHTTPWithoutFlag confirms login is stricter
// than the resolver: even a loopback http:// URL is rejected unless
// --insecure-allow-http is passed, and the error points at the flag.
func TestLoginRejectsLoopbackHTTPWithoutFlag(t *testing.T) {
	cmd := newLoginCmd()
	cmd.SetArgs([]string{"http://localhost:8080"})
	var out strings.Builder
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	err := cmd.Execute()
	if err == nil || !strings.Contains(err.Error(), "--insecure-allow-http") {
		t.Fatalf("expected opt-in hint for loopback http, got %v", err)
	}
}

// TestLoginInsecureAllowHTTPRejectsRemote confirms that even with
// --insecure-allow-http, a non-loopback http:// host is rejected: the
// flag is a localhost-only convenience, not a blanket cleartext escape.
func TestLoginInsecureAllowHTTPRejectsRemote(t *testing.T) {
	cmd := newLoginCmd()
	cmd.SetArgs([]string{"--insecure-allow-http", "http://meho.test"})
	var out strings.Builder
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	err := cmd.Execute()
	if err == nil || !strings.Contains(err.Error(), "routed host") {
		t.Fatalf("expected routed-host rejection, got %v", err)
	}
}

// TestLoginCommandHelpListsInsecureAllowHTTP pins the flag's
// discoverability — install scripts and operators grep --help for it.
func TestLoginCommandHelpListsInsecureAllowHTTP(t *testing.T) {
	cmd := newLoginCmd()
	if out := cmd.UsageString(); !strings.Contains(out, "--insecure-allow-http") {
		t.Errorf("usage missing --insecure-allow-http flag:\n%s", out)
	}
}

// ── Post-login nudge ──────────────────────────────────────────────────────────

func writeMemoryFixture(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
}

const testMemoryFile = `---
name: foo
description: foo desc
type: user
---
Some user memory.
`

// seedNudgeDir creates a CLAUDE_PROJECT_DIR + /memory subdir, sets
// XDG_CONFIG_HOME, and returns the memory dir path.
func seedNudgeDir(t *testing.T) (projectDir, memDir string) {
	t.Helper()
	projectDir = t.TempDir()
	memDir = filepath.Join(projectDir, "memory")
	if err := os.MkdirAll(memDir, 0o700); err != nil {
		t.Fatalf("mkdir memory: %v", err)
	}
	t.Setenv("CLAUDE_PROJECT_DIR", projectDir)
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)
	return projectDir, memDir
}

func TestPrintMigrationNudge_PrintsWhenFilesExistAndMarkerAbsent(t *testing.T) {
	_, memDir := seedNudgeDir(t)
	writeMemoryFixture(t, memDir, "foo.md", testMemoryFile)

	var buf bytes.Buffer
	printMigrationNudge(&buf)

	out := buf.String()
	if !strings.Contains(out, "meho migrate memory") {
		t.Errorf("expected nudge with 'meho migrate memory'; got: %q", out)
	}
	if !strings.Contains(out, "1") {
		t.Errorf("expected nudge to mention file count; got: %q", out)
	}
}

func TestPrintMigrationNudge_SilentWhenMarkerExists(t *testing.T) {
	_, memDir := seedNudgeDir(t)
	writeMemoryFixture(t, memDir, "foo.md", testMemoryFile)

	if err := migrate.TouchMarker(memDir); err != nil {
		t.Fatalf("TouchMarker: %v", err)
	}

	var buf bytes.Buffer
	printMigrationNudge(&buf)

	if buf.Len() > 0 {
		t.Errorf("expected no nudge when marker exists; got: %q", buf.String())
	}
}

func TestPrintMigrationNudge_SilentWhenDirEmpty(t *testing.T) {
	seedNudgeDir(t) // creates empty memory dir
	// No files written.

	var buf bytes.Buffer
	printMigrationNudge(&buf)

	if buf.Len() > 0 {
		t.Errorf("expected no nudge when dir is empty; got: %q", buf.String())
	}
}

func TestPrintMigrationNudge_SilentOnDirResolutionError(t *testing.T) {
	// With no CLAUDE_PROJECT_DIR and a non-writable env, ResolveSourceDir
	// falls back to $HOME/.claude/... — just ensure it doesn't panic.
	t.Setenv("CLAUDE_PROJECT_DIR", "")
	var buf bytes.Buffer
	printMigrationNudge(&buf)
	// No assertion — just must not panic.
}

// The acceptance criterion for --ca-file: a login against a server with
// a self-signed certificate works, and the next command trusts it too.
// The server plays the backplane and Keycloak, as a fresh install does
// with one `<release>-core-tls` certificate.
func TestLoginWithCAFileTrustsASelfSignedInstall(t *testing.T) {
	originalTransport := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = originalTransport })
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("MEHO_KEYRING_DISABLE", "1")

	var srv *httptest.Server
	writeJSON := func(w http.ResponseWriter, value any) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(value)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/auth-config", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]string{
			"keycloak_issuer": srv.URL + "/realms/meho", "audience": "meho", "cli_client_id": "meho-cli",
		})
	})
	mux.HandleFunc("/realms/meho/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]string{
			"issuer":                        srv.URL + "/realms/meho",
			"device_authorization_endpoint": srv.URL + "/device",
			"token_endpoint":                srv.URL + "/token",
		})
	})
	mux.HandleFunc("/device", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]any{
			"device_code": "dev", "user_code": "ABCD", "verification_uri": srv.URL + "/verify",
			"expires_in": 60, "interval": 1,
		})
	})
	mux.HandleFunc("/token", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]any{"access_token": "tok", "token_type": "Bearer", "expires_in": 300})
	})
	mux.HandleFunc("/version", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]string{"version": version.Version})
	})
	srv = httptest.NewTLSServer(mux)
	defer srv.Close()

	login := func(args ...string) (string, error) {
		cmd := newLoginCmd()
		var stdout, stderr bytes.Buffer
		cmd.SetOut(&stdout)
		cmd.SetErr(&stderr)
		cmd.SetArgs(append([]string{srv.URL}, args...))
		cmd.SetContext(context.Background())
		err := cmd.Execute()
		return stdout.String() + stderr.String(), err
	}

	if _, err := login(); err == nil {
		t.Fatal("a self-signed install must fail without --ca-file")
	} else if !strings.Contains(err.Error(), "--ca-file") {
		t.Errorf("the failure must name --ca-file: %v", err)
	}

	caFile := filepath.Join(t.TempDir(), "ca.crt")
	block := &pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}
	if err := os.WriteFile(caFile, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	if out, err := login("--ca-file", caFile); err != nil {
		t.Fatalf("login --ca-file: %v\n%s", err, out)
	}

	cfg, err := auth.LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.CAFile != caFile {
		t.Errorf("config ca_file = %q", cfg.CAFile)
	}

	// A new process starts from the plain transport; trustSavedCA must
	// bring the trust back from the config.
	http.DefaultTransport = originalTransport
	var warning bytes.Buffer
	later := &cobra.Command{Use: "status"}
	later.SetErr(&warning)
	trustSavedCA(later, nil)
	if warning.Len() != 0 {
		t.Errorf("warning: %s", warning.String())
	}
	resp, err := http.Get(srv.URL + "/version") //nolint:noctx
	if err != nil {
		t.Fatalf("a later command does not trust the CA: %v", err)
	}
	_ = resp.Body.Close()

	// A login without the flag keeps the file for the same backplane.
	if got := configAfterLogin(cfg.BackplaneURL, ""); got.CAFile != caFile {
		t.Errorf("re-login dropped ca_file: %q", got.CAFile)
	}
	if got := configAfterLogin("https://other.example.test", ""); got.CAFile != "" {
		t.Errorf("another backplane kept ca_file: %q", got.CAFile)
	}
}

func TestLoginRejectsACAFileWithNoCertificate(t *testing.T) {
	originalTransport := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = originalTransport })
	bad := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(bad, []byte("nope"), 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := newLoginCmd()
	cmd.SetOut(io.Discard)
	cmd.SetErr(io.Discard)
	cmd.SetArgs([]string{"https://meho.example.test", "--ca-file", bad})
	err := cmd.Execute()
	if err == nil || !strings.Contains(err.Error(), "--ca-file") {
		t.Fatalf("err = %v", err)
	}
}

func TestSavedCAFileThatIsGoneOnlyWarns(t *testing.T) {
	originalTransport := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = originalTransport })
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	if err := auth.SaveConfig(auth.Config{
		BackplaneURL: "https://meho.example.test",
		CAFile:       filepath.Join(t.TempDir(), "gone.crt"),
	}); err != nil {
		t.Fatal(err)
	}
	var warning bytes.Buffer
	cmd := &cobra.Command{Use: "status"}
	cmd.SetErr(&warning)
	trustSavedCA(cmd, nil)
	if !strings.Contains(warning.String(), "CA file") {
		t.Errorf("warning = %q", warning.String())
	}
}

// Two backplanes: httptest gives every TLS server the same certificate,
// so the CA saved for backplane A would vouch for backplane B. Only a
// command against A may use it.
func TestSavedCAStaysWithItsBackplane(t *testing.T) {
	originalTransport := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = originalTransport })
	xdg := withTempXDG(t)
	t.Setenv(api.TenantEnv, "")

	var hitsA, hitsB atomic.Int32
	handler := func(hits *atomic.Int32) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			hits.Add(1)
			w.Header().Set("Content-Type", "application/yaml")
			_, _ = io.WriteString(w, "spec:\n  tenants: []\n")
		})
	}
	a := httptest.NewTLSServer(handler(&hitsA))
	t.Cleanup(a.Close)
	b := httptest.NewTLSServer(handler(&hitsB))
	t.Cleanup(b.Close)
	a.Config.ErrorLog = log.New(io.Discard, "", 0)
	b.Config.ErrorLog = log.New(io.Discard, "", 0)

	caPath := filepath.Join(t.TempDir(), "ca.crt")
	block := &pem.Block{Type: "CERTIFICATE", Bytes: a.Certificate().Raw}
	if err := os.WriteFile(caPath, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	stored := func(url string) auth.StoredToken {
		return auth.StoredToken{
			BackplaneURL: url, AccessToken: "eyJ.test.token", TokenType: "Bearer",
			Expiry: time.Now().Add(time.Hour),
		}
	}
	seedCreds(t, xdg, b.URL, stored(b.URL))
	seedCreds(t, xdg, a.URL, stored(a.URL))
	if err := auth.SaveConfig(auth.Config{BackplaneURL: a.URL, CAFile: caPath}); err != nil {
		t.Fatal(err)
	}

	for name, args := range map[string][]string{
		"--backplane": {"settings", "export", "--backplane", b.URL},
		"login":       {"login", b.URL},
	} {
		http.DefaultTransport = originalTransport
		_, _, err := runMeho(t, args...)
		if err == nil {
			t.Errorf("%s against backplane B must fail on the certificate", name)
		}
		if got := hitsB.Load(); got != 0 {
			t.Fatalf("%s: backplane B got %d requests through A's CA", name, got)
		}
	}

	http.DefaultTransport = originalTransport
	if _, stderr, err := runMeho(t, "settings", "export"); err != nil {
		t.Fatalf("the saved backplane must stay trusted: %v (%s)", err, stderr)
	}
	http.DefaultTransport = originalTransport
	if _, stderr, err := runMeho(t, "settings", "export", "--backplane", a.URL+"/"); err != nil {
		t.Fatalf("--backplane naming the saved backplane must stay trusted: %v (%s)", err, stderr)
	}
	if hitsA.Load() != 2 {
		t.Errorf("backplane A got %d requests, want 2", hitsA.Load())
	}
}

func TestDiscoveryClientUsesTheSavedCAWithoutTheProcess(t *testing.T) {
	originalTransport := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = originalTransport })
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	t.Cleanup(srv.Close)
	srv.Config.ErrorLog = log.New(io.Discard, "", 0)
	caPath := filepath.Join(t.TempDir(), "ca.crt")
	block := &pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}
	if err := os.WriteFile(caPath, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	resp, err := discoveryClient(auth.Config{BackplaneURL: srv.URL, CAFile: caPath}).Get(srv.URL) //nolint:noctx
	if err != nil {
		t.Fatalf("discovery client: %v", err)
	}
	_ = resp.Body.Close()
	if http.DefaultTransport != originalTransport {
		t.Error("the discovery client must not change the process-wide transport")
	}
}

// `admin keycloak bootstrap-clients` sends the Keycloak admin password
// to --keycloak-base-url, which no saved backplane vouches for. It uses
// --keycloak-ca-bundle for its own CA and must not get the saved one.
func TestSavedCAIsNotTrustedByTheKeycloakBootstrap(t *testing.T) {
	originalTransport := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = originalTransport })
	withTempXDG(t)
	caPath := filepath.Join(t.TempDir(), "ca.crt")
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	t.Cleanup(srv.Close)
	srv.Config.ErrorLog = log.New(io.Discard, "", 0)
	block := &pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}
	if err := os.WriteFile(caPath, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := auth.SaveConfig(auth.Config{BackplaneURL: srv.URL, CAFile: caPath}); err != nil {
		t.Fatal(err)
	}

	restore := setDynamicRegistrar(func(*cobra.Command) {})
	t.Cleanup(restore)
	args := []string{"admin", "keycloak", "bootstrap-clients"}
	bootstrap, _, err := newRootCmdFor(args).Find(args)
	if err != nil {
		t.Fatal(err)
	}
	if bootstrap.Name() != "bootstrap-clients" {
		t.Fatalf("found %q", bootstrap.Name())
	}

	trustSavedCA(bootstrap, nil)

	if http.DefaultTransport != originalTransport {
		t.Fatal("the Keycloak bootstrap got the saved CA")
	}
	if _, err := http.Get(srv.URL); err == nil { //nolint:noctx,bodyclose
		t.Fatal("the Keycloak server must not be trusted through the saved CA")
	}
}
