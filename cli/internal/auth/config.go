// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Config is the unauthenticated companion to StoredToken: it
// captures the operator's preferred backplane URL so subcommands
// (meho status, meho version, future ops) can recover it without
// asking the operator to re-type it on every invocation. The
// credentials store can't fill this role on its own — its
// (service, user) addressing requires the URL as the lookup key,
// which is exactly what we don't have at status time.
//
// The file is unprivileged on purpose: it carries no secrets, only
// a URL. Mode 0600 anyway because $XDG_CONFIG_HOME/meho/ as a
// whole holds the credentials.json sibling, and mixing modes in
// the same directory invites confusion.
type Config struct {
	// BackplaneURL is the canonical address of the backplane the
	// operator most recently authenticated against. Written by
	// `meho login` at the end of a successful flow.
	BackplaneURL string `json:"backplane_url"`
}

// ErrConfigNotFound is returned by LoadConfig when the operator
// has never run `meho login`. Callers errors.Is against it to
// distinguish "first-time user" from a real I/O failure.
var ErrConfigNotFound = errors.New("meho: no config file")

// ConfigPath returns the canonical config path:
// $XDG_CONFIG_HOME/meho/config.json (or $HOME/.config/meho/...).
// Lives next to credentials.json so the two files share a parent
// directory operators can `chmod -R 0700` in one shot.
func ConfigPath() (string, error) {
	if xdg := os.Getenv("XDG_CONFIG_HOME"); xdg != "" {
		return filepath.Join(xdg, "meho", "config.json"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("meho: locate home dir: %w", err)
	}
	return filepath.Join(home, ".config", "meho", "config.json"), nil
}

// LoadConfig reads the operator's preferred backplane URL from
// disk. Returns ErrConfigNotFound when the file doesn't exist;
// other I/O / parse errors propagate verbatim.
//
// LoadConfigAt is the test-friendly sibling — it takes the path
// directly so unit tests can swap in a tmpdir without touching
// XDG_CONFIG_HOME or HOME.
func LoadConfig() (Config, error) {
	path, err := ConfigPath()
	if err != nil {
		return Config{}, err
	}
	return LoadConfigAt(path)
}

// LoadConfigAt loads the config from a specific path. Hoisted into
// its own function for the same reason auth.NewFileStoreAt is:
// tests need a way to point the loader at a tmpdir without
// stomping over real operator state.
func LoadConfigAt(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return Config{}, ErrConfigNotFound
		}
		return Config{}, fmt.Errorf("meho: read config: %w", err)
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return Config{}, fmt.Errorf("meho: parse config: %w", err)
	}
	return cfg, nil
}

// SaveConfig writes the config atomically (tmpfile + rename) so a
// crashed `meho login` can't truncate a previously good config.
// Mode 0600 because the file shares its parent directory with the
// credentials.json (which is mandatorily 0600); mixing modes
// within $XDG_CONFIG_HOME/meho/ would be a footgun. The parent
// directory is 0700 — same posture credentials.json enforces.
func SaveConfig(cfg Config) error {
	path, err := ConfigPath()
	if err != nil {
		return err
	}
	return SaveConfigAt(path, cfg)
}

// SaveConfigAt is the path-explicit sibling.
func SaveConfigAt(path string, cfg Config) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("meho: create config dir: %w", err)
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("meho: marshal config: %w", err)
	}
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return fmt.Errorf("meho: stale config tmpfile at %s — remove and retry", tmp)
		}
		return fmt.Errorf("meho: open config tmpfile: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("meho: write config: %w", err)
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("meho: close config: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("meho: install config: %w", err)
	}
	return nil
}
