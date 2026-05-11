// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

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

	"golang.org/x/oauth2"
)

// DiscoveryDocument is the subset of an OAuth 2.0 / OIDC discovery
// document the device-code flow needs. We deliberately decode only
// the fields we use — the spec permits arbitrary extra claims and we
// don't want to fail because a Keycloak release added a new key.
//
// Field names match RFC 8414 (OAuth 2.0 Authorization Server
// Metadata) and OpenID Connect Discovery 1.0; the JSON tags are the
// canonical names per those specs.
type DiscoveryDocument struct {
	// Issuer is the canonical realm URL the IdP self-identifies as.
	// We compare this against the issuer claim of the issued JWT to
	// reject mis-targeted tokens.
	Issuer string `json:"issuer"`
	// DeviceAuthorizationEndpoint is RFC 8628 §3.1 — where we POST
	// the device-code initiation. Keycloak puts this at
	// {realm}/protocol/openid-connect/auth/device.
	DeviceAuthorizationEndpoint string `json:"device_authorization_endpoint"`
	// TokenEndpoint is where we poll for the token. Keycloak puts
	// this at {realm}/protocol/openid-connect/token.
	TokenEndpoint string `json:"token_endpoint"`
}

// DiscoveryError wraps the failure modes of FetchDiscovery so
// callers can render a useful error without re-implementing the
// classification logic. Returned as a typed value (not a sentinel)
// because every failure carries situational context — the URL
// attempted, the HTTP status, the upstream body excerpt — that
// helps operators figure out which side of the seam is misbehaving.
type DiscoveryError struct {
	// URL is the discovery document URL we attempted.
	URL string
	// Status is the upstream HTTP status, when one came back.
	// Zero for transport-layer failures (connection refused, TLS
	// handshake failure).
	Status int
	// Underlying carries the wrapped lower-level error so callers
	// can errors.Is against it (e.g. context.DeadlineExceeded).
	Underlying error
	// Body excerpts the upstream response body for debugging.
	// Truncated to avoid spraying multi-megabyte HTML error pages
	// into operator terminals.
	Body string
}

func (e *DiscoveryError) Error() string {
	if e.Status != 0 {
		return fmt.Sprintf("meho: discovery %s returned HTTP %d: %s", e.URL, e.Status, e.Body)
	}
	return fmt.Sprintf("meho: discovery %s failed: %v", e.URL, e.Underlying)
}

func (e *DiscoveryError) Unwrap() error { return e.Underlying }

// FetchDiscovery loads the OAuth 2.0 / OIDC discovery document at the
// supplied URL. The URL should end in either
// .well-known/openid-configuration (OIDC) or
// .well-known/oauth-authorization-server (RFC 8414). Both shapes
// expose the device-authorization-endpoint field we depend on, so we
// don't differentiate at the call site.
//
// httpClient is injectable so tests can pass an httptest.Server's
// client; pass nil to use http.DefaultClient (which is what every
// production call site does).
func FetchDiscovery(ctx context.Context, httpClient *http.Client, discoveryURL string) (*DiscoveryDocument, error) {
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, discoveryURL, http.NoBody)
	if err != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Underlying: err}
	}
	// Discovery responses are JSON; some IdPs (Keycloak included)
	// 406-out without an explicit Accept header on certain
	// configurations.
	req.Header.Set("Accept", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Underlying: err}
	}
	defer func() { _ = resp.Body.Close() }()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if readErr != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Status: resp.StatusCode, Underlying: readErr}
	}
	if resp.StatusCode/100 != 2 {
		excerpt := string(body)
		// 256 chars is enough to identify a Keycloak error envelope
		// or an upstream proxy's HTML; anything longer is noise.
		if len(excerpt) > 256 {
			excerpt = excerpt[:256] + "…"
		}
		return nil, &DiscoveryError{URL: discoveryURL, Status: resp.StatusCode, Body: excerpt}
	}

	var doc DiscoveryDocument
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Status: resp.StatusCode, Underlying: fmt.Errorf("decode: %w", err)}
	}
	if doc.DeviceAuthorizationEndpoint == "" {
		return nil, &DiscoveryError{
			URL:        discoveryURL,
			Status:     resp.StatusCode,
			Underlying: errors.New("discovery document missing device_authorization_endpoint"),
		}
	}
	if doc.TokenEndpoint == "" {
		return nil, &DiscoveryError{
			URL:        discoveryURL,
			Status:     resp.StatusCode,
			Underlying: errors.New("discovery document missing token_endpoint"),
		}
	}
	return &doc, nil
}

