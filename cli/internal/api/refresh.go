// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"

	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/auth"
)

// errNoRefreshToken signals that the persisted token didn't carry
// a refresh_token, so the lazy 401-retry path can't recover. The
// cobra command surfaces this as output.AuthExpired (the operator
// must rerun `meho login`).
var errNoRefreshToken = errors.New("meho: no refresh_token persisted; rerun `meho login`")

// errRefreshRejected signals that a refresh_token *was* present but the
// IdP rejected the exchange as invalid_grant (the refresh token is
// expired, was consumed, or the session was ended) — Keycloak reports
// this with error_description "Token is not active". Distinct from
// errNoRefreshToken so the cobra command can tell "your session
// expired, log in again" apart from "there was no session to refresh"
// (#3320).
var errRefreshRejected = errors.New("meho: refresh token rejected by identity provider; session expired, rerun `meho login`")

// tokenBox holds the current access bearer plus enough state for a
// best-effort 401-retry refresh. Encapsulated in a struct so the
// editor function closes over a stable handle (the underlying
// *oauth2.Token swaps after a refresh) and so concurrent
// invocations of a meho subcommand — though v0.1 has none — would
// share one mutex rather than racing.
type tokenBox struct {
	mu sync.Mutex

	// current is the bearer attached to every outbound request via
	// the editor. After a successful refresh, current is replaced
	// in-place.
	current auth.StoredToken

	// httpClient drives the refresh exchange. Same transport as
	// the application's outbound calls — httptest.Server's client
	// in tests, default in production.
	httpClient *http.Client

	// refreshDiscoverer fetches the IdP's token endpoint URL when
	// a refresh is needed. Injectable so tests don't have to spin
	// up a real .well-known endpoint. Production code passes the
	// auth.FetchDiscoveryFromRealm bridge below.
	refreshDiscoverer func(ctx context.Context, httpClient *http.Client, issuerURL string) (*auth.DiscoveryDocument, error)

	// onRefresh is invoked with the post-refresh token. Best-effort;
	// errors swallow because the in-flight request already has the
	// new bearer in its editor.
	onRefresh func(updated auth.StoredToken)
}

// snapshot returns the current bearer string under the mutex. The
// returned value is safe to embed into an http.Header without
// further locking — strings are immutable.
func (b *tokenBox) snapshot() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.current.AccessToken
}

