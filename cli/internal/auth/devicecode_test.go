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
	"sync"
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
				// permits any non-negative value and pollDeviceToken
				// honours it as the minimum wait between checks.
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
			// RFC 8628 §3.4: a public client identifies itself in the
			// form body and sends no Authorization header. The fix
			// for #4892 hangs on this — the oauth2 package's
			// auto-detect sent a header first and the body second.
			if got := r.PostForm.Get("client_id"); got != "meho-cli" {
				http.Error(w, "wrong client_id in token request: "+got, http.StatusBadRequest)
				return
			}
			if got := r.Header.Get("Authorization"); got != "" {
				http.Error(w, "unexpected Authorization header: "+got, http.StatusBadRequest)
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
	// Exactly two: one request per check, pending then success. The
	// old "at least 2" let the doubled request of #4892 through.
	if tokenCalls.Load() != 2 {
		t.Errorf("token endpoint called %d times, expected exactly 2 (pending then success)", tokenCalls.Load())
	}

	// Convert to StoredToken and verify the mapping. This is the
	// boundary between oauth2's surface and our persisted shape.
	stored := ConvertOAuthToken(result.Token, "https://meho.example", result.Issuer, "meho-cli")
	if stored.AccessToken != "at-from-fake-idp" || stored.IDToken != "idt-from-fake-idp" {
		t.Errorf("ConvertOAuthToken dropped fields: %+v", stored)
	}
}

// pollingFakeIdP is a fake IdP for the polling tests. Every token
// request records its arrival time; answers come from the supplied
// script in order (an empty entry means "issue the token").
//
// Like Keycloak, the fake answers slow_down to a token request that
// arrives less than interval after the last accepted one, and counts
// it in tooSoon. Such a request does not use up a script entry.
type pollingFakeIdP struct {
	srv       *httptest.Server
	mu        sync.Mutex
	started   time.Time
	arrivals  []time.Time
	authHdrs  []string
	clientIDs []string
	accepted  time.Time
	tooSoon   int
	// approveAfter, when set, replaces the script: token requests that
	// arrive earlier than this after the device-auth answer are
	// pending, later ones get the token.
	approveAfter time.Duration
	// stall makes the token endpoint hold every request open until
	// the client gives up on it.
	stall bool
}

// newPollingFakeIdP starts a fake IdP whose device-auth answer carries
// expiresIn / interval and whose token endpoint follows script: each
// entry is the RFC 8628 §3.5 error code to answer with, and "" issues
// the token. Requests past the end of the script also get the token.
func newPollingFakeIdP(t *testing.T, expiresIn, interval int, script []string) *pollingFakeIdP {
	t.Helper()
	f := &pollingFakeIdP{}
	minGap := time.Duration(interval) * time.Second
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/auth/device":
			f.mu.Lock()
			f.started = time.Now()
			f.mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"device_code":      "dc",
				"user_code":        "ABCD-EFGH",
				"verification_uri": "https://kc.example/device",
				"expires_in":       expiresIn,
				"interval":         interval,
			})
		case "/token":
			_ = r.ParseForm()
			now := time.Now()
			f.mu.Lock()
			f.arrivals = append(f.arrivals, now)
			f.authHdrs = append(f.authHdrs, r.Header.Get("Authorization"))
			f.clientIDs = append(f.clientIDs, r.PostForm.Get("client_id"))
			if f.stall {
				f.mu.Unlock()
				<-r.Context().Done()
				return
			}
			answer := ""
			switch {
			case !f.accepted.IsZero() && now.Sub(f.accepted) < minGap:
				f.tooSoon++
				answer = "slow_down"
			case f.approveAfter > 0:
				f.accepted = now
				if now.Sub(f.started) < f.approveAfter {
					answer = "authorization_pending"
				}
			default:
				n := len(f.arrivals) - 1 - f.tooSoon
				f.accepted = now
				if n < len(script) {
					answer = script[n]
				}
			}
			f.mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			if answer != "" {
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(map[string]string{"error": answer})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token": "at",
				"token_type":   "Bearer",
				"expires_in":   3600,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

// setApproveAfter makes the fake approve the code d after the
// device-auth answer, like a person who approves late.
func (f *pollingFakeIdP) setApproveAfter(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.approveAfter = d
}

// setStall makes the token endpoint hold every request open.
func (f *pollingFakeIdP) setStall() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stall = true
}