// DiscoveryURLs returns the candidate well-known URLs to try in
// order for a given Keycloak realm root. Operators configure the
// CLI with the realm URL (e.g. https://kc.example.com/realms/meho);
// from that, the two standard well-known paths are mechanical
// derivations. We try OIDC first because it's the more common shape
// and Keycloak's primary advertisement; the RFC 8414 path is a
// belt-and-braces fallback for IdPs that don't expose the OIDC
// document but do publish the OAuth one.
func DiscoveryURLs(realmURL string) []string {
	base := strings.TrimRight(realmURL, "/")
	return []string{
		base + "/.well-known/openid-configuration",
		base + "/.well-known/oauth-authorization-server",
	}
}

// FetchDiscoveryFromRealm walks DiscoveryURLs in order, returning
// the first successful document. The combined error preserves every
// attempt's diagnostic so an operator can see exactly what failed
// and where — useful when a misconfigured realm URL produces a 404
// at one path and a 500 at the other.
func FetchDiscoveryFromRealm(ctx context.Context, httpClient *http.Client, realmURL string) (*DiscoveryDocument, error) {
	var errs []error
	for _, u := range DiscoveryURLs(realmURL) {
		doc, err := FetchDiscovery(ctx, httpClient, u)
		if err == nil {
			return doc, nil
		}
		errs = append(errs, err)
	}
	return nil, fmt.Errorf("meho: realm discovery failed: %w", errors.Join(errs...))
}

// LoginResult is the bundle a successful device-code login produces.
// Returned by RunDeviceFlow so the calling cobra command can both
// render the user-facing summary and pass the token through to the
// store without re-parsing oauth2's internals.
type LoginResult struct {
	// Token is the oauth2.Token issued by the IdP. We hold the
	// upstream type rather than copying its fields so future
	// refresh-token flows (v0.2) can use the standard
	// oauth2.TokenSource pattern without converting back.
	Token *oauth2.Token
	// Issuer is the realm URL the discovery document advertised.
	// Persisted alongside the token; see StoredToken.Issuer.
	Issuer string
}

// DeviceFlowPrompter is invoked once the device-code initiation
// succeeds so the caller can show the operator the user_code and
// verification URL. Injectable so tests don't have to scrape stdout
// and so a future "--browser" flag can open xdg-open from a
// concrete cobra-aware implementation. The function is called
// synchronously before polling begins; returning an error short-
// circuits the flow (e.g. operator pressed Ctrl-C).
type DeviceFlowPrompter func(ctx context.Context, resp *oauth2.DeviceAuthResponse) error

// DeviceFlowOptions configures a device-code login. Every field has
// a working zero value so callers only set what they need.
type DeviceFlowOptions struct {
	// HTTPClient is the http.Client both discovery and the oauth2
	// device flow use. Pass nil for http.DefaultClient. Set
	// explicitly in tests against an httptest.Server.
	HTTPClient *http.Client
	// Scopes requests specific OAuth scopes. Keycloak's device flow
	// defaults to "openid" — that's the minimum we need (gives us
	// the JWT). Operators / future ops can add more (offline_access
	// for refresh tokens, etc.).
	Scopes []string
	// Prompter renders the user_code / verification_uri to the
	// operator. Must be non-nil — the device flow is unusable
	// without one. The cobra command in cmd/login.go supplies a
	// stdout-writing implementation; tests pass an in-memory one.
	Prompter DeviceFlowPrompter
}

