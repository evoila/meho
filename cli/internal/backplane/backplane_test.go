// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package backplane

import (
	"errors"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
)

func TestNormaliseURLTrimsAndValidates(t *testing.T) {
	got, err := NormaliseURL("https://meho.test/")
	if err != nil || got != "https://meho.test" {
		t.Fatalf("NormaliseURL: got %q, err %v", got, err)
	}
	if _, err := NormaliseURL("   "); err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("blank URL should error 'empty', got %v", err)
	}
	if _, err := NormaliseURL("notaurl"); err == nil {
		t.Fatalf("malformed URL should error")
	}
}

// TestNormaliseURLRejectsRemoteHTTP — plaintext http:// to a routed
// host must always be rejected on the resolution path: the bearer token
// rides every request in cleartext over http (OWASP TLS cheat sheet,
// #101 L17).
func TestNormaliseURLRejectsRemoteHTTP(t *testing.T) {
	cases := []string{
		"http://meho.test",
		"http://169.254.169.254",
		"http://10.0.0.5:8080",
	}
	for _, in := range cases {
		if _, err := NormaliseURL(in); err == nil || !strings.Contains(err.Error(), "routed host") {
			t.Errorf("NormaliseURL(%q): want routed-host rejection, got %v", in, err)
		}
	}
}

// TestNormaliseURLAcceptsLoopbackHTTP — plaintext http:// to a loopback
// host is accepted on the resolution path: it never leaves the machine,
// so there is no cleartext-transmission risk. (login itself is stricter
// — it requires --insecure-allow-http even for loopback.)
func TestNormaliseURLAcceptsLoopbackHTTP(t *testing.T) {
	cases := map[string]string{
		"http://localhost:8080/": "http://localhost:8080",
		"http://127.0.0.1:8080":  "http://127.0.0.1:8080",
		"http://[::1]:8080":      "http://[::1]:8080",
	}
	for in, want := range cases {
		got, err := NormaliseURL(in)
		if err != nil || got != want {
			t.Errorf("NormaliseURL(%q): got %q, err %v (want %q)", in, got, err, want)
		}
	}
}

// TestNormaliseURLAcceptsHTTPS — https is the happy path and stays
// untouched apart from trailing-slash trimming.
func TestNormaliseURLAcceptsHTTPS(t *testing.T) {
	got, err := NormaliseURL("https://meho.test:8443/")
	if err != nil || got != "https://meho.test:8443" {
		t.Fatalf("NormaliseURL(https): got %q, err %v", got, err)
	}
}

// TestNormaliseURLRejectsNonHTTPScheme — non-http(s) schemes the CLI
// can't dial fail fast with an actionable error.
func TestNormaliseURLRejectsNonHTTPScheme(t *testing.T) {
	for _, in := range []string{"ftp://meho.test", "ssh://meho.test", "file:///tmp/x"} {
		if _, err := NormaliseURL(in); err == nil || !strings.Contains(err.Error(), "must use https") {
			t.Errorf("NormaliseURL(%q): want scheme rejection, got %v", in, err)
		}
	}
}

// TestNormaliseURLAllowHTTPFalseRejectsLoopback — with allowHTTP=false
// (the login default) even loopback http is rejected, demanding the
// explicit --insecure-allow-http opt-in.
func TestNormaliseURLAllowHTTPFalseRejectsLoopback(t *testing.T) {
	for _, in := range []string{"http://localhost:8080", "http://127.0.0.1:8080"} {
		if _, err := NormaliseURLAllowHTTP(in, false); err == nil || !strings.Contains(err.Error(), "--insecure-allow-http") {
			t.Errorf("NormaliseURLAllowHTTP(%q, false): want opt-in hint, got %v", in, err)
		}
	}
}

// TestNormaliseURLAllowHTTPPermitsLoopback — the --insecure-allow-http
// opt-in permits plaintext only for loopback hosts.
func TestNormaliseURLAllowHTTPPermitsLoopback(t *testing.T) {
	cases := map[string]string{
		"http://localhost:8080/": "http://localhost:8080",
		// Hostnames are case-insensitive (RFC 4343); url.Hostname()
		// preserves case, so the loopback check must fold case.
		"http://LOCALHOST:8080": "http://LOCALHOST:8080",
		"http://127.0.0.1:8080": "http://127.0.0.1:8080",
		"http://[::1]:8080":     "http://[::1]:8080",
	}
	for in, want := range cases {
		got, err := NormaliseURLAllowHTTP(in, true)
		if err != nil || got != want {
			t.Errorf("NormaliseURLAllowHTTP(%q, true): got %q, err %v (want %q)", in, got, err, want)
		}
	}
}

// TestNormaliseURLAllowHTTPRejectsRemote — even with the opt-in,
// plaintext http:// to a routed (non-loopback) host is rejected: the
// token would still cross the network in the clear.
func TestNormaliseURLAllowHTTPRejectsRemote(t *testing.T) {
	for _, in := range []string{"http://meho.test", "http://169.254.169.254", "http://10.0.0.5:8080"} {
		if _, err := NormaliseURLAllowHTTP(in, true); err == nil || !strings.Contains(err.Error(), "routed host") {
			t.Errorf("NormaliseURLAllowHTTP(%q, true): want routed-host rejection, got %v", in, err)
		}
	}
}

// TestNormaliseURLAllowHTTPStillAcceptsHTTPS — passing the opt-in does
// not weaken the https happy path.
func TestNormaliseURLAllowHTTPStillAcceptsHTTPS(t *testing.T) {
	got, err := NormaliseURLAllowHTTP("https://meho.test/", true)
	if err != nil || got != "https://meho.test" {
		t.Fatalf("NormaliseURLAllowHTTP(https, true): got %q, err %v", got, err)
	}
}

func TestResolveOverrideWins(t *testing.T) {
	got, err := Resolve("https://override.test/")
	if err != nil || got != "https://override.test" {
		t.Fatalf("Resolve(override): got %q, err %v", got, err)
	}
}

func TestClassifyError(t *testing.T) {
	notConfigured := &NotConfiguredError{Inner: auth.ErrConfigNotFound}
	if se := ClassifyError(notConfigured); se == nil || se.Code != "auth_expired" {
		t.Fatalf("not-configured should classify as auth_expired, got %+v", se)
	}
	if se := ClassifyError(errors.New("parse boom")); se == nil || se.Code != "unexpected_response" {
		t.Fatalf("arbitrary error should classify as unexpected_response, got %+v", se)
	}
}

func TestNotConfiguredErrorUnwrapsToConfigNotFound(t *testing.T) {
	err := &NotConfiguredError{Inner: auth.ErrConfigNotFound}
	if !errors.Is(err, auth.ErrConfigNotFound) {
		t.Fatalf("NotConfiguredError should unwrap to auth.ErrConfigNotFound")
	}
	if !strings.Contains(err.Error(), "meho login") {
		t.Fatalf("error message should hint `meho login`, got %q", err.Error())
	}
}