// refresh performs a one-shot refresh exchange against the IdP.
// Returns errNoRefreshToken when the stored token didn't carry a
// refresh_token. Any other refresh failure (IdP rejected, network
// error, no token_endpoint advertised) propagates verbatim.
//
// The refreshed token replaces b.current and onRefresh fires
// before this method returns. Concurrent refresh attempts on the
// same tokenBox serialize on b.mu so we never round-trip the IdP
// twice for one stale access token.
func (b *tokenBox) refresh(ctx context.Context) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.current.RefreshToken == "" {
		return errNoRefreshToken
	}

	doc, err := b.refreshDiscoverer(ctx, b.httpClient, b.current.Issuer)
	if err != nil {
		return fmt.Errorf("meho: refresh discovery: %w", err)
	}
	// No Scopes on the refresh: golang.org/x/oauth2's refresh_token
	// grant (tokenRefresher.Token) sends only grant_type + refresh_token
	// and never forwards Config.Scopes, so an explicit scope here would
	// be inert. Omitting scope is also the correct behaviour — per RFC
	// 6749 §6 an omitted scope preserves the originally-granted scope,
	// which is exactly what keeps a `meho login --offline` token offline
	// across rotations. Narrowing the scope on refresh (e.g. back to
	// ["openid"]) is what Keycloak rejects with invalid_scope for
	// offline sessions (#2902).
	cfg := oauth2.Config{
		ClientID: b.current.ClientID,
		Endpoint: oauth2.Endpoint{TokenURL: doc.TokenEndpoint},
	}

	// Push httpClient into the oauth2 ctx so the refresh POST uses
	// the same transport as everything else this CLI run does.
	flowCtx := context.WithValue(ctx, oauth2.HTTPClient, b.httpClient)
	stale := &oauth2.Token{
		AccessToken:  b.current.AccessToken,
		RefreshToken: b.current.RefreshToken,
		TokenType:    b.current.TokenType,
		Expiry:       b.current.Expiry,
	}
	src := cfg.TokenSource(flowCtx, stale)
	fresh, err := src.Token()
	if err != nil {
		if isInvalidGrant(err) {
			// The refresh token itself is dead (expired / consumed /
			// session ended). Wrap the sentinel so the command layer
			// surfaces "session expired, rerun `meho login`" rather
			// than a generic transport failure (#3320). The in-memory
			// and on-disk tokens are left untouched — we never delete
			// on a failed refresh.
			return fmt.Errorf("%w: %v", errRefreshRejected, err)
		}
		return fmt.Errorf("meho: refresh exchange: %w", err)
	}

	// Update the in-memory copy first so a subsequent editor call
	// picks the new bearer; onRefresh writes to the store after, on
	// a best-effort basis (we never roll back the in-memory swap
	// because the IdP already burnt the old refresh_token).
	updated := b.current
	updated.AccessToken = fresh.AccessToken
	if fresh.RefreshToken != "" {
		updated.RefreshToken = fresh.RefreshToken
	}
	if fresh.TokenType != "" {
		updated.TokenType = fresh.TokenType
	}
	updated.Expiry = fresh.Expiry
	if raw := fresh.Extra("id_token"); raw != nil {
		if s, ok := raw.(string); ok && s != "" {
			updated.IDToken = s
		}
	}
	b.current = updated
	if b.onRefresh != nil {
		b.onRefresh(updated)
	}
	return nil
}

// authorizationHeader is the canonical bearer-header value the
// editor stamps onto every outbound request. Lifted into a helper
// so the format is in one place (matters once the spec ever adds a
// non-Bearer auth scheme).
func authorizationHeader(accessToken string) string {
	if accessToken == "" {
		return ""
	}
	return "Bearer " + accessToken
}

// isInvalidGrant reports whether a failed refresh exchange was
// rejected by the IdP as invalid_grant — the RFC 6749 §5.2 error for
// an expired, revoked, or already-consumed refresh token. golang.org/
// x/oauth2 surfaces a token-endpoint error as *oauth2.RetrieveError,
// whose ErrorCode carries RFC 6749's `error` parameter. Keycloak
// returns error_description "Token is not active" for a dead offline/
// refresh session, so we also match that description as a belt-and-
// braces check for providers that don't set a clean ErrorCode. Network
// / discovery failures don't produce a RetrieveError, so they fall
// through as ordinary (retryable-looking) errors (#3320).
func isInvalidGrant(err error) bool {
	var re *oauth2.RetrieveError
	if !errors.As(err, &re) {
		return false
	}
	if strings.EqualFold(re.ErrorCode, "invalid_grant") {
		return true
	}
	if strings.Contains(strings.ToLower(re.ErrorDescription), "token is not active") {
		return true
	}
	// Older/newer oauth2 releases may leave ErrorCode/ErrorDescription
	// unparsed; fall back to the raw response body.
	return strings.Contains(strings.ToLower(string(re.Body)), "token is not active")
}

// fetchDiscovery is the production bridge from refreshDiscoverer
// onto auth.FetchDiscoveryFromRealm. Lifted into a named function
// so the tokenBox's struct field stays untyped against the auth
// package's full signature (which keeps the file's import surface
// to just what it uses).
func fetchDiscovery(ctx context.Context, httpClient *http.Client, issuerURL string) (*auth.DiscoveryDocument, error) {
	return auth.FetchDiscoveryFromRealm(ctx, httpClient, issuerURL)
}
