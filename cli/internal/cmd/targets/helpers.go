// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/evoila/meho/cli/internal/auth"
)

// resolveURL resolves the backplane URL from the --backplane flag or
// from the persisted config written by `meho login`. Mirrors the logic
// in cmd.resolveBackplaneURL; duplicated here to avoid the package-
// internal function being accessible from outside the cmd package.
func resolveURL(override string) (string, error) {
	if override != "" {
		return normalizeURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", errors.New("no backplane URL configured; run `meho login <url>` first or pass --backplane <url>")
		}
		return "", err
	}
	return normalizeURL(cfg.BackplaneURL)
}

func normalizeURL(s string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(s), "/")
	if trimmed == "" {
		return "", errors.New("backplane URL is empty")
	}
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid backplane URL %q: %w", s, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
}
