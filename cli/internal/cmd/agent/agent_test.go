// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

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
// backplane.Resolve and the generated typed client (via
// api.NewAuthedClient) read. Mirrors the same helper in
// cli/internal/cmd/kb/kb_test.go.
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

// newTestCmd builds a fresh cobra.Command with stdout/stderr buffers.
func newTestCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
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

// TestNewRootCmdRegistersAllVerbs — every advertised verb has a cobra
// subcommand. The CLI manifest is the contract operators build muscle
// memory around; dropping a verb silently is the regression class this
// catches at unit-time. The five CRUD verbs (T2 #809) plus the three
// invocation verbs (T4 #811) must all register.
func TestNewRootCmdRegistersAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"list":       false,
		"show":       false,
		"create":     false,
		"edit":       false,
		"delete":     false,
		"run":        false,
		"run-status": false,
		"run-list":   false,
		"run-events": false,
	}
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("verb %q not registered on `meho agent`", name)
		}
	}
}

// TestLoadJSONObjectFlagInline — inline JSON object parses.
func TestLoadJSONObjectFlagInline(t *testing.T) {
	cmd, _, _ := newTestCmd(t)
	got, err := loadJSONObjectFlag(cmd, `{"allow": ["call_operation"]}`, "--toolset")
	if err != nil {
		t.Fatalf("loadJSONObjectFlag: %v", err)
	}
	if _, ok := got["allow"]; !ok {
		t.Errorf("parsed object missing key: %+v", got)
	}
}

// TestLoadJSONObjectFlagEmptyIsNil — an empty value yields nil so the
// caller omits the field from the request.
func TestLoadJSONObjectFlagEmptyIsNil(t *testing.T) {
	cmd, _, _ := newTestCmd(t)
	got, err := loadJSONObjectFlag(cmd, "  ", "--toolset")
	if err != nil {
		t.Fatalf("loadJSONObjectFlag: %v", err)
	}
	if got != nil {
		t.Errorf("empty value should yield nil; got %+v", got)
	}
}

// TestLoadJSONObjectFlagRejectsNonObject — a JSON array / scalar is
// rejected (the backend's fields are objects).
func TestLoadJSONObjectFlagRejectsNonObject(t *testing.T) {
	cmd, _, _ := newTestCmd(t)
	if _, err := loadJSONObjectFlag(cmd, `["a", "b"]`, "--toolset"); err == nil {
		t.Errorf("expected error for non-object JSON")
	}
}

// TestLoadJSONObjectFlagStdin — @- reads a JSON object from stdin.
func TestLoadJSONObjectFlagStdin(t *testing.T) {
	cmd, _, _ := newTestCmd(t)
	cmd.SetIn(strings.NewReader(`{"x": 1}`))
	got, err := loadJSONObjectFlag(cmd, "@-", "--output-schema")
	if err != nil {
		t.Fatalf("loadJSONObjectFlag @-: %v", err)
	}
	if got["x"] != float64(1) {
		t.Errorf("stdin parse produced %+v", got)
	}
}
