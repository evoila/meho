// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package backplane holds the backplane-URL resolution + error
// classification helpers shared by every cmd/* command package. Before
// it, each package carried a byte-identical resolveBackplane /
// classifyBackplaneError / normaliseURL / errNoBackplaneConfigured —
// duplicated only because sibling cmd/* packages can't import one
// another without an import cycle (cmd/root.go grafts each onto the
// tree). The per-package transport (doAuthedRequest) and error-render
// (renderRequestError) helpers stay put: they have genuine per-connector
// variants (response-size caps, overflow handling) that aren't safe to
// merge mechanically.
package backplane

import (
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// NotConfiguredError wraps auth.ErrConfigNotFound so callers can
// distinguish "operator never logged in" (→ auth_expired exit code 2 —
// the fix is `meho login`) from URL-parse failures (→ unexpected exit
// code 4 — the fix is correcting argv).
type NotConfiguredError struct{ Inner error }

func (e *NotConfiguredError) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}

func (e *NotConfiguredError) Unwrap() error { return e.Inner }

// Resolve returns the backplane URL: the --backplane override when set,
// otherwise the URL recorded by the most recent `meho login`. A missing
// config surfaces as *NotConfiguredError so ClassifyError can route it
// to auth_expired.
func Resolve(override string) (string, error) {
	if override != "" {
		return NormaliseURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", &NotConfiguredError{Inner: err}
		}
		return "", err
	}
	return NormaliseURL(cfg.BackplaneURL)
}

// ClassifyError maps a Resolve error to the right output.StructuredError
// category: missing-config → auth_expired; everything else (parse / fs
// errors) → unexpected.
func ClassifyError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// NormaliseURL strips trailing slashes + parses the URL to fail fast on
// garbage input.
func NormaliseURL(s string) (string, error) {
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
