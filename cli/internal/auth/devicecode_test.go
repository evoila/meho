// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/oauth2"
)

// TestFetchDiscoveryOIDCShape exercises the happy path against a
// minimal Keycloak-shaped discovery document. The fields we depend
// on (issuer, device_authorization_endpoint, token_endpoint) are
// the only ones we assert against — the loader explicitly ignores
// extras per the spec.
func TestFetchDiscoveryOIDCShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/.well-known/openid-configuration" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"issuer":                        "https://kc.example/realms/meho",
			"device_authorization_endpoint": "https://kc.example/realms/meho/protocol/openid-connect/auth/device",
			"token_endpoint":                "https://kc.example/realms/meho/protocol/openid-connect/token",
			"unrelated_field":               "ignored",
		})
	}))
	defer srv.Close()

	doc, err := FetchDiscovery(context.Background(), srv.Client(), srv.URL+"/.well-known/openid-configuration")
	if err != nil {
		t.Fatalf("FetchDiscovery: %v", err)
	}
	if doc.Issuer != "https://kc.example/realms/meho" {
		t.Errorf("issuer: %q", doc.Issuer)
	}
	if !strings.HasSuffix(doc.DeviceAuthorizationEndpoint, "/auth/device") {
		t.Errorf("device_authorization_endpoint: %q", doc.DeviceAuthorizationEndpoint)
	}
	if !strings.HasSuffix(doc.TokenEndpoint, "/token") {
		t.Errorf("token_endpoint: %q", doc.TokenEndpoint)
	}
}

// TestFetchDiscoveryMissingFieldsErrors guarantees we don't silently
// accept a discovery document that's missing the device flow's
// load-bearing endpoints. The flow would fail later anyway, but
// failing here gives the operator a precise diagnostic.
func TestFetchDiscoveryMissingFieldsErrors(t *testing.T) {
	cases := map[string]map[string]any{
		"no device_authorization_endpoint": {
			"issuer":         "https://kc/realms/x",
			"token_endpoint": "https://kc/realms/x/token",
		},
		"no token_endpoint": {
			"issuer":                        "https://kc/realms/x",
			"device_authorization_endpoint": "https://kc/realms/x/device",
		},
	}
	for name, payload := range cases {
		t.Run(name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(payload)
			}))
			defer srv.Close()
			_, err := FetchDiscovery(context.Background(), srv.Client(), srv.URL+"/.well-known/openid-configuration")
			if err == nil {
				t.Fatalf("expected error, got nil")
			}
		})
	}
}

