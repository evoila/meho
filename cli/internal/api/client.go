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
	// ResponseBodyLimit caps the bytes the underlying transport reads
	// off `rsp.Body` for every response. Zero (default) means no cap
	// — back-compat with consumers that haven't opted in. When > 0,
	// NewAuthedClient wraps the client's transport in a
	// RoundTripper that re-binds `rsp.Body` to an
	// `http.MaxBytesReader`-equivalent so the generated
	// `*WithResponse` parsers (which call `io.ReadAll(rsp.Body)`)
	// can't be pinned by an adversarial / runaway backplane response.
	// A read at the cap surfaces as `*http.MaxBytesError`, which
	// consumer-side `renderRequestError` helpers map to
	// `output.Unexpected` (exit 4) rather than `output.Unreachable`
	// (exit 3). 1 MiB is the convention adopted by the kb verb tree
	// (`cli/internal/cmd/kb/kb.go`); other consumers should pick a
	// per-package cap matching their largest expected payload.
	ResponseBodyLimit int64
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
	if opts.ResponseBodyLimit > 0 {
		// Clone so capping the body cap doesn't mutate the
		// caller-supplied http.Client (notably
		// http.DefaultClient, which is process-global). The clone
		// keeps Timeout / Jar / CheckRedirect intact and only
		// swaps the Transport for a capped wrapper.
		clone := *httpClient
		base := clone.Transport
		if base == nil {
			base = http.DefaultTransport
		}
		clone.Transport = &capRoundTripper{base: base, limit: opts.ResponseBodyLimit}
		httpClient = &clone
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

// HTTPClient returns the underlying http.Client the AuthedClient
// uses for all backplane traffic. Exposed so subcommand files that
// call endpoints not yet wrapped by the oapi-codegen-generated
// methods (e.g. /api/v1/retrieve/eval before the next
// `make generate` pass) can issue typed JSON requests on the same
// transport — same TLS config, same proxy settings, same timeouts
// — as the generated client.
//
// The returned client carries no bearer-injecting RoundTripper;
// callers are expected to set the Authorization header themselves
// using the bearer returned from AccessToken (and to invoke Refresh
// on a 401, mirroring GetHealth's one-shot-retry behaviour).
func (c *AuthedClient) HTTPClient() *http.Client {
	return c.box.httpClient
}

// AccessToken returns the current bearer string the AuthedClient
// would attach to a generated-client call. Exposed for the same
// reason as HTTPClient: subcommand files calling unwrapped endpoints
// need the bearer the editor function would have injected.
//
// Reads under the tokenBox mutex so an in-flight refresh on another
// goroutine can't surface a torn string.
func (c *AuthedClient) AccessToken() string {
	return c.box.snapshot()
}

// Refresh runs a one-shot refresh exchange against the IdP, replacing
// the in-memory bearer if the IdP returns a fresh token. Returns
// errNoRefreshToken (matched by IsNoRefreshToken) when the stored
// token didn't carry a refresh_token; any other refresh failure
// propagates verbatim.
//
// Subcommand files that issue unwrapped HTTP calls invoke Refresh
// after a 401 to mirror GetHealth's transparent-retry behaviour.
// Concurrent calls serialise on the tokenBox mutex so the IdP is
// hit at most once per stale access token.
func (c *AuthedClient) Refresh(ctx context.Context) error {
	return c.box.refresh(ctx)
}

// IsNoRefreshToken reports whether err is the "stored token never
// carried a refresh_token" sentinel. Lets the cobra command map
// the refresh-impossible case onto output.AuthExpired (the
// operator must rerun `meho login`).
func IsNoRefreshToken(err error) bool {
	return errors.Is(err, errNoRefreshToken)
}

// capRoundTripper wraps an http.RoundTripper so every response body
// is re-bound to an http.MaxBytesReader before the typed-client
// parsers (oapi-codegen's generated `Parse*Response` helpers, which
// `io.ReadAll(rsp.Body)` to populate `*Response.Body []byte`) get a
// chance to drain it. Without this, an adversarial or runaway
// backplane response could OOM the CLI by streaming an unbounded
// body into ReadAll. The wrapper applies the cap server-wide on the
// underlying transport so every typed verb on the same AuthedClient
// inherits it uniformly.
//
// Reads at or past `limit` surface as `*http.MaxBytesError` out of
// the body Reader, which the kb verb's `renderRequestError` (and
// any future consumer following the same pattern) maps to
// `output.Unexpected` (exit 4 — `unexpected_response`) rather than
// `output.Unreachable` (exit 3 — `network_unreachable`). The
// `*http.MaxBytesError` shape was added in Go 1.19 and is the
// canonical signal for "transport refused to read past N bytes."
type capRoundTripper struct {
	base  http.RoundTripper
	limit int64
}

func (c *capRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := c.base.RoundTrip(req)
	if err != nil {
		return resp, err
	}
	if resp.Body != nil && c.limit > 0 {
		// http.MaxBytesReader returns an io.ReadCloser whose Close
		// closes the underlying body, so the existing close
		// discipline on the caller (oapi-codegen's `defer
		// rsp.Body.Close()` inside every `*WithResponse` method)
		// still drains the original body cleanly.
		resp.Body = http.MaxBytesReader(nil, resp.Body, c.limit)
	}
	return resp, nil
}
