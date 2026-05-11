// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// newStatusCmd returns the `meho status` subcommand.
//
// status hits GET /api/v1/health on the backplane the operator last
// authenticated against and renders the response. The bearer token
// is read from the same TokenStore `meho login` writes to (OS
// keyring with file fallback); refresh-on-expiry is best-effort via
// the AuthedClient's lazy 401-retry path.
//
// Output discipline (Goal #11 §5):
//
//   - Default: a human-readable summary on stdout. Format is
//     stable: identity line + Vault + DB.
//   - --json: the typed HealthResponse, pretty-printed to stdout.
//   - Errors: structured codes + non-zero exit codes mapped via
//     output.StructuredError. Errors go to stderr (cobra default);
//     --json on error mode emits a JSON envelope on stderr.
//
// The backplane URL is resolved from `meho login`'s persisted
// config file. Operators can override per-invocation with
// `--backplane <url>` (useful for ad-hoc queries against a second
// environment without re-running login).
func newStatusCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)

	cmd := &cobra.Command{
		Use:   "status",
		Short: "Show operator identity and backplane health",
		Long: "status calls the backplane's authenticated health endpoint " +
			"(/api/v1/health) with the bearer token stored by `meho login` " +
			"and prints a summary of the federation chain (operator identity, " +
			"Vault reachability, DB migration state).\n\n" +
			"Output is human-readable by default. Pass --json for a single " +
			"machine-parseable JSON document on stdout — agents (and the " +
			"meho install.sh smoke test) consume this shape.\n\n" +
			"Exit codes: 0 success, 2 auth_expired (no stored token, or the " +
			"backplane rejected even a refreshed bearer), 3 unreachable, 4 " +
			"unexpected response shape.",
		Args:         cobra.NoArgs,
		SilenceUsage: true,
		// SilenceErrors so we control error output entirely — JSON
		// envelopes go to stderr from output.RenderError, and cobra
		// must not also print the .Error() text on top of them. The
		// silentError sentinel returned by RenderError on the JSON
		// path keeps the exit code propagating to main.
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := resolveBackplaneURL(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(err.Error()), jsonOut)
			}

			client, err := api.NewAuthedClient(cmd.Context(), backplaneURL, api.AuthedClientOptions{})
			if err != nil {
				if api.IsTokenNotFound(err) {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("no stored credentials for %s; run `meho login %s`", backplaneURL, backplaneURL)),
						jsonOut)
				}
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("build authed client: %v", err)),
					jsonOut)
			}

			resp, err := client.GetHealth(cmd.Context())
			if err != nil {
				if api.IsNoRefreshToken(err) {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("stored token rejected and no refresh_token present; run `meho login %s`", backplaneURL)),
						jsonOut)
				}
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, redactedError(err))),
					jsonOut)
			}

			if resp.StatusCode() == http.StatusUnauthorized {
				return output.RenderError(cmd.ErrOrStderr(),
					output.AuthExpired(fmt.Sprintf("backplane rejected stored credentials; run `meho login %s`", backplaneURL)),
					jsonOut)
			}
			if resp.JSON200 == nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("HTTP %d from %s", resp.StatusCode(), backplaneURL)),
					jsonOut)
			}

			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			return output.PrintHealth(cmd.OutOrStdout(), resp.JSON200)
		},
	}

	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a single JSON document on stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// resolveBackplaneURL picks the backplane to talk to. Priority:
//
//  1. The --backplane override. Skips the config file entirely —
//     useful for ad-hoc queries against a different environment
//     without re-running `meho login`.
//  2. The single backplane URL recorded by the most recent `meho
//     login` invocation (config.json next to credentials.json).
//
// The returned URL is normalised: trailing slash stripped, scheme
// and host parsed for sanity (an invalid URL stored on disk
// surfaces here rather than mid-request).
func resolveBackplaneURL(override string) (string, error) {
	if override != "" {
		return normalizeBackplaneURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", errors.New("no backplane URL configured; run `meho login <url>` first or pass --backplane <url>")
		}
		return "", err
	}
	return normalizeBackplaneURL(cfg.BackplaneURL)
}

// normalizeBackplaneURL canonicalises a backplane URL: strips
// trailing slashes, parses the scheme + host to reject garbage at
// the boundary, and reassembles a clean string. Mirrors the
// canonicalisation `meho login` does at storage time so the same
// URL maps to the same store key on every command.
func normalizeBackplaneURL(s string) (string, error) {
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

// redactedError strips potentially sensitive substrings from an
// error before it lands in operator-visible output. v0.1 enforces
// one rule: no string starting with "eyJ" survives — that's the
// base64-URL prefix every JWT carries, and the only credential the
// CLI handles. If a downstream library ever leaks the bearer into
// a wrapped error string (e.g. an http.Request URL with the token
// embedded), this catches it before the operator sees it.
//
// More targeted redaction (refresh_token shape, id_token shape) is
// a v0.2 enhancement; in practice "eyJ" matches every realistic
// leak because the same base64 prefix appears on access/refresh/id
// tokens alike.
func redactedError(err error) string {
	msg := err.Error()
	return redactJWTLike(msg)
}

// redactJWTLike replaces any whitespace-bounded run starting with
// the JWT prefix "eyJ" with the literal "[redacted-token]". Exposed
// at package scope so the unit test can pin the exact substitution
// behaviour against arbitrary inputs.
func redactJWTLike(msg string) string {
	if !strings.Contains(msg, "eyJ") {
		return msg
	}
	fields := strings.Fields(msg)
	for i, f := range fields {
		if strings.Contains(f, "eyJ") {
			// Capture leading/trailing punctuation (quotes, parens,
			// trailing commas) so the redaction reads naturally in
			// a wrapped error string.
			lead, core, trail := splitPunctuation(f)
			if strings.HasPrefix(core, "eyJ") {
				fields[i] = lead + "[redacted-token]" + trail
			}
		}
	}
	return strings.Join(fields, " ")
}

// splitPunctuation peels punctuation runs off both ends of a field
// so the inner "core" can be inspected for the JWT prefix without
// dropping the surrounding context.
func splitPunctuation(s string) (lead, core, trail string) {
	isPunct := func(r byte) bool {
		switch r {
		case '"', '\'', '(', '[', '<', ')', ']', '>', ',', '.', ';', ':':
			return true
		}
		return false
	}
	i := 0
	for i < len(s) && isPunct(s[i]) {
		i++
	}
	j := len(s)
	for j > i && isPunct(s[j-1]) {
		j--
	}
	return s[:i], s[i:j], s[j:]
}