func (f *pollingFakeIdP) doc() *DiscoveryDocument {
	return &DiscoveryDocument{
		Issuer:                      "iss",
		DeviceAuthorizationEndpoint: f.srv.URL + "/auth/device",
		TokenEndpoint:               f.srv.URL + "/token",
	}
}

// gaps returns the time between consecutive token requests.
func (f *pollingFakeIdP) gaps() []time.Duration {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []time.Duration
	for i := 1; i < len(f.arrivals); i++ {
		out = append(out, f.arrivals[i].Sub(f.arrivals[i-1]))
	}
	return out
}

func (f *pollingFakeIdP) requests() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.arrivals)
}

// rejectedTooSoon is how many token requests arrived less than
// interval after the last accepted one.
func (f *pollingFakeIdP) rejectedTooSoon() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.tooSoon
}

// setPollKnobs shortens the polling knobs for one test and restores
// them afterwards.
func setPollKnobs(t *testing.T, step, maxWait, margin time.Duration) {
	t.Helper()
	oldStep, oldMax, oldMargin := slowDownStep, maxPollInterval, lastCheckMargin
	slowDownStep, maxPollInterval, lastCheckMargin = step, maxWait, margin
	t.Cleanup(func() { slowDownStep, maxPollInterval, lastCheckMargin = oldStep, oldMax, oldMargin })
}

// gapTolerance is how much later than planned a request may arrive
// on a loaded test runner (-race, CI) before a timing assertion
// fails. Gaps are never allowed to be shorter than planned.
const gapTolerance = 750 * time.Millisecond

func assertGapAbout(t *testing.T, name string, got, want time.Duration) {
	t.Helper()
	if got < want-20*time.Millisecond || got > want+gapTolerance {
		t.Errorf("%s: gap %v, want about %v", name, got, want)
	}
}

// runFlow runs the device flow against the fake and fails the test
// when any check reached the fake sooner than its interval allows.
func runFlow(t *testing.T, f *pollingFakeIdP) (*LoginResult, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := RunDeviceFlow(ctx, f.doc(), "meho-cli", DeviceFlowOptions{
		HTTPClient: f.srv.Client(),
		Prompter:   func(context.Context, *oauth2.DeviceAuthResponse) error { return nil },
	})
	if n := f.rejectedTooSoon(); n != 0 {
		t.Errorf("%d token request(s) came sooner than the interval allows", n)
	}
	return result, err
}

// TestRunDeviceFlowOneRequestPerCheck pins the core of #4892: every
// check is exactly one token request, the client id travels in the
// form body, and no Authorization header is sent. The first wait is
// the server's interval.
func TestRunDeviceFlowOneRequestPerCheck(t *testing.T) {
	f := newPollingFakeIdP(t, 600, 1, []string{"authorization_pending", "authorization_pending", ""})
	start := time.Now()
	if _, err := runFlow(t, f); err != nil {
		t.Fatalf("RunDeviceFlow: %v", err)
	}
	if got := f.requests(); got != 3 {
		t.Fatalf("token requests: %d, want 3 (one per check)", got)
	}
	for i, h := range f.authHdrs {
		if h != "" {
			t.Errorf("request %d carried an Authorization header: %q", i+1, h)
		}
	}
	for i, id := range f.clientIDs {
		if id != "meho-cli" {
			t.Errorf("request %d client_id in body: %q, want meho-cli", i+1, id)
		}
	}
	// First check comes one interval after the start, not at once.
	f.mu.Lock()
	first := f.arrivals[0].Sub(start)
	f.mu.Unlock()
	assertGapAbout(t, "first wait", first, time.Second)
	for i, g := range f.gaps() {
		assertGapAbout(t, fmt.Sprintf("gap %d", i+1), g, time.Second)
	}
}

// TestRunDeviceFlowSlowDownAddsOneStep: after one slow_down the next
// wait is one step longer (RFC 8628 §3.5), the login still works, and
// the server sees exactly three requests.
func TestRunDeviceFlowSlowDownAddsOneStep(t *testing.T) {
	setPollKnobs(t, time.Second, 30*time.Second, 2*time.Second)
	f := newPollingFakeIdP(t, 600, 1, []string{"slow_down", "authorization_pending", ""})
	result, err := runFlow(t, f)
	if err != nil {
		t.Fatalf("RunDeviceFlow: %v", err)
	}
	if result.Token.AccessToken != "at" {
		t.Errorf("access token: %q", result.Token.AccessToken)
	}
	if got := f.requests(); got != 3 {
		t.Fatalf("token requests: %d, want 3", got)
	}
	gaps := f.gaps()
	// interval 1s + one step of 1s = 2s after slow_down; the wait
	// stays raised for all later checks.
	assertGapAbout(t, "wait after slow_down", gaps[0], 2*time.Second)
	assertGapAbout(t, "wait after pending", gaps[1], 2*time.Second)
}

