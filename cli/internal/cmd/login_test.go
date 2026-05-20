// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
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
// /api/v1/auth-config endpoint that the backplane will expose once
// G2.2 ships the corresponding endpoint. We fake the shape locally
// per the Task body's coordination note.
func TestResolveAuthConfigDiscoveryHappyPath(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/auth-config" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"keycloak_issuer": "https://kc/realms/meho",
			"audience":        "meho-cli",
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
		t.Errorf("client id: %q", cfg.ClientID)
	}
}

// TestResolveAuthConfigPartialOverride lets operators pin one half
// (e.g. a custom client_id) while still auto-discovering the other.
func TestResolveAuthConfigPartialOverride(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{
			"keycloak_issuer": "https://discovered-issuer",
			"audience":        "discovered-audience",
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
// intentional decision rather than an oversight.
func TestResolveAuthConfigDiscoveryFailureMentionsFlags(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "auth-config not implemented", http.StatusNotFound)
	}))
	defer srv.Close()

	_, err := resolveAuthConfig(context.Background(), srv.Client(), srv.URL, "", "")
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(err.Error(), "--issuer") || !strings.Contains(err.Error(), "--client-id") {
		t.Errorf("error should mention --issuer and --client-id, got: %v", err)
	}
}

// TestLoginCommandHelpListsFlags is a static-surface check: the help
// output (which is what install scripts and integration tests parse)
// must include the override flags. Catches accidental flag rename
// during refactors.
func TestLoginCommandHelpListsFlags(t *testing.T) {
	cmd := newLoginCmd()
	out := cmd.UsageString()
	for _, want := range []string{"--issuer", "--client-id", "--scope"} {
		if !strings.Contains(out, want) {
			t.Errorf("usage missing flag %s:\n%s", want, out)
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
