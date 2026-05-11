// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package api is the typed HTTP surface for the meho backplane. The
// generated client (client.gen.go) is produced by oapi-codegen v2.5
// from the OpenAPI 3.0 snapshot at cli/api/openapi.json — see
// cli/api/oapi.config.yaml for the generation knobs.
//
// This file is the hand-written sibling: NewAuthedClient assembles
// a ClientWithResponses pre-wired with the operator's stored
// bearer token (Authorization: Bearer …) and a one-shot 401-retry
// refresh path that transparently exchanges an expired access_token
// for a new one before the request leaves the CLI. The refresh
// path uses golang.org/x/oauth2's TokenSource — the same package
// that powers `meho login`'s device-code flow — so the issuer /
// client_id / scope tuple stays consistent between login and
// status.
package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/evoila/meho/cli/internal/auth"
)

// AuthedClient wraps the generated ClientWithResponses with the
// auth-aware machinery the meho subcommands need: stored-token
// retrieval, bearer-header injection, and best-effort refresh on
// expiry. Embeds rather than wraps so callers see the full
// generated surface (every typed op the spec exposes) without an
// indirection layer per call.
type AuthedClient struct {
	*ClientWithResponses

	// box holds the current bearer + refresh plumbing. Lifted into
	// its own struct (refresh.go) so the editor function closes
	// over a stable handle and the refresh path stays orthogonal
	// to the generated client.
	box *tokenBox

	// store + service / user remember where the token came from so
	// a successful refresh can write the new token back to the
	// same backend without re-deriving the address. Persisted with
	// the client so persistRefresh can stamp the new expiry without
	// forcing callers to thread (service, user) through every
	// command.
	store   auth.TokenStore
	service string
	user    string
}

// AuthedClientOptions configures NewAuthedClient. Every field has a
// working zero value, mirroring DeviceFlowOptions in
// internal/auth/devicecode.go.
type AuthedClientOptions struct {
	// HTTPClient is the http.Client both the bearer-injecting
	// request editor and the oauth2 refresh exchange use. Pass nil
	// for http.DefaultClient. Tests pass an httptest.Server's
	// client.
	HTTPClient *http.Client
	// Store overrides the default TokenStore. Tests pass an
	// in-memory store; production code passes nil so
	// NewAuthedClient picks via auth.NewTokenStore.
	Store auth.TokenStore
	// RefreshDiscoverer overrides the token-endpoint discovery
	// step on refresh. Tests pass a fake; production code passes
	// nil so NewAuthedClient routes to auth.FetchDiscoveryFromRealm.
	RefreshDiscoverer func(ctx context.Context, httpClient *http.Client, issuerURL string) (*auth.DiscoveryDocument, error)
}

// NewAuthedClient builds an AuthedClient for the supplied backplane
// URL. The function looks up the stored token (errors.Is matching
// auth.ErrTokenNotFound when no operator ever ran `meho login` for
// this backplane), wires a refresh-aware editor, and assembles a
// generated client with the Authorization header injected by a
// RequestEditorFn.
//
// Returns an error satisfying IsTokenNotFound when the operator
// hasn't run `meho login`; callers errors.Is against it to surface
// a `meho login <url>` hint via output.AuthExpired.
func NewAuthedClient(_ context.Context, backplaneURL string, opts AuthedClientOptions) (*AuthedClient, error) {
	store := opts.Store
	if store == nil {
		s, err := auth.NewTokenStore()
		if err != nil {
			return nil, fmt.Errorf("meho: token store: %w", err)
		}
		store = s
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	tok, err := store.Load(service, user)
	if err != nil {
		return nil, err
	}

	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = http.DefaultClient
	}

	discoverer := opts.RefreshDiscoverer
	if discoverer == nil {
		discoverer = fetchDiscovery
	}

	box := &tokenBox{
		current:           tok,
		httpClient:        httpClient,
		refreshDiscoverer: discoverer,
	}

	editor := func(_ context.Context, req *http.Request) error {
		// Read the current bearer under the box's mutex so a
		// refresh in flight on another call site can't surface a
		// torn string. Empty bearer would mean "we're sending an
		// unauthenticated request" — flag it so the server-side
		// 401 path is the only auth-failure surface.
		bearer := box.snapshot()
		if bearer == "" {
			return errors.New("meho: stored token has no access_token")
		}
		req.Header.Set("Authorization", authorizationHeader(bearer))
		return nil
	}

	clientWR, err := NewClientWithResponses(
		backplaneURL,
		WithHTTPClient(httpClient),
		WithRequestEditorFn(editor),
	)
	if err != nil {
		return nil, fmt.Errorf("meho: build api client: %w", err)
	}

	authed := &AuthedClient{
		ClientWithResponses: clientWR,
		box:                 box,
		store:               store,
		service:             service,
		user:                user,
	}
	box.onRefresh = authed.persistRefresh
	return authed, nil
}

// persistRefresh saves the post-refresh token back to the original
// store. Called by the tokenBox after a successful refresh
// exchange. Any save error is swallowed: the in-flight request
// already has the new bearer, and the next CLI invocation will
// hit the same refresh path again. Surfacing the failure here
// would mean breaking a working status command because the
// storage was momentarily unhappy.
func (c *AuthedClient) persistRefresh(updated auth.StoredToken) {
	_ = c.store.Save(c.service, c.user, updated)
}

// GetHealth calls GET /api/v1/health, transparently refreshing the
// access token once on a 401. Returns the typed response on
// success; a tokenRefreshFailedError when refresh was attempted
// and rejected by the IdP; or any underlying transport error.
//
// The 401-retry pattern is the meho equivalent of curl --oauth2
// behaviour: the first call uses the stored bearer; if the
// backplane rejects it, the CLI refreshes and re-issues. Without
// the refresh, an expired token would force the operator to
// re-run `meho login` on every short-lived access_token rotation.
func (c *AuthedClient) GetHealth(ctx context.Context) (*AuthenticatedHealthApiV1HealthGetResponse, error) {
	resp, err := c.AuthenticatedHealthApiV1HealthGetWithResponse(ctx, nil)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusUnauthorized {
		return resp, nil
	}
	// 401 path: try a one-shot refresh, then re-issue. Refresh
	// failures (no refresh_token, IdP rejected) propagate so the
	// command layer can surface auth_expired.
	if rerr := c.box.refresh(ctx); rerr != nil {
		return resp, rerr
	}
	return c.AuthenticatedHealthApiV1HealthGetWithResponse(ctx, nil)
}

// IsTokenNotFound reports whether err is the "operator never ran
// meho login" sentinel. Exposed as a helper because the underlying
// auth package is intentionally not imported by cobra command
// files (root.go, status.go) outside this seam.
func IsTokenNotFound(err error) bool {
	return errors.Is(err, auth.ErrTokenNotFound)
}

// IsNoRefreshToken reports whether err is the "stored token never
// carried a refresh_token" sentinel. Lets the cobra command map
// the refresh-impossible case onto output.AuthExpired (the
// operator must rerun `meho login`).
func IsNoRefreshToken(err error) bool {
	return errors.Is(err, errNoRefreshToken)
}
