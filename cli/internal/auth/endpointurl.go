// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"fmt"
	"net"
	"net/url"
	"strings"
)

// Well-known paths of the discovery document, below the realm address.
const (
	oidcWellKnownPath  = "/.well-known/openid-configuration"
	oauthWellKnownPath = "/.well-known/oauth-authorization-server"
)

// IsLoopbackHost reports whether host (a URL hostname, port already
// stripped) refers to the local machine: the literal "localhost"
// (case-insensitive, since hostnames are per RFC 4343), or any IP
// literal in a loopback range (127.0.0.0/8, ::1).
func IsLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// checkEndpointURL returns an error unless raw is an absolute https
// URL, or an http URL to a loopback host. field names the discovery
// field in the error text.
func checkEndpointURL(field, raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("%s %q is not a valid URL: %w", field, raw, err)
	}
	if u.Host == "" {
		return fmt.Errorf("%s %q has no host", field, raw)
	}
	switch u.Scheme {
	case "https":
		return nil
	case "http":
		if IsLoopbackHost(u.Hostname()) {
			return nil
		}
		return fmt.Errorf("%s %q uses plain http:// to a host that is not this machine; "+
			"the identity provider must use https://", field, raw)
	default:
		return fmt.Errorf("%s %q must use https://", field, raw)
	}
}

// realmFromDiscoveryURL returns the realm address that discoveryURL
// was built from (see DiscoveryURLs), or false when discoveryURL does
// not end in a known well-known path.
func realmFromDiscoveryURL(discoveryURL string) (string, bool) {
	for _, suffix := range []string{oidcWellKnownPath, oauthWellKnownPath} {
		if realm, ok := strings.CutSuffix(discoveryURL, suffix); ok {
			return realm, true
		}
	}
	return "", false
}

// checkDiscoveryDocument checks that doc belongs to the realm it was
// fetched from and that its addresses are safe to send tokens to.
// The issuer must be the realm address (OpenID Connect Discovery 1.0
// §4.3, RFC 8414 §3.3); a trailing slash is ignored. The issuer and
// every endpoint must use https, or http to a loopback host.
func checkDiscoveryDocument(discoveryURL string, doc *DiscoveryDocument) error {
	realm, ok := realmFromDiscoveryURL(discoveryURL)
	if !ok {
		return fmt.Errorf("discovery URL %q does not end in %s or %s",
			discoveryURL, oidcWellKnownPath, oauthWellKnownPath)
	}
	if strings.TrimRight(doc.Issuer, "/") != strings.TrimRight(realm, "/") {
		return fmt.Errorf("discovery document names issuer %q, but it was fetched from the realm %q; "+
			"the two must be the same", doc.Issuer, realm)
	}
	fields := []struct{ name, value string }{
		{"issuer", doc.Issuer},
		{"device_authorization_endpoint", doc.DeviceAuthorizationEndpoint},
		{"token_endpoint", doc.TokenEndpoint},
	}
	// Only the browser sign-in uses authorization_endpoint, and
	// RunFreshSignIn refuses a document without it.
	if doc.AuthorizationEndpoint != "" {
		fields = append(fields, struct{ name, value string }{"authorization_endpoint", doc.AuthorizationEndpoint})
	}
	for _, f := range fields {
		if err := checkEndpointURL(f.name, f.value); err != nil {
			return err
		}
	}
	return nil
}
