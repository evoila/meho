// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"

	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/auth"
)

// errNoRefreshToken signals that the persisted token didn't carry
// a refresh_token, so the lazy 401-retry path can't recover. The
// cobra command surfaces this as output.AuthExpired (the operator
// must rerun `meho login`).
var errNoRefreshToken = errors.New("meho: no refresh_token persisted; rerun `meho login`")

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
	cfg := oauth2.Config{
		ClientID: b.current.ClientID,
		Endpoint: oauth2.Endpoint{TokenURL: doc.TokenEndpoint},
		Scopes:   []string{"openid"},
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

// fetchDiscovery is the production bridge from refreshDiscoverer
// onto auth.FetchDiscoveryFromRealm. Lifted into a named function
// so the tokenBox's struct field stays untyped against the auth
// package's full signature (which keeps the file's import surface
// to just what it uses).
func fetchDiscovery(ctx context.Context, httpClient *http.Client, issuerURL string) (*auth.DiscoveryDocument, error) {
	return auth.FetchDiscoveryFromRealm(ctx, httpClient, issuerURL)
}
