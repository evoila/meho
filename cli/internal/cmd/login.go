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

	"github.com/spf13/cobra"
	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
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
		issuerOverride    string
		clientIDOverride  string
		scopes            []string
		insecureAllowHTTP bool
		resolveEntries    []string
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
			"Credential-store fallback: macOS Keychain caps a single value at " +
			"~4 KiB, which a full OIDC token bundle can exceed. When the " +
			"keyring rejects the token by size, the CLI transparently writes " +
			"to the credentials file instead and the success message names " +
			"that backend. Set MEHO_KEYRING_DISABLE=1 to force the file " +
			"backend unconditionally (useful on shared dev hosts where the " +
			"keyring belongs to another session, or in CI).\n\n" +
			"Discovery: by default the CLI fetches the backplane's auth-config " +
			"endpoint at <backplane-url>/api/v1/auth-config to learn the realm " +
			"issuer and the public device-code client_id. Pass --issuer and/or " +
			"--client-id to skip discovery or override either half (e.g. when " +
			"the backplane URL isn't reachable on the operator's network but " +
			"the IdP is).\n\n" +
			"Split-DNS escape hatch: on workstations where the system resolver " +
			"can reach the backplane host but returns NXDOMAIN for the Keycloak " +
			"host (a common VPN split-DNS quirk), pass --resolve " +
			"<host>:<port>:<ip> to pin that host to a known IP for the duration " +
			"of the flow. The format mirrors `curl --resolve`; repeat the flag " +
			"for multiple hosts. The pinned IP is only used at connect time — " +
			"the original hostname is still sent as the TLS SNI and Host header, " +
			"so certificate validation is unchanged. Example: " +
			"`meho login https://<backplane> --resolve kc.example.com:443:10.0.0.5`.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Normalise + enforce transport security before any
			// network call. https is required by default so the
			// bearer token minted below is never sent in the clear;
			// --insecure-allow-http opts a loopback backplane out
			// (see NormaliseURLAllowHTTP).
			backplaneURL, err := backplane.NormaliseURLAllowHTTP(args[0], insecureAllowHTTP)
			if err != nil {
				return err
			}

			// Parse any --resolve host:port:ip overrides and build the
			// HTTP client the whole flow shares. When no override is
			// supplied this is http.DefaultClient, so the common path is
			// unchanged; when one is, the same pinned-dial transport is
			// threaded through auth-config discovery, OIDC discovery, and
			// the device-code/token endpoints alike (AC #2 — the knob
			// must reach every call site, not just discovery).
			overrides, err := auth.ParseResolveEntries(resolveEntries)
			if err != nil {
				return err
			}
			httpClient := auth.HTTPClientWithOverrides(overrides)

			// Discovery and auth-config fetches honour the ambient
			// `cmd.Context()` deadline: those are short, bounded HTTP
			// calls that should fail fast if the network is wedged.
			// Only the interactive device-flow wait below detaches
			// from that ambient context (see NewDeviceFlowContext) —
			// without that split, a wrapping CI step or bash-tool
			// deadline would silently truncate the operator's
			// approval window and `context deadline exceeded` would
			// be misattributed to the device code itself
			// (Initiative G0.9.1, Wall #4).
			parentCtx := cmd.Context()

			// Resolve auth config. Override flags win; otherwise hit
			// the backplane's discovery endpoint. The override path
			// stays useful when the backplane URL isn't reachable on
			// the operator's network (locked-down VPN, intermediate
			// firewall) but the IdP is — meho login can still
			// complete by skipping discovery via both --issuer and
			// --client-id.
			cfg, err := resolveAuthConfig(parentCtx, httpClient, backplaneURL, issuerOverride, clientIDOverride)
			if err != nil {
				return err
			}

			doc, err := auth.FetchDiscoveryFromRealm(parentCtx, httpClient, cfg.Issuer)
			if err != nil {
				// OIDC discovery hits the Keycloak host, not the
				// backplane. A DNS failure here is the split-DNS case
				// the --resolve flag exists for; name the host that
				// failed (distinct from the backplane-side auth-config
				// error above) and point at the escape hatch.
				return hintKeycloakResolution(err, cfg.Issuer)
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

			// Build the detached device-flow context. Inherits values
			// from parentCtx (so future oauth2.HTTPClient injections
			// at this layer would still ride through), drops the
			// parent's deadline (so a short wrapper deadline can't
			// truncate the approval wait), and re-attaches
			// SIGINT/SIGTERM cancellation + a PollTimeout cap.
			flowCtx, cancelFlow := auth.NewDeviceFlowContext(parentCtx)
			defer cancelFlow()

			result, err := auth.RunDeviceFlow(flowCtx, doc, cfg.ClientID, auth.DeviceFlowOptions{
				HTTPClient:    httpClient,
				Scopes:        scopes,
				Prompter:      prompter,
				ParentContext: parentCtx,
			})
			if err != nil {
				// The device-authorization and token endpoints also live
				// on the Keycloak host, so the same split-DNS hint
				// applies to a resolution failure here.
				return hintKeycloakResolution(err, cfg.Issuer)
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
	cmd.Flags().BoolVar(&insecureAllowHTTP, "insecure-allow-http", false,
		"permit a plaintext http:// backplane URL for a localhost backplane only "+
			"(local-dev convenience; the bearer token is sent in the clear — never use against a remote host)")
	cmd.Flags().StringSliceVar(&resolveEntries, "resolve", nil,
		"pin a host to an IP for the flow, mirroring `curl --resolve <host>:<port>:<ip>` "+
			"(split-DNS escape hatch when the Keycloak host doesn't resolve). Repeat for multiple hosts. "+
			"TLS SNI/Host use the real hostname, so certificate validation is unaffected")
	return cmd
}

// hintKeycloakResolution rewraps err when it was caused by DNS
// resolution failing against the Keycloak host. The bare transport error
// ("no such host") does not tell the operator which of the two hosts in
// play — the backplane (which resolved fine, it's how we got the issuer)
// or Keycloak — is unreachable, nor how to work around it. This hint
// names the Keycloak host explicitly (distinct from the backplane-side
// auth-config discovery failure, which is worded around --issuer /
// --client-id) and points at the --resolve escape hatch. Any error that
// is not a resolution failure is returned unchanged so the existing
// device-flow classification (expired_token, access_denied, timeouts)
// still reaches the operator verbatim.
func hintKeycloakResolution(err error, issuer string) error {
	if err == nil || !auth.IsHostResolutionError(err) {
		return err
	}
	host := issuer
	if u, parseErr := url.Parse(issuer); parseErr == nil && u.Host != "" {
		host = u.Hostname()
	}
	return fmt.Errorf(
		"could not resolve the Keycloak host %q (the backplane resolved fine; this is a split-DNS gap): %w; "+
			"pass --resolve %s:443:<keycloak-ip> to pin it to a known IP for this login (mirrors `curl --resolve`)",
		host, err, host,
	)
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
		// The remediation differs depending on the failure shape, but
		// the CLI can't tell a TLS verify error from a 404 reliably
		// without sniffing the wrapped error chain — and a misleading
		// hint is worse than a complete one. Both error paths name
		// the flag-override fallback AND the TLS-trust remediation so
		// the operator can pick whichever matches what they see. (The
		// TLS hint addresses internal-CA deployments where the
		// operator's system trust store doesn't yet know the
		// deployment's CA; Goal #11 RDC dogfood Signal #16, 2026-05-21.)
		if issuerOverride != "" || clientIDOverride != "" {
			// Caller pinned at least one half; surface the discovery
			// failure with a hint that pinning both flags is the
			// supported fallback.
			return authConfig{}, fmt.Errorf(
				"backplane auth-config discovery failed (%w); pass both --issuer and --client-id to skip discovery, or install your deployment's root CA in your system trust store",
				err,
			)
		}
		return authConfig{}, fmt.Errorf(
			"backplane auth-config discovery failed (%w); rerun with --issuer and --client-id, or install your deployment's root CA in your system trust store",
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
// configuration. The endpoint shipped with v0.3.1 carried two fields
// (issuer + audience); v0.3.2 (G0.9.1-T9, after RDC dogfood Signal #16)
// added the public device-code client_id as a third field.
//
// Response shape:
//
//	{
//	  "keycloak_issuer": "...",      // realm URL — driven into Issuer
//	  "audience":        "...",      // backplane resource-server id
//	  "cli_client_id":   "..."       // public device-code client — driven into ClientID
//	}
//
// We map keycloak_issuer → Issuer and cli_client_id → ClientID. The
// audience field is intentionally NOT used here as ClientID: it is
// the confidential resource-server identifier the backplane validates
// inbound JWTs against, and Keycloak rejects device-code initiation
// against a confidential client with `401 unauthorized_client` (the
// device grant requires a public client because the CLI can't carry
// a client secret). v0.3.1 mis-mapped audience → ClientID and broke
// `meho login`'s documented happy path; the v0.3.2 endpoint adds the
// dedicated cli_client_id field for this exact reason. Older
// backplanes that don't carry the field — or operators that haven't
// wired KEYCLOAK_CLI_CLIENT_ID — surface as an empty string here,
// which we promote to an actionable error naming the public-client
// requirement rather than silently retrying with audience.
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
		CLIClientID    string `json:"cli_client_id"`
	}
	if err := decodeJSON(body, &payload); err != nil {
		return authConfig{}, fmt.Errorf("parse auth-config: %w", err)
	}
	if payload.KeycloakIssuer == "" || payload.Audience == "" {
		return authConfig{}, errors.New("auth-config response missing keycloak_issuer or audience")
	}
	if payload.CLIClientID == "" {
		// Treat absent-key and empty-string identically — both mean
		// "this backplane has not been wired with a public CLI
		// client_id". Naming the public-client requirement (and the
		// override escape hatch) up front is the highest-signal hint
		// we can give an operator who has only ever seen Keycloak's
		// `401 unauthorized_client` from the device-grant endpoint.
		return authConfig{}, fmt.Errorf(
			"auth-config response carries no cli_client_id: the backplane has not been wired with a public OAuth client for device-code login. " +
				"Ask your deployer to register a public Keycloak client (suggested name `meho-cli`, device-grant enabled, audience mapper -> backplane) " +
				"and set the chart value `config.keycloakCliClientId` (env `KEYCLOAK_CLI_CLIENT_ID`) to its client_id. " +
				"Override per-invocation with `--client-id <public-client-id>`",
		)
	}
	return authConfig{Issuer: payload.KeycloakIssuer, ClientID: payload.CLIClientID}, nil
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
