// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
	"github.com/evoila/meho/cli/internal/version"
)

// newStatusCmd returns the `meho status` subcommand.
//
// Two modes:
//
//   - Default — single-shot health check against GET /api/v1/health.
//     Renders operator identity + Vault + DB. Exit codes 0 / 2 / 3 / 4.
//
//   - --watch — subscribes to GET /api/v1/feed (Server-Sent Events).
//     Streams one rendered line per event until the operator hits
//     Ctrl-C. Disconnects retry with exponential backoff using the
//     SSE Last-Event-Id header for replay. Filters (--op-class,
//     --principal, --target) forward to the SSE query string. Exit
//     codes add 5 for insufficient_role (read_only operators).
//
// Both modes share the bearer token resolution (TokenStore that
// `meho login` writes to, OS keyring with file fallback) and the
// --backplane / --json output discipline (Goal #11 §5).
func newStatusCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
		watch             bool
		opClass           string
		principal         string
		target            string
	)

	cmd := &cobra.Command{
		Use:   "status",
		Short: "Show operator identity + backplane health; --watch streams live activity",
		Long: "status calls the backplane's authenticated health endpoint " +
			"(/api/v1/health) with the bearer token stored by `meho login` " +
			"and prints a summary of the federation chain (operator identity, " +
			"Vault reachability, DB migration state with the database revision " +
			"and the revision this version expects), the readiness checks the " +
			"backplane's /ready endpoint reports, the approval settings in " +
			"force (who must approve, approval key state) and the gated " +
			"features that still need a setting. After an upgrade this is " +
			"how to confirm the database reached the new revision.\n\n" +
			"With the read_only role in the current tenant, the deep check " +
			"is not allowed. status then shows the identity, the tenants and " +
			"the database state from /api/v1/health/live, and says that the " +
			"other checks need the operator role.\n\n" +
			"With --watch the command subscribes to /api/v1/feed and streams " +
			"one rendered line per broadcast event until the operator hits " +
			"Ctrl-C. Filters (--op-class, --principal, --target) forward to " +
			"the SSE query string; disconnects retry with exponential " +
			"backoff using Last-Event-Id for replay.\n\n" +
			"Output is human-readable by default. Pass --json for a single " +
			"machine-parseable JSON document on stdout (one JSON object per " +
			"line in --watch mode) — agents (and the meho install.sh smoke " +
			"test) consume this shape.\n\n" +
			"status also compares the CLI version with the backplane's " +
			"/version endpoint. When they are too far apart (supported: same " +
			"minor version, or the CLI one minor version behind) it prints a " +
			"one-line warning on stderr with the release to install.\n\n" +
			"Exit codes: 0 success, 2 auth_expired, 3 unreachable, 4 " +
			"unexpected response shape, 5 insufficient_role (--watch only).",
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
			if watch {
				return runWatch(cmd.Context(), watchOptions{
					BackplaneURL: backplaneURL,
					OpClass:      opClass,
					Principal:    principal,
					Target:       target,
					JSONOut:      jsonOut,
					Stdout:       cmd.OutOrStdout(),
					Stderr:       cmd.ErrOrStderr(),
				})
			}
			return runOneShot(cmd, backplaneURL, jsonOut)
		},
	}

	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a single JSON document on stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.Flags().BoolVarP(&watch, "watch", "w", false,
		"stream a live SSE feed of broadcast events (one line per event; Ctrl-C to exit)")
	cmd.Flags().StringVar(&opClass, "op-class", "",
		"filter --watch events by op_class (read, write, credential_read, audit_query)")
	cmd.Flags().StringVar(&principal, "principal", "",
		"filter --watch events by principal_sub (JWT subject claim)")
	cmd.Flags().StringVar(&target, "target", "",
		"filter --watch events by target_name (the connector instance name)")
	return cmd
}

// runOneShot performs the original single-request health check —
// extracted from the RunE closure when --watch was added so each
// dispatch arm stays a single function. Behaviour is unchanged from
// the pre-T5 status command.
func runOneShot(cmd *cobra.Command, backplaneURL string, jsonOut bool) error {
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
	if msg, ok := noTenantMessage(resp); ok {
		return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(msg), jsonOut)
	}
	if err != nil {
		if api.IsRefreshRejected(err) {
			// A refresh_token was present but the IdP rejected it as
			// invalid_grant — the session genuinely expired. Say so
			// (exit auth_expired) and point at a new login (#3320).
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf("session expired (refresh token rejected); run `meho login %s`", backplaneURL)),
				jsonOut)
		}
		if api.IsNoRefreshToken(err) {
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf("stored token rejected and no refresh_token present; run `meho login %s`; if you believe you are logged in, try MEHO_KEYRING_DISABLE=1 (keyring/file mismatch — see #3320)", backplaneURL)),
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
	if msg, ok := tenantRefusedMessage(resp); ok {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(msg), jsonOut)
	}
	if resp.StatusCode() == http.StatusForbidden {
		return runLiveness(cmd, client, backplaneURL, jsonOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d from %s", resp.StatusCode(), backplaneURL)),
			jsonOut)
	}

	printVersionSkew(cmd.Context(), client.HTTPClient(), backplaneURL, cmd.ErrOrStderr())

	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	return output.PrintHealth(cmd.OutOrStdout(), resp.JSON200)
}

