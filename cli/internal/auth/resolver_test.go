// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"golang.org/x/oauth2"
)

// TestParseResolveEntries covers the happy path plus the IPv6 and
// bracketed-IPv6 acceptance the format promises.
func TestParseResolveEntries(t *testing.T) {
	cases := []struct {
		name    string
		in      []string
		wantKey string
		wantIP  string
	}{
		{"ipv4", []string{"kc.example.com:443:10.0.0.5"}, "kc.example.com:443", "10.0.0.5"},
		{"ipv6-bare", []string{"kc.example.com:443:::1"}, "kc.example.com:443", "::1"},
		{"ipv6-bracketed", []string{"kc.example.com:443:[2001:db8::1]"}, "kc.example.com:443", "2001:db8::1"},
		{"numeric-port-low", []string{"kc.example.com:1:10.0.0.5"}, "kc.example.com:1", "10.0.0.5"},
		{"numeric-port-high", []string{"kc.example.com:65535:10.0.0.5"}, "kc.example.com:65535", "10.0.0.5"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseResolveEntries(tc.in)
			if err != nil {
				t.Fatalf("ParseResolveEntries(%q): %v", tc.in, err)
			}
			ip, ok := got[tc.wantKey]
			if !ok {
				t.Fatalf("no override for %q; got map %v", tc.wantKey, got)
			}
			if ip != tc.wantIP {
				t.Errorf("ip = %q, want %q", ip, tc.wantIP)
			}
		})
	}
}

// TestParseResolveEntriesEmpty returns a nil map (not an error) so the
// no-flag path costs nothing.
func TestParseResolveEntriesEmpty(t *testing.T) {
	got, err := ParseResolveEntries(nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != nil {
		t.Errorf("expected nil map for empty input, got %v", got)
	}
}

// TestParseResolveEntriesRejectsMalformed pins the fail-loud contract: a
// mistyped override must error, not silently fall back to the broken
// system resolver.
func TestParseResolveEntriesRejectsMalformed(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"no-colons", "kc.example.com", "host:port:ip"},
		{"only-one-colon", "kc.example.com:443", "host:port:ip"},
		{"bad-port", "kc.example.com:notaport:10.0.0.5", "numeric TCP port"},
		// A named service would pass net.LookupPort but key the override
		// map as "host:https", which never matches the numeric dial
		// address — the pin would be silently ignored. It must be
		// rejected loudly at parse time instead.
		{"named-port", "kc.example.com:https:10.0.0.5", "not a numeric TCP port"},
		{"port-zero", "kc.example.com:0:10.0.0.5", "range 1-65535"},
		{"port-out-of-range", "kc.example.com:65536:10.0.0.5", "range 1-65535"},
		{"negative-port", "kc.example.com:-1:10.0.0.5", "numeric TCP port"},
		// Atoi accepts a leading sign / leading zeros, but the dial address
		// never carries either spelling — such an entry would be inert.
		{"signed-port", "kc.example.com:+443:10.0.0.5", "numeric TCP port"},
		{"leading-zero-port", "kc.example.com:0443:10.0.0.5", "numeric TCP port"},
		// The format is front-split (host = everything before the first
		// colon), so an IPv6 literal cannot appear in the host position;
		// it gets an explicit error, not a confusing port/IP one.
		{"ipv6-literal-host", "[::1]:443:10.0.0.5", "IPv6-literal hosts are not supported"},
		{"bad-ip", "kc.example.com:443:not-an-ip", "valid IP"},
		{"empty-host", ":443:10.0.0.5", "host is empty"},
		{"empty-ip", "kc.example.com:443:", "ip is empty"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseResolveEntries([]string{tc.in})
			if err == nil {
				t.Fatalf("expected error for %q", tc.in)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error %q should mention %q", err, tc.want)
			}
			// The offending entry must be echoed so the operator knows
			// which --resolve value to fix.
			if !strings.Contains(err.Error(), tc.in) {
				t.Errorf("error %q should echo the bad entry %q", err, tc.in)
			}
		})
	}
}

// TestHTTPClientWithOverridesEmpty returns the default client untouched
// when no override is supplied.
func TestHTTPClientWithOverridesEmpty(t *testing.T) {
	if got := HTTPClientWithOverrides(nil); got != http.DefaultClient {
		t.Errorf("expected http.DefaultClient for nil overrides, got %p", got)
	}
	if got := HTTPClientWithOverrides(HostOverrides{}); got != http.DefaultClient {
		t.Errorf("expected http.DefaultClient for empty overrides, got %p", got)
	}
}

