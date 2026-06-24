// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package backplane holds the backplane-URL resolution + error
// classification helpers shared by every cmd/* command package. Before
// it, each package carried a byte-identical resolveBackplane /
// classifyBackplaneError / normaliseURL / errNoBackplaneConfigured —
// duplicated only because sibling cmd/* packages can't import one
// another without an import cycle (cmd/root.go grafts each onto the
// tree). The per-package transport (doAuthedRequest) and error-render
// (renderRequestError) helpers stay put: they have genuine per-connector
// variants (response-size caps, overflow handling) that aren't safe to
// merge mechanically.
package backplane

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// NotConfiguredError wraps auth.ErrConfigNotFound so callers can
// distinguish "operator never logged in" (→ auth_expired exit code 2 —
// the fix is `meho login`) from URL-parse failures (→ unexpected exit
// code 4 — the fix is correcting argv).
type NotConfiguredError struct{ Inner error }

func (e *NotConfiguredError) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}

func (e *NotConfiguredError) Unwrap() error { return e.Inner }

// Resolve returns the backplane URL: the --backplane override when set,
// otherwise the URL recorded by the most recent `meho login`. A missing
// config surfaces as *NotConfiguredError so ClassifyError can route it
// to auth_expired.
func Resolve(override string) (string, error) {
	if override != "" {
		return NormaliseURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", &NotConfiguredError{Inner: err}
		}
		return "", err
	}
	return NormaliseURL(cfg.BackplaneURL)
}

// ClassifyError maps a Resolve error to the right output.StructuredError
// category: missing-config → auth_expired; everything else (parse / fs
// errors) → unexpected.
func ClassifyError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// NormaliseURL strips trailing slashes, parses the URL to fail fast on
// garbage input, and enforces the transport-security policy: https is
// always accepted; plaintext http:// is accepted only for a loopback
// host (localhost / 127.0.0.0/8 / ::1); http:// to any routed host is
// rejected. The bearer token minted by `meho login` rides every request
// in an Authorization header, so plaintext to a routed host would
// transmit it in the clear (OWASP Transport Layer Security cheat sheet)
// — but a loopback connection never leaves the machine, so it carries
// no such risk. This is the resolver every verb runs against the stored
// (already login-vetted) URL or a --backplane override.
func NormaliseURL(s string) (string, error) {
	// The stored/override path tolerates loopback http: a value can
	// only have reached the config by passing `meho login`'s stricter
	// gate (which requires --insecure-allow-http even for loopback),
	// and httptest harnesses dial loopback http.
	return NormaliseURLAllowHTTP(s, true)
}

// NormaliseURLAllowHTTP is NormaliseURL with explicit control over the
// loopback-http allowance. `meho login` passes allowHTTP=false by
// default so first-contact login is https-only, and allowHTTP=true only
// when the operator passes --insecure-allow-http for a localhost
// backplane. Regardless of allowHTTP, plaintext http:// to a routed
// (non-loopback) host is always rejected — a token sent over http:// to
// a routed host crosses the network in the clear regardless of operator
// intent. This mirrors the deliberately narrow gating of the
// `--insecure-skip-tls-verify` bootstrap flag: a convenience for local
// dev, never a blanket cleartext escape hatch.
func NormaliseURLAllowHTTP(s string, allowHTTP bool) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(s), "/")
	if trimmed == "" {
		return "", errors.New("backplane URL is empty")
	}
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid backplane URL %q: %w", s, err)
	}
	// Scheme before host: the transport-security policy is the more
	// fundamental gate, and it gives a clearer message for hostless
	// non-http schemes (e.g. file:///tmp/x, whose host is empty) than
	// the generic "has no host".
	if err := validateScheme(u, allowHTTP); err != nil {
		return "", err
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
}

// validateScheme enforces the transport-security policy on a parsed
// backplane URL: https always passes; plaintext http to a routed host
// is always rejected; plaintext http to a loopback host passes only
// when allowHTTP is set; any other scheme is rejected.
func validateScheme(u *url.URL, allowHTTP bool) error {
	switch u.Scheme {
	case "https":
		return nil
	case "http":
		// Routed-host plaintext is rejected regardless of allowHTTP:
		// the token would cross the network in the clear no matter the
		// operator's intent.
		if !isLoopbackHost(u.Hostname()) {
			return fmt.Errorf(
				"backplane URL %q uses plaintext http:// to a routed host — the bearer token would be "+
					"sent in the clear; use https://", u.String())
		}
		// Loopback plaintext never leaves the machine, but first-contact
		// login still demands the explicit --insecure-allow-http opt-in.
		if !allowHTTP {
			return fmt.Errorf(
				"backplane URL %q uses plaintext http:// — pass --insecure-allow-http to allow a "+
					"localhost backplane, or use https://", u.String())
		}
		return nil
	default:
		return fmt.Errorf("backplane URL %q must use https (or http for a localhost backplane)", u.String())
	}
}

// isLoopbackHost reports whether host (a URL hostname, port already
// stripped) refers to the local machine: the literal "localhost"
// (case-insensitive, since hostnames are per RFC 4343), or any IP
// literal in a loopback range (127.0.0.0/8, ::1).
func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
