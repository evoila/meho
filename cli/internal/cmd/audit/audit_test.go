// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

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
// resolveBackplane / doAuthedRequest will read. Mirrors the same
// helper in cli/internal/cmd/targets/list_test.go — the auth
// package's keyring backend defaults are disabled for tests so the
// file-store path is exercised deterministically.
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
// attached. The runXxx helpers consume cmd.OutOrStdout /
// cmd.ErrOrStderr; tests inspect the buffers afterwards.
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

// TestNewRootCmdRegistersAllFiveVerbs — AC1: every advertised verb
// has a cobra subcommand. The CLI manifest is the contract operators
// build muscle memory around; dropping a verb silently is the
// regression class we want to catch at unit-time.
func TestNewRootCmdRegistersAllFiveVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"query":       false,
		"recent":      false,
		"show":        false,
		"who-touched": false,
		"my-recent":   false,
	}
	for _, sub := range root.Commands() {
		// cobra splits `Use` on the first space to render <args>
		// (so `show <audit-id>` registers as `show`). Mirror that
		// split for the contains-check.
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho audit`", name)
		}
	}
}

// TestNormaliseURLStripsTrailingSlash — the resolver mirrors the
// targets / operation helpers; the trailing slash invariant is
// load-bearing for the request paths assembled on top.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := normaliseURL("https://meho.example/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.example" {
		t.Errorf("trailing slash not stripped: got %q", got)
	}
}

// TestNormaliseURLRejectsHostlessInput — bare paths fail fast rather
// than producing a request against the local filesystem.
func TestNormaliseURLRejectsHostlessInput(t *testing.T) {
	if _, err := normaliseURL("/just/a/path"); err == nil {
		t.Errorf("expected error for hostless URL")
	}
}

// TestNormaliseURLRejectsGarbage — a fundamentally unparseable URL
// surfaces the parse error rather than the silently-empty resolver.
func TestNormaliseURLRejectsGarbage(t *testing.T) {
	if _, err := normaliseURL("h ttp://broken"); err == nil {
		t.Errorf("expected error for malformed URL")
	}
}

// TestNormaliseURLRejectsEmpty — empty config slot must reach the
// caller as an error rather than producing a zero-host request.
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	if _, err := normaliseURL("   "); err == nil {
		t.Errorf("expected error for empty URL")
	}
}

// TestDecodeDetailStringPullsString — FastAPI's HTTPException body
// surfaces as `{"detail": "<string>"}`. The audit-API 400 path
// (DurationParseError / InvalidCursorError / UnsupportedFilterError)
// uses that shape, so the CLI's error renderer leans on this helper.
func TestDecodeDetailStringPullsString(t *testing.T) {
	body := `{"detail": "cursor is not valid base64"}`
	if got := decodeDetailString(body); got != "cursor is not valid base64" {
		t.Errorf("decodeDetailString: got %q", got)
	}
}

// TestDecodeDetailStringFallsBackOnNonJSON — operators see the raw
// body when the response isn't FastAPI-shaped (a load balancer 503,
// a stray HTML page) rather than an empty error.
func TestDecodeDetailStringFallsBackOnNonJSON(t *testing.T) {
	body := "Service Unavailable"
	if got := decodeDetailString(body); got != "Service Unavailable" {
		t.Errorf("decodeDetailString fallback: got %q", got)
	}
}

// TestTruncateRuneAware — multi-byte UTF-8 stays valid when the
// table renderer truncates a long target name.
func TestTruncateRuneAware(t *testing.T) {
	got := truncate("vörtex-vcenter-prod-eu-central-1", 10)
	if len(got) == 0 || !strings.Contains(got, "…") {
		t.Errorf("truncate did not emit ellipsis: %q", got)
	}
}

// TestStrDerefHandlesNil — defensive nil-deref so the table /
// summary helpers don't panic on backend-null fields.
func TestStrDerefHandlesNil(t *testing.T) {
	if got := strDeref(nil); got != "" {
		t.Errorf("strDeref(nil): got %q", got)
	}
	v := "hello"
	if got := strDeref(&v); got != "hello" {
		t.Errorf("strDeref(&\"hello\"): got %q", got)
	}
}