// TestHTTPClientWithOverridesPinsDial proves the pin actually rewrites
// the dialled address: a request to an unresolvable phantom host with a
// --resolve override pointing at a live httptest server must reach that
// server. Without the pin, the phantom host would fail DNS resolution.
func TestHTTPClientWithOverridesPinsDial(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		hits++
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	host, port, err := net.SplitHostPort(srv.Listener.Addr().String())
	if err != nil {
		t.Fatalf("split server addr: %v", err)
	}

	// A hostname that will never resolve via the system resolver.
	const phantom = "keycloak.invalid.split-dns.test"
	overrides, err := ParseResolveEntries([]string{
		net.JoinHostPort(phantom, port) + ":" + host,
	})
	if err != nil {
		t.Fatalf("ParseResolveEntries: %v", err)
	}
	client := HTTPClientWithOverrides(overrides)

	req, err := http.NewRequest(http.MethodGet, "http://"+net.JoinHostPort(phantom, port)+"/", http.NoBody)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("request via pinned client failed (the pin did not take): %v", err)
	}
	_ = resp.Body.Close()
	if hits != 1 {
		t.Errorf("server hit %d times, want 1", hits)
	}
}

// TestHTTPClientWithOverridesLeavesOtherHosts confirms that a host NOT in
// the override map still goes through the normal resolver — the pin is
// per-endpoint, not a blanket rewrite.
func TestHTTPClientWithOverridesLeavesOtherHosts(t *testing.T) {
	overrides := HostOverrides{"kc.example.com:443": "10.0.0.5"}
	client := HTTPClientWithOverrides(overrides)

	// Dial a host that isn't pinned; it should fail on resolution, not
	// get silently redirected to 10.0.0.5.
	req, err := http.NewRequest(http.MethodGet, "http://other.invalid.split-dns.test:443/", http.NoBody)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	_, err = client.Do(req)
	if err == nil {
		t.Fatalf("expected resolution failure for un-pinned host")
	}
	if !IsHostResolutionError(err) {
		t.Errorf("expected a DNS resolution error for the un-pinned host, got %v", err)
	}
}

// TestIsHostResolutionError distinguishes a DNS failure from an
// arbitrary transport error.
func TestIsHostResolutionError(t *testing.T) {
	if !IsHostResolutionError(&net.DNSError{Err: "no such host", Name: "kc.example.com", IsNotFound: true}) {
		t.Error("a *net.DNSError should be classified as a host-resolution error")
	}
	if IsHostResolutionError(errors.New("connection refused")) {
		t.Error("a generic error must not be classified as host-resolution")
	}
	if IsHostResolutionError(nil) {
		t.Error("nil must not be classified as host-resolution")
	}
}

// TestOverrideHonouredOnDeviceAuthPOST is the AC #2 guard: the custom
// resolver/host-pin must be honoured on the device-authorization POST
// (and the token poll), not only on discovery. It drives the full
// RunDeviceFlow against a Keycloak whose endpoints are addressed by a
// phantom hostname that never resolves via the system resolver; only the
// --resolve pin to the live httptest server makes the POSTs land. If the
// pin were dropped anywhere in RunDeviceFlow's client threading, the
// device-auth POST would fail DNS and this test would fail.
func TestOverrideHonouredOnDeviceAuthPOST(t *testing.T) {
	var deviceAuthHits, tokenHits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/auth/device":
			deviceAuthHits++
			_ = json.NewEncoder(w).Encode(map[string]any{
				"device_code":      "dc",
				"user_code":        "UC-1234",
				"verification_uri": "https://kc/device",
				"expires_in":       600,
				"interval":         1,
			})
		case "/token":
			tokenHits++
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

	_, port, err := net.SplitHostPort(srv.Listener.Addr().String())
	if err != nil {
		t.Fatalf("split server addr: %v", err)
	}
	serverHost, _, _ := net.SplitHostPort(srv.Listener.Addr().String())

	// Endpoints point at a hostname the system resolver will reject.
	const phantom = "keycloak.invalid.split-dns.test"
	base := "http://" + net.JoinHostPort(phantom, port)
	doc := &DiscoveryDocument{
		Issuer:                      base + "/realms/meho",
		DeviceAuthorizationEndpoint: base + "/auth/device",
		TokenEndpoint:               base + "/token",
	}

	overrides, err := ParseResolveEntries([]string{
		net.JoinHostPort(phantom, port) + ":" + serverHost,
	})
	if err != nil {
		t.Fatalf("ParseResolveEntries: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	result, err := RunDeviceFlow(ctx, doc, "meho-cli", DeviceFlowOptions{
		HTTPClient: HTTPClientWithOverrides(overrides),
		Prompter:   func(context.Context, *oauth2.DeviceAuthResponse) error { return nil },
	})
	if err != nil {
		t.Fatalf("RunDeviceFlow with pinned client failed (pin not honoured on device-auth POST): %v", err)
	}
	if result.Token.AccessToken != "at" {
		t.Errorf("access token: %q", result.Token.AccessToken)
	}
	if deviceAuthHits == 0 {
		t.Error("device-authorization endpoint was never hit — the pin did not reach the device-auth POST")
	}
	if tokenHits == 0 {
		t.Error("token endpoint was never hit — the pin did not reach the token poll")
	}
}
