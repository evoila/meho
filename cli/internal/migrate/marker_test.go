// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"os"
	"testing"
)

func TestTouchMarker_CreatesMarkerFile(t *testing.T) {
	dir := t.TempDir()
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)

	if err := TouchMarker(dir); err != nil {
		t.Fatalf("TouchMarker: %v", err)
	}

	exists, err := MarkerExists(dir)
	if err != nil {
		t.Fatalf("MarkerExists: %v", err)
	}
	if !exists {
		t.Error("marker should exist after TouchMarker")
	}
}

func TestMarkerExists_ReturnsFalseWhenAbsent(t *testing.T) {
	dir := t.TempDir()
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)

	exists, err := MarkerExists(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if exists {
		t.Error("marker should not exist before TouchMarker")
	}
}

func TestTouchMarker_Idempotent(t *testing.T) {
	dir := t.TempDir()
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)

	if err := TouchMarker(dir); err != nil {
		t.Fatalf("first touch: %v", err)
	}
	if err := TouchMarker(dir); err != nil {
		t.Fatalf("second touch: %v", err)
	}
	exists, err := MarkerExists(dir)
	if err != nil || !exists {
		t.Errorf("marker should exist after two touches: exists=%v err=%v", exists, err)
	}
}

func TestMarkerExists_DegracesOnBadXDG(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", "")
	// Without XDG_CONFIG_HOME set, falls back to $HOME/.config.
	// Just verify it doesn't panic or return an error.
	_, err := MarkerExists("/some/nonexistent/dir")
	if err != nil {
		t.Errorf("unexpected error (should degrade): %v", err)
	}
}

func TestDeleteMarkerReEnablesNudge(t *testing.T) {
	dir := t.TempDir()
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)

	if err := TouchMarker(dir); err != nil {
		t.Fatalf("TouchMarker: %v", err)
	}

	// Delete the marker.
	p, err := markerPath(dir)
	if err != nil {
		t.Fatalf("markerPath: %v", err)
	}
	if err := os.Remove(p); err != nil {
		t.Fatalf("remove marker: %v", err)
	}

	exists, err := MarkerExists(dir)
	if err != nil {
		t.Fatalf("MarkerExists: %v", err)
	}
	if exists {
		t.Error("marker should not exist after deletion")
	}
}

func TestSanitizeDirName(t *testing.T) {
	cases := []struct {
		input string
		want  string
	}{
		{"/home/user/.config/meho/memory", "home_user_.config_meho_memory"},
		{"/Users/bob/Desktop", "Users_bob_Desktop"},
		{"relative/path", "relative_path"},
	}
	for _, tc := range cases {
		got := sanitizeDirName(tc.input)
		if got != tc.want {
			t.Errorf("sanitizeDirName(%q) = %q; want %q", tc.input, got, tc.want)
		}
	}
}
