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

	"github.com/spf13/cobra"
	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/migrate"
)

// newLoginCmd returns the `meho login` subcommand. The cobra surface
// stays thin — argument parsing, flag wiring, IO routing — so the
// flow logic lives in internal/auth/ where it's straightforward to
// unit-test without a cobra harness.
//
// G2.6-T2 ships device-code flow only. Refresh tokens (v0.2),
// browser auto-launch (deferred per task body), and logout (#45's
// successor — not in scope for this Task) deliberately stay out.
func newLoginCmd() *cobra.Command {
	var (
		issuerOverride   string
		clientIDOverride string
		scopes           []string
	)

	cmd := &cobra.Command{
		Use:   "login <backplane-url>",
		Short: "Authenticate against the MEHO backplane via Keycloak device-code flow",
		Long: "login starts the OAuth 2.0 Device Authorization Grant (RFC 8628) " +
			"against the backplane's configured Keycloak realm.\n\n" +
			"The CLI prints a verification URL and a short user_code. " +
			"Open the URL on any device with a browser, sign in, and approve " +
			"the request. login then stores the resulting access token in the " +
			"OS keyring (Keychain on macOS, Secret Service on Linux, Wincred " +
			"on Windows) and falls back to a 0600-mode credentials file at " +
			"$XDG_CONFIG_HOME/meho/credentials.json on headless hosts.\n\n" +
			"Discovery: by default the CLI fetches the backplane's auth-config " +
			"endpoint at <backplane-url>/api/v1/auth-config to learn the realm " +
			"issuer and OAuth client ID. Until that endpoint ships, pass " +
			"--issuer and --client-id explicitly.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			backplaneURL := strings.TrimRight(args[0], "/")
			if _, err := url.ParseRequestURI(backplaneURL); err != nil {
				return fmt.Errorf("invalid backplane URL %q: %w", backplaneURL, err)
			}

			ctx, cancel := context.WithTimeout(cmd.Context(), auth.PollTimeout)
			defer cancel()

			// Resolve auth config. Override flags win; otherwise hit
			// the backplane's discovery endpoint. The override path
			// is the documented fallback while G2.2 is still wiring
			// up /api/v1/auth-config — operators can ship a working
			// login today even though the backplane endpoint doesn't
			// exist yet.
			cfg, err := resolveAuthConfig(ctx, http.DefaultClient, backplaneURL, issuerOverride, clientIDOverride)
			if err != nil {
				return err
			}

			doc, err := auth.FetchDiscoveryFromRealm(ctx, http.DefaultClient, cfg.Issuer)
			if err != nil {
				return err
			}

			store, err := auth.NewTokenStore()
			if err != nil {
				return err
			}

			// Prompter renders the verification URL + user code to
			// stdout. Output discipline: this is a prompt, not an
			// error, so it goes to stdout (per Goal #11 §5 + matches
			// `gh auth login`, `flux bootstrap` behaviour).
			out := cmd.OutOrStdout()
			prompter := stdoutPrompter(out)

			result, err := auth.RunDeviceFlow(ctx, doc, cfg.ClientID, auth.DeviceFlowOptions{
				HTTPClient: http.DefaultClient,
				Scopes:     scopes,
				Prompter:   prompter,
			})
			if err != nil {
				return err
			}

			stored := auth.ConvertOAuthToken(result.Token, backplaneURL, result.Issuer, cfg.ClientID)
			service, user := auth.KeyForBackplane(backplaneURL)
			if err := store.Save(service, user, stored); err != nil {
				return fmt.Errorf("token obtained but storage failed: %w", err)
			}

			// Persist the backplane URL to the unauthenticated config
			// file so future subcommands (meho status, future ops)
			// can recover it without asking the operator to retype
			// it. The config file carries no secrets — only the URL
			// — so a write failure is surfaced as a warning, not a
			// hard error: the token is already safely stored, and
			// the operator can supply --backplane explicitly on the
			// next invocation.
			if err := auth.SaveConfig(auth.Config{BackplaneURL: backplaneURL}); err != nil {
				fmt.Fprintf(cmd.ErrOrStderr(),
					"warning: failed to persist backplane URL to config file: %v\n", err)
			}

			fmt.Fprintf(out, "Logged in to %s; token stored in %s.\n", backplaneURL, store.Describe())
			printMigrationNudge(out)
			return nil
		},
	}

	cmd.Flags().StringVar(&issuerOverride, "issuer", "",
		"Keycloak realm issuer URL (auto-discovered from the backplane when blank)")
	cmd.Flags().StringVar(&clientIDOverride, "client-id", "",
		"OAuth client_id to use for the device-code flow (auto-discovered when blank)")
	cmd.Flags().StringSliceVar(&scopes, "scope", nil,
		"OAuth scopes to request (default: openid). Repeat or comma-separate for multiple.")
	return cmd
}

// authConfig is what either the backplane discovery endpoint or the
// operator-supplied overrides resolve to. Kept as a private struct so
// the public surface stays oauth2-focused; consumers outside this
// package have no business knowing how login plumbed its discovery.
type authConfig struct {
	Issuer   string
	ClientID string
}