// TestRunDeviceFlowSlowDownWaitIsCapped: a server that keeps
// answering slow_down cannot stretch the wait past the upper limit.
func TestRunDeviceFlowSlowDownWaitIsCapped(t *testing.T) {
	setPollKnobs(t, time.Second, 1500*time.Millisecond, 2*time.Second)
	f := newPollingFakeIdP(t, 600, 1, []string{"slow_down", "slow_down", "slow_down", ""})
	if _, err := runFlow(t, f); err != nil {
		t.Fatalf("RunDeviceFlow: %v", err)
	}
	if got := f.requests(); got != 4 {
		t.Fatalf("token requests: %d, want 4", got)
	}
	for i, g := range f.gaps() {
		// 1s + 1s would be 2s; the cap holds it at 1.5s.
		assertGapAbout(t, fmt.Sprintf("gap %d", i+1), g, 1500*time.Millisecond)
	}
}

// TestRunDeviceFlowLastCheckBeforeExpiry: with a code that lives 3s
// and an interval of 10s the regular cadence would never check. The
// loop checks once about 2s before the end and the approval is seen.
func TestRunDeviceFlowLastCheckBeforeExpiry(t *testing.T) {
	f := newPollingFakeIdP(t, 3, 10, []string{""})
	start := time.Now()
	result, err := runFlow(t, f)
	if err != nil {
		t.Fatalf("RunDeviceFlow: %v", err)
	}
	if result.Token.AccessToken != "at" {
		t.Errorf("access token: %q", result.Token.AccessToken)
	}
	if got := f.requests(); got != 1 {
		t.Fatalf("token requests: %d, want 1", got)
	}
	f.mu.Lock()
	at := f.arrivals[0].Sub(start)
	f.mu.Unlock()
	assertGapAbout(t, "last check", at, time.Second)
}

