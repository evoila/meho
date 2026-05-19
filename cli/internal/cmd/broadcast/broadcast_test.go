// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

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
// resolveBackplane / doAuthedRequest will read. Mirrors the helper
// in cli/internal/cmd/audit/audit_test.go.
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
// attached. Mirrors the audit-package helper.
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

// TestNewRootCmdRegistersOverridesParent -- the `broadcast` parent
// mounts the `overrides` subcommand parent.
func TestNewRootCmdRegistersOverridesParent(t *testing.T) {
	root := NewRootCmd()
	found := false
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if name == "overrides" {
			found = true
		}
	}
	if !found {
		t.Errorf("subcommand %q not registered under `meho broadcast`", "overrides")
	}
}

// TestOverridesRegistersListSetRemove -- the `overrides` parent mounts
// the three CRUD verbs.
func TestOverridesRegistersListSetRemove(t *testing.T) {
	root := NewRootCmd()
	var overridesCmd *cobra.Command
	for _, sub := range root.Commands() {
		if strings.SplitN(sub.Use, " ", 2)[0] == "overrides" {
			overridesCmd = sub
			break
		}
	}
	if overridesCmd == nil {
		t.Fatal("`overrides` parent missing")
	}
	want := map[string]bool{"list": false, "set": false, "remove": false}
	for _, sub := range overridesCmd.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho broadcast overrides`", name)
		}
	}
}

// TestNormaliseURLStripsTrailingSlash -- shared resolver shape.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := normaliseURL("https://meho.test:8443/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test:8443" {
		t.Errorf("got %q; want %q", got, "https://meho.test:8443")
	}
}

func TestNormaliseURLRejectsEmpty(t *testing.T) {
	if _, err := normaliseURL(""); err == nil {
		t.Errorf("empty URL should error")
	}
}

func TestNormaliseURLRejectsNoHost(t *testing.T) {
	if _, err := normaliseURL("https:///path"); err == nil {
		t.Errorf("no-host URL should error")
	}
}
