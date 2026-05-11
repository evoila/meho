// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// TestConfigRoundTrip pins the config file save/load shape so a
// future schema rename (e.g. `backplane_url` → `default_backplane`)
// is caught — the field name is part of the wire contract between
// CLI versions.
func TestConfigRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	want := Config{BackplaneURL: "https://meho.evba.lab"}
	if err := SaveConfigAt(path, want); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	got, err := LoadConfigAt(path)
	if err != nil {
		t.Fatalf("LoadConfigAt: %v", err)
	}
	if got != want {
		t.Errorf("roundtrip mismatch: got %+v want %+v", got, want)
	}
}

// TestConfigPermissionsEnforced confirms the saved config sits at
// 0600 and its parent dir at 0700 — same posture as
// credentials.json so a `chmod -R 0700 ~/.config/meho/` covers
// both files identically.
func TestConfigPermissionsEnforced(t *testing.T) {
	dir := t.TempDir()
	nested := filepath.Join(dir, "subdir", "config.json")
	if err := SaveConfigAt(nested, Config{BackplaneURL: "https://x"}); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	info, err := os.Stat(nested)
	if err != nil {
		t.Fatalf("stat config: %v", err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Errorf("expected 0600 file mode, got %o", info.Mode().Perm())
	}
	parent, err := os.Stat(filepath.Dir(nested))
	if err != nil {
		t.Fatalf("stat parent: %v", err)
	}
	if parent.Mode().Perm() != 0o700 {
		t.Errorf("expected 0700 parent dir mode, got %o", parent.Mode().Perm())
	}
}

// TestLoadConfigMissingReturnsSentinel pins the
// "operator hasn't run meho login" signal so callers can branch
// on errors.Is rather than parsing error messages.
func TestLoadConfigMissingReturnsSentinel(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist.json")
	_, err := LoadConfigAt(missing)
	if !errors.Is(err, ErrConfigNotFound) {
		t.Errorf("expected ErrConfigNotFound, got %v", err)
	}
}
