// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"os"
	"path/filepath"
	"strings"
	"time"
)

// markerDir returns the directory that holds migration-complete markers.
// Path: $XDG_CONFIG_HOME/meho/migrated-from/ (or $HOME/.config/meho/migrated-from/).
func markerDir() (string, error) {
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".config")
	}
	return filepath.Join(base, "meho", "migrated-from"), nil
}

// sanitizeDirName converts an absolute path to a safe file-name component
// by replacing path separators and colons with underscores and stripping
// leading separators so the result is always a plain filename.
func sanitizeDirName(dir string) string {
	r := strings.NewReplacer(
		string(filepath.Separator), "_",
		":", "_",
	)
	return strings.TrimLeft(r.Replace(dir), "_")
}

// markerPath returns the full path of the migration-complete marker
// file for the given source directory.
func markerPath(sourceDir string) (string, error) {
	d, err := markerDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(d, sanitizeDirName(sourceDir)), nil
}

// TouchMarker writes the migration-complete marker for sourceDir.
// Subsequent logins will not print the nudge tip while the marker exists.
// Deleting the marker re-enables the nudge.
func TouchMarker(sourceDir string) error {
	p, err := markerPath(sourceDir)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	_, werr := f.WriteString(time.Now().UTC().Format(time.RFC3339) + "\n")
	cerr := f.Close()
	if werr != nil {
		return werr
	}
	return cerr
}

// MarkerExists reports whether the migration-complete marker for sourceDir
// is present. Returns (false, nil) on any I/O error so callers can degrade
// gracefully (e.g. the post-login nudge prints nothing rather than failing login).
func MarkerExists(sourceDir string) (bool, error) {
	p, err := markerPath(sourceDir)
	if err != nil {
		return false, nil //nolint:nilerr // degrade gracefully
	}
	_, err = os.Stat(p)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, nil //nolint:nilerr // degrade gracefully
	}
	return true, nil
}