// RunDeviceFlow performs RFC 8628 against the supplied discovery
// document. The function blocks until the IdP returns a token, the
// device code expires, the user denies the grant, or the context is
// cancelled.
//
// We use the oauth2 package's DeviceAuth / DeviceAccessToken
// helpers rather than rolling our own POSTs — that gets us the
// authorization_pending / slow_down handling, the interval-doubling
// on slow_down, and the expired_token classification for free, all
// of which are subtle to implement correctly per the spec.
func RunDeviceFlow(ctx context.Context, doc *DiscoveryDocument, clientID string, opts DeviceFlowOptions) (*LoginResult, error) {
	if opts.Prompter == nil {
		return nil, errors.New("meho: RunDeviceFlow requires a Prompter")
	}

	// Push the HTTPClient through the oauth2 context handle — that's
	// the package's documented mechanism for injecting an
	// http.Client. http.DefaultClient is fine when no override is
	// supplied; oauth2 falls back to it on its own when the context
	// key is absent, but we set it explicitly for symmetry with the
	// FetchDiscovery call earlier in the chain.
	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	flowCtx := context.WithValue(ctx, oauth2.HTTPClient, httpClient)

	scopes := opts.Scopes
	if len(scopes) == 0 {
		// "openid" is the minimum Keycloak's device-code flow
		// requires when the realm enforces OIDC semantics. Empty
		// scope lists trigger Keycloak to return an
		// invalid_scope error; rather than discover that
		// at runtime, we lock the default here.
		scopes = []string{"openid"}
	}

	cfg := oauth2.Config{
		ClientID: clientID,
		Endpoint: oauth2.Endpoint{
			DeviceAuthURL: doc.DeviceAuthorizationEndpoint,
			TokenURL:      doc.TokenEndpoint,
			// AuthStyle left at the zero value (AuthStyleAutoDetect)
			// because Keycloak's device-code flow uses public
			// clients in v0.1 — no client_secret to negotiate.
		},
		Scopes: scopes,
	}

	deviceResp, err := cfg.DeviceAuth(flowCtx)
	if err != nil {
		return nil, fmt.Errorf("meho: device auth initiation: %w", err)
	}

	if err := opts.Prompter(ctx, deviceResp); err != nil {
		return nil, err
	}

	// DeviceAccessToken implements the polling loop, respecting the
	// interval and slow_down semantics in RFC 8628 §3.5.
	tok, err := cfg.DeviceAccessToken(flowCtx, deviceResp)
	if err != nil {
		return nil, classifyDeviceTokenError(err)
	}

	return &LoginResult{Token: tok, Issuer: doc.Issuer}, nil
}

// classifyDeviceTokenError unwraps the oauth2.RetrieveError the
// package returns when the IdP refuses the token. We surface the
// device-flow-specific error codes (expired_token, access_denied)
// as Go-native sentinels so the cobra command can render them with
// operator-friendly hints rather than dumping the raw upstream
// payload.
func classifyDeviceTokenError(err error) error {
	var retr *oauth2.RetrieveError
	if errors.As(err, &retr) {
		switch retr.ErrorCode {
		case "expired_token":
			return fmt.Errorf("meho: device code expired before authorisation; rerun `meho login`")
		case "access_denied":
			return fmt.Errorf("meho: authorisation denied by the operator at the verification URI")
		}
	}
	return fmt.Errorf("meho: token exchange failed: %w", err)
}

// ConvertOAuthToken collapses an oauth2.Token into the StoredToken
// shape the persistence layer expects. Kept in its own function so
// the field mapping is in one place — adding (or renaming) a
// persisted field happens here and the call sites stay short.
func ConvertOAuthToken(tok *oauth2.Token, backplaneURL, issuer, clientID string) StoredToken {
	st := StoredToken{
		BackplaneURL: backplaneURL,
		Issuer:       issuer,
		ClientID:     clientID,
		AccessToken:  tok.AccessToken,
		RefreshToken: tok.RefreshToken,
		TokenType:    tok.TokenType,
		Expiry:       tok.Expiry,
	}
	// id_token rides in the oauth2.Token Extra() map — it's not a
	// first-class field because the OAuth 2.0 spec doesn't define it
	// (it's an OIDC extension). Lift it manually.
	if raw := tok.Extra("id_token"); raw != nil {
		if s, ok := raw.(string); ok {
			st.IDToken = s
		}
	}
	return st
}

// keyForBackplane is the (service, user) addressing the store uses
// for a given backplane URL. Centralised here so login and (future)
// status agree on the layout without copy-pasting the string format.
// Service stays at DefaultService; user is the backplane URL so
// multi-backplane support is a free upgrade once the rest of the CLI
// catches up.
func KeyForBackplane(backplaneURL string) (service, user string) {
	// Normalise the URL minimally — drop trailing slash so
	// "https://meho.example/" and "https://meho.example" collapse
	// to the same key. Anything more aggressive (case-folding the
	// host, stripping default ports) belongs in a v0.2
	// canonicalisation pass; keep it small for now.
	u, err := url.Parse(backplaneURL)
	if err != nil {
		// Unparseable URL: just trim slashes so we still get a
		// deterministic key. The caller will hit a real failure on
		// the actual HTTP request anyway.
		return DefaultService, strings.TrimRight(backplaneURL, "/")
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return DefaultService, u.String()
}

// PollTimeout is the default upper bound on the whole device-code
// dance — initiation plus polling plus token issuance. RFC 8628
// suggests 5 minutes as a typical expires_in; we double it so a
// distracted operator who walks away from their terminal still has
// a chance to come back and complete the flow. The cobra command
// wraps the context with this timeout so the bound is enforced
// regardless of what the IdP advertises in expires_in.
const PollTimeout = 10 * time.Minute