// TestRunDeviceFlowExpiresWithoutApproval: no approval until the end
// → the "device code expired" message, not the 10-minute timeout one.
func TestRunDeviceFlowExpiresWithoutApproval(t *testing.T) {
	setPollKnobs(t, 5*time.Second, 30*time.Second, 500*time.Millisecond)
	pending := []string{"authorization_pending", "authorization_pending", "authorization_pending", "authorization_pending"}
	f := newPollingFakeIdP(t, 3, 1, pending)
	start := time.Now()
	_, err := runFlow(t, f)
	if err == nil {
		t.Fatalf("expected an error")
	}
	if !strings.Contains(err.Error(), "device code expired") {
		t.Errorf("error should say the code expired, got %q", err.Error())
	}
	if strings.Contains(err.Error(), "timed out") {
		t.Errorf("error must not blame the 10-minute cap: %q", err.Error())
	}
	// A check at ~1s, then the last one at ~2.5s (a regular check at
	// ~2s would leave only 0.5s to the last one); the loop stops
	// before the 3s end instead of running into the cap.
	if got := f.requests(); got != 2 {
		t.Errorf("token requests: %d, want 2", got)
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second+gapTolerance {
		t.Errorf("loop ran %v, should stop at the code's end (3s)", elapsed)
	}
}

// TestRunDeviceFlowLastCheckKeepsTheGap: the last check is planned
// so that it keeps the full wait to the check before it. Here the code
// lives 5s, the interval is 1s and the last check is meant for 1.5s
// before the end: checks at ~1s and ~2s, then the last one at ~3.5s.
// Moving only the final check (~3s regular, ~3.5s last) would put two
// checks 0.5s apart; the fake, like Keycloak, answers slow_down to
// that, and the approval at 3.2s would be reported as "expired".
func TestRunDeviceFlowLastCheckKeepsTheGap(t *testing.T) {
	setPollKnobs(t, time.Second, 30*time.Second, 1500*time.Millisecond)
	f := newPollingFakeIdP(t, 5, 1, nil)
	f.setApproveAfter(3200 * time.Millisecond)
	result, err := runFlow(t, f)
	if err != nil {
		t.Fatalf("RunDeviceFlow: %v", err)
	}
	if result.Token.AccessToken != "at" {
		t.Errorf("access token: %q", result.Token.AccessToken)
	}
	if got := f.requests(); got != 3 {
		t.Fatalf("token requests: %d, want 3", got)
	}
	gaps := f.gaps()
	assertGapAbout(t, "regular gap", gaps[0], time.Second)
	assertGapAbout(t, "gap before the last check", gaps[1], 1500*time.Millisecond)
}

// TestRunDeviceFlowStalledAnswerEndsWithTheCode: an answer that never
// arrives cannot keep the login waiting past the code's end. The code
// lives 2s; the one check (at ~1.5s) stalls, and the loop stops at
// ~2s with the "device code expired" message, not the 10-minute one.
func TestRunDeviceFlowStalledAnswerEndsWithTheCode(t *testing.T) {
	setPollKnobs(t, 5*time.Second, 30*time.Second, 500*time.Millisecond)
	f := newPollingFakeIdP(t, 2, 1, nil)
	f.setStall()
	start := time.Now()
	_, err := runFlow(t, f)
	elapsed := time.Since(start)
	if err == nil {
		t.Fatalf("expected an error")
	}
	if !strings.Contains(err.Error(), "device code expired") {
		t.Errorf("error should say the code expired, got %q", err.Error())
	}
	if strings.Contains(err.Error(), "timed out") {
		t.Errorf("error must not blame the 10-minute cap: %q", err.Error())
	}
	if got := f.requests(); got != 1 {
		t.Errorf("token requests: %d, want 1", got)
	}
	if elapsed < 2*time.Second-20*time.Millisecond || elapsed > 2*time.Second+gapTolerance {
		t.Errorf("loop ran %v, should stop at the code's end (2s)", elapsed)
	}
}

// TestNextCheck pins the arithmetic of the last check.
func TestNextCheck(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		name     string
		wait     time.Duration
		first    bool
		expiry   time.Time
		want     time.Duration
		wantLast bool
		wantOK   bool
	}{
		{"no expiry keeps cadence", 5 * time.Second, false, time.Time{}, 5 * time.Second, false, true},
		{"far from the end", 5 * time.Second, false, now.Add(time.Minute), 5 * time.Second, false, true},
		{"another wait still fits", 5 * time.Second, false, now.Add(12 * time.Second), 5 * time.Second, false, true},
		{"another wait would not fit", 5 * time.Second, false, now.Add(11 * time.Second), 9 * time.Second, true, true},
		{"last check keeps the gap", 5 * time.Second, false, now.Add(6 * time.Second), 5 * time.Second, true, true},
		{"first check may run at the margin", 10 * time.Second, true, now.Add(3 * time.Second), time.Second, true, true},
		{"first check already inside the margin", 5 * time.Second, true, now.Add(time.Second), 0, true, true},
		{"no check fits before the end", 10 * time.Second, false, now.Add(3 * time.Second), 0, true, false},
		{"gap-keeping check would be at the end", 5 * time.Second, false, now.Add(5 * time.Second), 0, true, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, last, ok := nextCheck(now, tc.wait, tc.first, tc.expiry)
			if got != tc.want || last != tc.wantLast || ok != tc.wantOK {
				t.Errorf("got (%v, %v, %v), want (%v, %v, %v)", got, last, ok, tc.want, tc.wantLast, tc.wantOK)
			}
		})
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

func TestIsInvalidScope(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"error code", &oauth2.RetrieveError{ErrorCode: "invalid_scope"}, true},
		{"body only", fmt.Errorf("wrapped: %w", &oauth2.RetrieveError{Body: []byte(`{"error":"invalid_scope"}`)}), true},
		{"other code", &oauth2.RetrieveError{ErrorCode: "invalid_client"}, false},
		{"other body", &oauth2.RetrieveError{Body: []byte(`{"error":"invalid_request"}`)}, false},
		{"not json", &oauth2.RetrieveError{Body: []byte("<html>")}, false},
		{"plain error", errors.New("invalid_scope"), false},
		{"nil", nil, false},
	}
	for _, c := range cases {
		if got := IsInvalidScope(c.err); got != c.want {
			t.Errorf("%s: IsInvalidScope = %v, want %v", c.name, got, c.want)
		}
	}
}
