// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
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

// buildClient resolves the backplane URL and builds an AuthedClient, rendering
// a structured error for the well-known failure modes (no config, no creds,
// generic build failure). The returned URL is always valid when error is nil.
func buildClient(cmd *cobra.Command, override string, jsonOut bool) (*api.AuthedClient, string, error) {
	backplaneURL, err := resolveURL(override)
	if err != nil {
		return nil, "", output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(err.Error()), jsonOut)
	}
	client, err := api.NewAuthedClient(cmd.Context(), backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		if api.IsTokenNotFound(err) {
			return nil, backplaneURL, output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf("no stored credentials for %s; run `meho login %s`", backplaneURL, backplaneURL)),
				jsonOut)
		}
		return nil, backplaneURL, output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("build client: %v", err)),
			jsonOut)
	}
	return client, backplaneURL, nil
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
