// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/migrate"
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
	for _, want := range []string{"--issuer", "--client-id", "--scope", "--resolve"} {
		if !strings.Contains(out, want) {
			t.Errorf("usage missing flag %s:\n%s", want, out)
		}
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