// resolveAuthConfig picks the auth config in priority order:
//
//  1. Overrides — both --issuer and --client-id supplied skips
//     backplane discovery entirely. This is the documented fallback
//     while G2.2's /api/v1/auth-config endpoint is still being wired
//     up, and stays useful long-term for operators behind locked-down
//     networks where the backplane isn't reachable until VPN is up
//     but the IdP is.
//  2. Partial overrides — if only one flag is supplied, we still hit
//     the backplane for the other half. Lets operators pin one half
//     (e.g. a non-standard client_id) without giving up auto-discovery
//     for the other.
//  3. Full discovery — call <backplane>/api/v1/auth-config.
//
// httpClient is injected for testability.
func resolveAuthConfig(ctx context.Context, httpClient *http.Client, backplaneURL, issuerOverride, clientIDOverride string) (authConfig, error) {
	if issuerOverride != "" && clientIDOverride != "" {
		return authConfig{Issuer: issuerOverride, ClientID: clientIDOverride}, nil
	}

	cfg, err := fetchBackplaneAuthConfig(ctx, httpClient, backplaneURL)
	if err != nil {
		if issuerOverride != "" || clientIDOverride != "" {
			// Caller pinned at least one half; surface the discovery
			// failure with a hint that pinning both flags is the
			// supported fallback.
			return authConfig{}, fmt.Errorf(
				"backplane auth-config discovery failed (%w); pass both --issuer and --client-id to skip discovery",
				err,
			)
		}
		return authConfig{}, fmt.Errorf(
			"backplane auth-config discovery failed (%w); rerun with --issuer and --client-id",
			err,
		)
	}

	if issuerOverride != "" {
		cfg.Issuer = issuerOverride
	}
	if clientIDOverride != "" {
		cfg.ClientID = clientIDOverride
	}
	return cfg, nil
}

// fetchBackplaneAuthConfig queries the backplane for its OIDC
// configuration. The endpoint is documented in Initiative #42 / Task
// #44 as /api/v1/auth-config — once G2.2 ships it, this function
// becomes the happy path. Until then it'll return a discovery error
// and operators use the --issuer / --client-id overrides.
//
// Response shape (per the Task body coordination note):
//
//	{ "keycloak_issuer": "...", "audience": "..." }
//
// We map keycloak_issuer → Issuer and audience → ClientID; the
// audience claim in Keycloak's JWT is the OAuth client_id by
// default, so the same value drives both.
func fetchBackplaneAuthConfig(ctx context.Context, httpClient *http.Client, backplaneURL string) (authConfig, error) {
	endpoint := backplaneURL + "/api/v1/auth-config"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, http.NoBody)
	if err != nil {
		return authConfig{}, fmt.Errorf("build auth-config request: %w", err)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		return authConfig{}, fmt.Errorf("call %s: %w", endpoint, err)
	}
	defer func() { _ = resp.Body.Close() }()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return authConfig{}, fmt.Errorf("read auth-config response: %w", err)
	}
	if resp.StatusCode/100 != 2 {
		excerpt := string(body)
		if len(excerpt) > 256 {
			excerpt = excerpt[:256] + "…"
		}
		return authConfig{}, fmt.Errorf("auth-config %s returned HTTP %d: %s", endpoint, resp.StatusCode, excerpt)
	}

	var payload struct {
		KeycloakIssuer string `json:"keycloak_issuer"`
		Audience       string `json:"audience"`
	}
	if err := decodeJSON(body, &payload); err != nil {
		return authConfig{}, fmt.Errorf("parse auth-config: %w", err)
	}
	if payload.KeycloakIssuer == "" || payload.Audience == "" {
		return authConfig{}, errors.New("auth-config response missing keycloak_issuer or audience")
	}
	return authConfig{Issuer: payload.KeycloakIssuer, ClientID: payload.Audience}, nil
}

// decodeJSON is a tiny wrapper that lets us swap json.Unmarshal for a
// streaming decoder later (yagni for v0.1; the response is bounded
// at 64 KiB). Kept extracted so call sites read the same way.
func decodeJSON(data []byte, into any) error {
	return json.Unmarshal(data, into)
}

// printMigrationNudge prints a one-line tip when the operator has
// memory files in the default source directory that have not yet been
// migrated. Any error (dir resolution failure, stat error) silently
// degrades to "print nothing" — the nudge must never block or fail login.
func printMigrationNudge(w io.Writer) {
	dir, err := migrate.ResolveSourceDir("")
	if err != nil {
		return
	}
	ok, err := migrate.MarkerExists(dir)
	if err != nil || ok {
		return
	}
	files, err := migrate.ScanDir(dir)
	if err != nil || len(files) == 0 {
		return
	}
	fmt.Fprintf(w, "Tip: you have %d memory file(s) at %s. Run `meho migrate memory` to sync them to MEHO.\n",
		len(files), dir)
}

// stdoutPrompter writes the device-code prompt to w. Format mirrors
// `gh auth login`: the URL and the user_code on separate lines so
// operators can copy each cleanly. The verification_uri (not
// verification_uri_complete) is preferred because the operator's
// device may not be the same one running the CLI — a QR-code
// rendering of verification_uri_complete is a v0.2 enhancement.
func stdoutPrompter(w io.Writer) auth.DeviceFlowPrompter {
	return func(_ context.Context, resp *oauth2.DeviceAuthResponse) error {
		fmt.Fprintf(w, "\nTo authenticate, open the following URL:\n  %s\n\nAnd enter the code:\n  %s\n\n",
			resp.VerificationURI, resp.UserCode)
		// Print without a trailing newline so the spinner-less wait
		// for the IdP doesn't leave the cursor mid-line if the
		// operator inspects the terminal during polling.
		fmt.Fprintf(w, "Waiting for authorisation…\n")
		return nil
	}
}