// runLiveness is the status answer for a login with the read_only role
// in the current tenant (#5241). The deep check stays behind the
// operator role on the backplane (it checks the credential store under
// the person's identity), so the CLI reads the liveness route instead:
// identity, tenants with the current one marked, and the database
// state. A person who switched to a tenant where they only read still
// sees where they are.
func runLiveness(cmd *cobra.Command, client *api.AuthedClient, backplaneURL string, jsonOut bool) error {
	live, err := client.GetLiveness(cmd.Context())
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, redactedError(err))),
			jsonOut)
	}
	if live.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d from %s", live.StatusCode(), backplaneURL)),
			jsonOut)
	}
	printVersionSkew(cmd.Context(), client.HTTPClient(), backplaneURL, cmd.ErrOrStderr())
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), live.JSON200)
	}
	return output.PrintLiveness(cmd.OutOrStdout(), live.JSON200)
}

// tenantBootstrapGuideURL is the guide that explains how a person gets
// a tenant: through exactly one MEHO role group.
const tenantBootstrapGuideURL = "https://evoila.github.io/meho/latest/guides/tenant-bootstrap/"

// noTenantMessage returns the message for a 401 whose detail starts with
// missing_tenant_claim (#3872). The login worked, but the token carries
// no tenant, so "run meho login" would not help. Older backplanes send
// the bare code, so the CLI writes its own text instead of passing the
// server detail through.
func noTenantMessage(resp *api.AuthenticatedHealthApiV1HealthGetResponse) (string, bool) {
	if resp == nil || resp.StatusCode() != http.StatusUnauthorized {
		return "", false
	}
	var body struct {
		Detail string `json:"detail"`
	}
	if err := json.Unmarshal(resp.Body, &body); err != nil {
		return "", false
	}
	if body.Detail != "missing_tenant_claim" && !strings.HasPrefix(body.Detail, "missing_tenant_claim:") {
		return "", false
	}
	return "missing_tenant_claim: your login has no tenant. Usually you are in no " +
		"MEHO role group. Logging in again does not help: ask your admin to put " +
		"you in exactly one role group, then run `meho login` again. Guide: " +
		tenantBootstrapGuideURL, true
}

// tenantRefusedMessage returns the message for a 403 whose detail starts
// with tenant_not_member or tenant_not_found (#5239): the tenant from
// `meho tenants use` or MEHO_TENANT is not one the login may work in,
// for example after the person left it. The backplane's detail lists
// the person's tenants; the CLI adds how to pick another one.
func tenantRefusedMessage(resp *api.AuthenticatedHealthApiV1HealthGetResponse) (string, bool) {
	if resp == nil || resp.StatusCode() != http.StatusForbidden {
		return "", false
	}
	var body struct {
		Detail string `json:"detail"`
	}
	if err := json.Unmarshal(resp.Body, &body); err != nil {
		return "", false
	}
	if !strings.HasPrefix(body.Detail, "tenant_not_member:") && !strings.HasPrefix(body.Detail, "tenant_not_found:") {
		return "", false
	}
	return body.Detail + " Run `meho tenants use` to list your tenants, then " +
		"`meho tenants use <name>` (or change " + api.TenantEnv + ").", true
}

// versionCheckTimeout caps the GET /version call so a slow backplane
// never holds up `meho login` or `meho status`.
const versionCheckTimeout = 3 * time.Second

// printVersionSkew compares this CLI's version with the backplane's
// GET /version and writes a one-line warning to w when they are too
// far apart. Best effort: any error is ignored, so the check never
// makes login or status fail.
func printVersionSkew(ctx context.Context, client *http.Client, backplaneURL string, w io.Writer) {
	ctx, cancel := context.WithTimeout(ctx, versionCheckTimeout)
	defer cancel()
	bp, err := version.FetchBackplane(ctx, client, backplaneURL)
	if err != nil {
		return
	}
	if msg := version.SkewWarning(version.Get().Version, bp); msg != "" {
		fmt.Fprintln(w, msg)
	}
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