// TestFetchDiscoveryNon2xxSurfacesStatus lets operators see the
// upstream status code when discovery 4xx/5xx's. The body excerpt
// is included so a 403 with a Keycloak error envelope renders
// usefully.
func TestFetchDiscoveryNon2xxSurfacesStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"error":"realm-not-found"}`))
	}))
	defer srv.Close()

	_, err := FetchDiscovery(context.Background(), srv.Client(), srv.URL+"/.well-known/openid-configuration")
	if err == nil {
		t.Fatalf("expected error")
	}
	var derr *DiscoveryError
	if !errors.As(err, &derr) {
		t.Fatalf("expected *DiscoveryError, got %T", err)
	}
	if derr.Status != http.StatusForbidden {
		t.Errorf("status: got %d, want 403", derr.Status)
	}
	if !strings.Contains(derr.Body, "realm-not-found") {
		t.Errorf("body excerpt missing payload: %q", derr.Body)
	}
}

// TestFetchDiscoveryFromRealmTriesOAuthFallback verifies that an
// IdP that doesn't expose the OIDC well-known but does serve the
// RFC 8414 OAuth metadata still resolves. The first URL 404s, the
// second returns the document.
func TestFetchDiscoveryFromRealmTriesOAuthFallback(t *testing.T) {
	var oauthHits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/realms/meho/.well-known/openid-configuration":
			http.NotFound(w, r)
		case "/realms/meho/.well-known/oauth-authorization-server":
			atomic.AddInt32(&oauthHits, 1)
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"issuer":                        "https://kc/realms/meho",
				"device_authorization_endpoint": "https://kc/device",
				"token_endpoint":                "https://kc/token",
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	doc, err := FetchDiscoveryFromRealm(context.Background(), srv.Client(), srv.URL+"/realms/meho")
	if err != nil {
		t.Fatalf("FetchDiscoveryFromRealm: %v", err)
	}
	if doc.Issuer != "https://kc/realms/meho" {
		t.Errorf("issuer: %q", doc.Issuer)
	}
	if atomic.LoadInt32(&oauthHits) != 1 {
		t.Errorf("OAuth fallback URL not hit")
	}
}

// TestRunDeviceFlowHappyPath drives the full device-code dance
// against an httptest fake IdP. The fake echoes the spec:
//   - device-auth POST returns a device_code + user_code + intervals
//   - first token poll returns authorization_pending
//   - second token poll returns the access token
//
// The test pins the prompter contract (called exactly once, gets
// the upstream user_code), the polling contract (loop continues
// past authorization_pending), and the token mapping (ConvertOAuthToken
// pulls AccessToken / RefreshToken / IDToken correctly).
func TestRunDeviceFlowHappyPath(t *testing.T) {
	var (
		tokenCalls atomic.Int32
		deviceCode = "dev-code-fixture"
	)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/auth/device":
			if err := r.ParseForm(); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if got := r.PostForm.Get("client_id"); got != "meho-cli" {
				http.Error(w, "wrong client_id "+got, http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"device_code":      deviceCode,
				"user_code":        "ABCD-EFGH",
				"verification_uri": "https://kc.example/device",
				"expires_in":       600,
				// Short interval keeps the test fast — RFC 8628 §3.5
				// permits any non-negative value and the oauth2
				// package's DeviceAccessToken honours it as the
				// minimum poll period.
				"interval": 1,
			})
		case "/token":
			if err := r.ParseForm(); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if r.PostForm.Get("device_code") != deviceCode {
				http.Error(w, "wrong device_code", http.StatusBadRequest)
				return
			}
			n := tokenCalls.Add(1)
			if n == 1 {
				// First poll: still pending.
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(map[string]string{
					"error": "authorization_pending",
				})
				return
			}
			// Second poll: success — issue the token.
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token":  "at-from-fake-idp",
				"refresh_token": "rt-from-fake-idp",
				"id_token":      "idt-from-fake-idp",
				"token_type":    "Bearer",
				"expires_in":    3600,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	doc := &DiscoveryDocument{
		Issuer:                      "https://kc.example/realms/meho",
		DeviceAuthorizationEndpoint: srv.URL + "/auth/device",
		TokenEndpoint:               srv.URL + "/token",
	}

	var promptCalled int
	var capturedCode string
	prompter := func(_ context.Context, resp *oauth2.DeviceAuthResponse) error {
		promptCalled++
		capturedCode = resp.UserCode
		return nil
	}

	// Tight context so a regression that loops forever fails fast.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	result, err := RunDeviceFlow(ctx, doc, "meho-cli", DeviceFlowOptions{
		HTTPClient: srv.Client(),
		Prompter:   prompter,
	})
	if err != nil {
		t.Fatalf("RunDeviceFlow: %v", err)
	}
	if promptCalled != 1 {
		t.Errorf("prompter called %d times, want 1", promptCalled)
	}
	if capturedCode != "ABCD-EFGH" {
		t.Errorf("user code captured wrong: %q", capturedCode)
	}
	if result.Token.AccessToken != "at-from-fake-idp" {
		t.Errorf("access token: %q", result.Token.AccessToken)
	}
	if result.Token.RefreshToken != "rt-from-fake-idp" {
		t.Errorf("refresh token: %q", result.Token.RefreshToken)
	}
	// id_token lives in oauth2.Token.Extra, not a top-level field.
	if got := result.Token.Extra("id_token"); got != "idt-from-fake-idp" {
		t.Errorf("id_token extra: got %v, want idt-from-fake-idp", got)
	}
	if tokenCalls.Load() < 2 {
		t.Errorf("token endpoint called %d times, expected at least 2 (pending then success)", tokenCalls.Load())
	}

	// Convert to StoredToken and verify the mapping. This is the
	// boundary between oauth2's surface and our persisted shape.
	stored := ConvertOAuthToken(result.Token, "https://meho.example", result.Issuer, "meho-cli")
	if stored.AccessToken != "at-from-fake-idp" || stored.IDToken != "idt-from-fake-idp" {
		t.Errorf("ConvertOAuthToken dropped fields: %+v", stored)
	}
}

// TestRunDeviceFlowDeniedSurfacesFriendlyError checks the access_denied
// classification: the operator clicked "deny" at the verification UI
// and we want a clean message, not a raw oauth2.RetrieveError.
func TestRunDeviceFlowDeniedSurfacesFriendlyError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/auth/device":
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"device_code":      "dc",
				"user_code":        "UC",
				"verification_uri": "https://kc/device",
				"expires_in":       600,
				"interval":         1,
			})
		case "/token":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": "access_denied"})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	doc := &DiscoveryDocument{
		Issuer:                      "iss",
		DeviceAuthorizationEndpoint: srv.URL + "/auth/device",
		TokenEndpoint:               srv.URL + "/token",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := RunDeviceFlow(ctx, doc, "meho-cli", DeviceFlowOptions{
		HTTPClient: srv.Client(),
		Prompter:   func(context.Context, *oauth2.DeviceAuthResponse) error { return nil },
	})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(err.Error(), "denied") {
		t.Errorf("error should mention 'denied', got %q", err.Error())
	}
}

// TestRunDeviceFlowPrompterErrorAborts ensures the flow stops if the
// prompter signals failure (e.g. operator hit Ctrl-C while we're
// rendering the prompt). The token endpoint must not be polled.
func TestRunDeviceFlowPrompterErrorAborts(t *testing.T) {
	var tokenCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/auth/device":
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"device_code":      "dc",
				"user_code":        "UC",
				"verification_uri": "https://kc/device",
				"expires_in":       600,
				"interval":         1,
			})
		case "/token":
			tokenCalls.Add(1)
			w.WriteHeader(http.StatusOK)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	doc := &DiscoveryDocument{
		DeviceAuthorizationEndpoint: srv.URL + "/auth/device",
		TokenEndpoint:               srv.URL + "/token",
	}
	wantErr := errors.New("operator cancelled")
	_, err := RunDeviceFlow(context.Background(), doc, "meho-cli", DeviceFlowOptions{
		HTTPClient: srv.Client(),
		Prompter:   func(context.Context, *oauth2.DeviceAuthResponse) error { return wantErr },
	})
	if !errors.Is(err, wantErr) {
		t.Errorf("prompter error not propagated: got %v", err)
	}
	if tokenCalls.Load() != 0 {
		t.Errorf("token endpoint should not be hit when prompter errors; saw %d calls", tokenCalls.Load())
	}
}

// TestRunDeviceFlowRequiresPrompter is the precondition check —
// a caller that forgets to set Prompter should fail loudly rather
// than silently completing the flow and losing the user_code line.
func TestRunDeviceFlowRequiresPrompter(t *testing.T) {
	doc := &DiscoveryDocument{
		DeviceAuthorizationEndpoint: "https://x",
		TokenEndpoint:               "https://y",
	}
	_, err := RunDeviceFlow(context.Background(), doc, "c", DeviceFlowOptions{})
	if err == nil {
		t.Fatalf("expected error for nil prompter")
	}
}

// TestDiscoveryURLsOrder pins the well-known precedence: OIDC first
// because that's the more common shape, OAuth metadata second as
// the spec-pure fallback.
func TestDiscoveryURLsOrder(t *testing.T) {
	got := DiscoveryURLs("https://kc/realms/meho/")
	if len(got) != 2 {
		t.Fatalf("expected 2 URLs, got %d", len(got))
	}
	if !strings.HasSuffix(got[0], "/.well-known/openid-configuration") {
		t.Errorf("first URL should be OIDC: %q", got[0])
	}
	if !strings.HasSuffix(got[1], "/.well-known/oauth-authorization-server") {
		t.Errorf("second URL should be OAuth metadata: %q", got[1])
	}
	// Trailing slash should not appear twice in the result.
	for _, u := range got {
		if _, err := url.Parse(u); err != nil {
			t.Errorf("invalid URL: %q (%v)", u, err)
		}
		if strings.Contains(u, "//.well-known") {
			t.Errorf("double slash in %q", u)
		}
	}
}

// TestConvertOAuthTokenHandlesMissingIDToken makes sure the
// id_token extraction tolerates non-OIDC IdPs that omit it; the
// access token still has to round-trip.
func TestConvertOAuthTokenHandlesMissingIDToken(t *testing.T) {
	tok := &oauth2.Token{
		AccessToken:  "at",
		RefreshToken: "rt",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(time.Hour),
	}
	stored := ConvertOAuthToken(tok, "https://x", "iss", "client")
	if stored.AccessToken != "at" {
		t.Errorf("access token: %q", stored.AccessToken)
	}
	if stored.IDToken != "" {
		t.Errorf("id_token should be empty when absent, got %q", stored.IDToken)
	}
}

// TestDiscoveryErrorUnwraps validates errors.Is plumbing — callers
// (the cobra command, future logging middleware) match against
// underlying sentinels like context.DeadlineExceeded.
func TestDiscoveryErrorUnwraps(t *testing.T) {
	root := fmt.Errorf("ctx err: %w", context.DeadlineExceeded)
	de := &DiscoveryError{URL: "https://x", Underlying: root}
	if !errors.Is(de, context.DeadlineExceeded) {
		t.Errorf("DiscoveryError should unwrap to context.DeadlineExceeded")
	}
}

// TestNewDeviceFlowContextDropsParentDeadline is the regression pin
// for Initiative G0.9.1, Wall #4. A parent context with a deadline
// shorter than the device-flow approval window must not propagate
// that deadline to the polling wait. Inheriting context *values*
// stays — `oauth2.HTTPClient` injection at higher layers depends on
// it.
func TestNewDeviceFlowContextDropsParentDeadline(t *testing.T) {
	type ctxKey struct{}

	// Parent: short deadline + a stuffed value.
	parent, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	parent = context.WithValue(parent, ctxKey{}, "carried-through")

	flowCtx, cancelFlow := NewDeviceFlowContext(parent)
	defer cancelFlow()

	// The parent's deadline must not appear on the flow context (we
	// installed PollTimeout = 10m, not 10ms). Use Deadline() rather
	// than waiting and observing Done() so the test stays fast.
	dl, ok := flowCtx.Deadline()
	if !ok {
		t.Fatalf("flow context should have a deadline (PollTimeout cap), got none")
	}
	if remaining := time.Until(dl); remaining < PollTimeout-time.Minute {
		t.Errorf("flow context deadline too close (%v); parent's short deadline leaked", remaining)
	}

	// Wait past the parent's deadline; the flow context must remain
	// open. The parent will be Done; the flow context must not be.
	time.Sleep(40 * time.Millisecond)
	if parent.Err() == nil {
		t.Fatalf("parent context should already be expired by now")
	}
	if err := flowCtx.Err(); err != nil {
		t.Errorf("flow context should still be alive after parent expires; got err=%v", err)
	}

	// Values must still ride through.
	if got := flowCtx.Value(ctxKey{}); got != "carried-through" {
		t.Errorf("value not inherited: got %v", got)
	}
}

// TestNewDeviceFlowContextCancelStopsSignalRelay ensures the cancel
// func returned by NewDeviceFlowContext releases both inner
// resources — the PollTimeout timer and the signal.Notify
// registration — so spawning a hundred logins doesn't leak a
// hundred SIGINT handlers. We can't directly observe signal-relay
// teardown without reaching into runtime internals, so we settle
// for confirming the context becomes Done after cancel and that
// the cancel func is safe to call twice.
func TestNewDeviceFlowContextCancelStopsSignalRelay(t *testing.T) {
	flowCtx, cancel := NewDeviceFlowContext(context.Background())
	cancel()
	cancel() // idempotent — double-defer in callers must not panic.

	select {
	case <-flowCtx.Done():
		// Expected.
	case <-time.After(time.Second):
		t.Fatalf("flow context should be Done after cancel")
	}
}

// TestRunDeviceFlowSurvivesShortParentDeadline is the integration-
// shaped pin: drive RunDeviceFlow with a parent context whose
// deadline elapses while the stub IdP is still returning
// `authorization_pending`. Before the fix, this scenario produced
// `context deadline exceeded`. After the fix, the polling wait
// continues past the parent's deadline and the flow completes with
// the IdP-issued token.
func TestRunDeviceFlowSurvivesShortParentDeadline(t *testing.T) {
	const parentBudget = 50 * time.Millisecond

	var tokenCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/auth/device":
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"device_code":      "dc",
				"user_code":        "UC",
				"verification_uri": "https://kc/device",
				"expires_in":       600,
				"interval":         1,
			})
		case "/token":
			n := tokenCalls.Add(1)
			// Stay pending until the parent budget has elapsed, then
			// approve. This pins the exact scenario reported in
			// G0.9.1 Wall #4: the IdP would have approved, but the
			// CLI had already given up because the ambient deadline
			// fired first.
			if n < 3 {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(map[string]string{
					"error": "authorization_pending",
				})
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token": "at",
				"token_type":   "Bearer",
				"expires_in":   3600,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	doc := &DiscoveryDocument{
		Issuer:                      "iss",
		DeviceAuthorizationEndpoint: srv.URL + "/auth/device",
		TokenEndpoint:               srv.URL + "/token",
	}

	parent, cancelParent := context.WithTimeout(context.Background(), parentBudget)
	defer cancelParent()

	flowCtx, cancelFlow := NewDeviceFlowContext(parent)
	defer cancelFlow()

	// Sanity check: by the time the polling loop logs the second
	// pending response (~2s into a 1s-interval poll), the parent
	// deadline is long gone. The detached flow context must still
	// be open.
	result, err := RunDeviceFlow(flowCtx, doc, "meho-cli", DeviceFlowOptions{
		HTTPClient:    srv.Client(),
		Prompter:      func(context.Context, *oauth2.DeviceAuthResponse) error { return nil },
		ParentContext: parent,
	})
	if err != nil {
		t.Fatalf("RunDeviceFlow failed even though IdP approved after parent deadline: %v", err)
	}
	if result.Token.AccessToken != "at" {
		t.Errorf("token: got %q, want at", result.Token.AccessToken)
	}
	if parent.Err() == nil {
		t.Fatalf("test invariant violated: parent should have expired by now (budget %v)", parentBudget)
	}
}

// TestClassifyDeviceTokenErrorNamesAmbientDeadline verifies the
// error-message disambiguation for the case where the polling
// timeout fires *and* the parent context is also past deadline.
// Operators wrapped under a CI step or bash-tool timeout need to be
// told the wrapper is the problem, not the IdP — see G0.9.1 Wall
// #4 acceptance criteria.
func TestClassifyDeviceTokenErrorNamesAmbientDeadline(t *testing.T) {
	expiredParent, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()

	// Parent must be observably expired before we classify.
	if expiredParent.Err() == nil {
		t.Fatalf("test setup: parent should be expired")
	}

	err := classifyDeviceTokenError(expiredParent, context.DeadlineExceeded)
	if err == nil {
		t.Fatalf("expected an error")
	}
	msg := err.Error()
	for _, want := range []string{"parent process", "deadline", "wrapping timeout"} {
		if !strings.Contains(msg, want) {
			t.Errorf("message should mention %q, got %q", want, msg)
		}
	}
}

// TestClassifyDeviceTokenErrorDistinguishesCancel pins the cancel
// branch separately from the deadline branch so SIGINT / explicit
// cancel still produces a clean message rather than the raw
// `context canceled`. Acceptance criterion: "Genuine cancellation
// (SIGINT / explicit cancel) still aborts promptly".
func TestClassifyDeviceTokenErrorDistinguishesCancel(t *testing.T) {
	err := classifyDeviceTokenError(context.Background(), context.Canceled)
	if err == nil {
		t.Fatalf("expected an error")
	}
	if !strings.Contains(err.Error(), "cancelled") {
		t.Errorf("cancel message missing 'cancelled', got %q", err.Error())
	}
}

// TestClassifyDeviceTokenErrorOwnPollTimeout covers the case where
// our PollTimeout cap fires (10m elapsed) but the parent context
// is still healthy. The message should not blame the parent.
func TestClassifyDeviceTokenErrorOwnPollTimeout(t *testing.T) {
	err := classifyDeviceTokenError(context.Background(), context.DeadlineExceeded)
	if err == nil {
		t.Fatalf("expected an error")
	}
	msg := err.Error()
	if strings.Contains(msg, "parent process") {
		t.Errorf("should not blame parent when parent is healthy: %q", msg)
	}
	if !strings.Contains(msg, "approval") {
		t.Errorf("message should mention the approval wait: %q", msg)
	}
}

// TestClassifyDeviceTokenErrorIdPCodesUnchanged confirms the
// existing classifications for `expired_token` / `access_denied`
// still produce the same operator-facing strings — those branches
// pre-date this fix and tests / docs already pin them.
func TestClassifyDeviceTokenErrorIdPCodesUnchanged(t *testing.T) {
	expired := &oauth2.RetrieveError{ErrorCode: "expired_token"}
	denied := &oauth2.RetrieveError{ErrorCode: "access_denied"}

	if got := classifyDeviceTokenError(context.Background(), expired).Error(); !strings.Contains(got, "device code expired") {
		t.Errorf("expired_token message regressed: %q", got)
	}
	if got := classifyDeviceTokenError(context.Background(), denied).Error(); !strings.Contains(got, "denied") {
		t.Errorf("access_denied message regressed: %q", got)
	}
}
