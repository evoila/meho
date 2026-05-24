// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"bytes"
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// seedXDGAndToken seeds a per-test config dir + token store that
// backplane.Resolve / doAuthedRequest read. Mirrors the same helper in
// cli/internal/cmd/agent/agent_test.go.
func seedXDGAndToken(t *testing.T, backplaneURL string) string {
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
		Expiry:       time.Now().Add(1 * time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	return dir
}

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers
// wired so per-verb runners can be exercised directly without going
// through the full cobra arg-parsing path.
func newRunCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	return cmd, &stdout, &stderr
}

// TestNewRootCmdRegistersAllSixVerbs — every advertised verb has a
// cobra subcommand. The CLI manifest is the contract operators build
// muscle memory around; dropping a verb silently is the regression
// class this catches at unit time.
func TestNewRootCmdRegistersAllSixVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"list":    false,
		"show":    false,
		"create":  false,
		"edit":    false,
		"delete":  false,
		"history": false,
	}
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("verb %q not registered on `meho conventions`", name)
		}
	}
}

// TestRootCmdHelpMentionsAllVerbs — the parent's help text should
// mention every verb so operators new to the surface find them.
func TestRootCmdHelpMentionsAllVerbs(t *testing.T) {
	root := NewRootCmd()
	var buf bytes.Buffer
	root.SetOut(&buf)
	root.SetErr(&buf)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("`meho conventions --help` failed: %v", err)
	}
	help := buf.String()
	for _, want := range []string{"list", "show", "create", "edit", "delete", "history", "tenant"} {
		if !strings.Contains(help, want) {
			t.Errorf("expected help to mention %q; got:\n%s", want, help)
		}
	}
}

// TestLoadBodyFlagInline — inline text passes through verbatim.
func TestLoadBodyFlagInline(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	got, err := loadBodyFlag(cmd, "rule body text")
	if err != nil {
		t.Fatalf("loadBodyFlag inline: %v", err)
	}
	if got != "rule body text" {
		t.Errorf("inline got %q", got)
	}
}

// TestLoadBodyFlagEmptyRejected — backend has min_length=1 on body;
// fail fast at the CLI.
func TestLoadBodyFlagEmptyRejected(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	if _, err := loadBodyFlag(cmd, ""); err == nil {
		t.Errorf("empty --body should be rejected")
	}
}

// TestLoadBodyFlagStdin — @- reads from stdin with trailing newline
// stripping.
func TestLoadBodyFlagStdin(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(strings.NewReader("one\ntwo\n"))
	got, err := loadBodyFlag(cmd, "@-")
	if err != nil {
		t.Fatalf("loadBodyFlag @-: %v", err)
	}
	if got != "one\ntwo" {
		t.Errorf("@- got %q; want %q", got, "one\ntwo")
	}
}

// TestLoadBodyFlagStdinEmptyRejected — empty stdin can't seed a body.
func TestLoadBodyFlagStdinEmptyRejected(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(strings.NewReader(""))
	if _, err := loadBodyFlag(cmd, "@-"); err == nil {
		t.Errorf("empty stdin should be rejected")
	}
}

// TestLoadBodyFlagFile — @<path> reads a file; stub readBodyFile so we
// don't touch the filesystem.
func TestLoadBodyFlagFile(t *testing.T) {
	orig := readBodyFile
	defer func() { readBodyFile = orig }()
	readBodyFile = func(path string) ([]byte, error) {
		if path != "rule.md" {
			t.Errorf("readBodyFile path: %q", path)
		}
		return []byte("file body\n"), nil
	}
	cmd, _, _ := newRunCmd(t)
	got, err := loadBodyFlag(cmd, "@rule.md")
	if err != nil {
		t.Fatalf("loadBodyFlag @file: %v", err)
	}
	if got != "file body" {
		t.Errorf("@file got %q; want %q", got, "file body")
	}
}

// TestDecodeDetailStringPydanticEnvelope — the FastAPI HTTPException
// shape `{"detail": "..."}` extracts cleanly.
func TestDecodeDetailStringPydanticEnvelope(t *testing.T) {
	got := decodeDetailString(`{"detail":"convention_not_found"}`)
	if got != "convention_not_found" {
		t.Errorf("decodeDetailString string: %q", got)
	}
}

// TestDecodeDetailStringFallsThroughOnListDetail — the pydantic
// validation envelope's `detail` is a list, not a string; fall back to
// the raw body in that case.
func TestDecodeDetailStringFallsThroughOnListDetail(t *testing.T) {
	raw := `{"detail":[{"loc":["body","slug"],"msg":"string too short"}]}`
	got := decodeDetailString(raw)
	if !strings.Contains(got, "string too short") {
		t.Errorf("list-detail fall-through lost content: %q", got)
	}
}

// TestURLPathEscapePreservesUnreserved — slug-shaped strings (the
// V_SLUG pattern) round-trip without escaping.
func TestURLPathEscapePreservesUnreserved(t *testing.T) {
	for _, slug := range []string{"vault-canonical", "rbac.canonical", "secret-handling_v2"} {
		if urlPathEscape(slug) != slug {
			t.Errorf("urlPathEscape mutated unreserved slug %q -> %q", slug, urlPathEscape(slug))
		}
	}
}

// TestURLPathEscapeEscapesPathSeparator — defensive escape of an
// adversarial slug containing `/` so it can't smuggle a path
// boundary.
func TestURLPathEscapeEscapesPathSeparator(t *testing.T) {
	got := urlPathEscape("not/a/slug")
	if strings.Contains(got, "/") {
		t.Errorf("urlPathEscape failed to escape `/`: %q", got)
	}
}

// TestValidKindsCoversAllThree — the closed vocabulary is the
// API-layer single line of defence; the CLI mirrors it so a typo
// surfaces locally.
func TestValidKindsCoversAllThree(t *testing.T) {
	for _, k := range []string{"operational", "workflow", "reference"} {
		if !validKinds[k] {
			t.Errorf("validKinds missing %q", k)
		}
	}
	if validKinds["garbage"] {
		t.Errorf("validKinds should reject garbage")
	}
}
