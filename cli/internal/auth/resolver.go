// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
)

// HostOverrides maps a "host:port" key to a literal IP address, mirroring
// the semantics of `curl --resolve <host>:<port>:<ip>`. It is the escape
// hatch for split-DNS operator workstations where the system resolver
// returns NXDOMAIN for the Keycloak host even though that host is
// reachable at a known IP (VPN-pushed DNS forwarder configured for the
// backplane name but not the Keycloak name).
//
// The map is keyed by the exact "host:port" pair rather than by host
// alone so an operator can pin a single endpoint (443) without
// accidentally short-circuiting resolution for a different port on the
// same host. This matches curl's per-port override model.
type HostOverrides map[string]string

// ParseResolveEntries parses zero or more `host:port:ip` strings into a
// HostOverrides map. The format is identical to `curl --resolve`:
//
//	kc.example.com:443:10.0.0.5
//
// The port is required (and must be numeric) so the override targets a
// specific endpoint; the IP is validated as a literal IPv4 or IPv6
// address. IPv6 addresses are accepted in either bare or bracketed form
// on the IP side (`kc:443:[::1]` and `kc:443:::1` both resolve to `::1`),
// with the host taken as everything before the last two colon-separated
// fields so IPv6 literals used as the *host* are not silently mangled.
//
// A malformed entry is a hard error rather than a silent skip: an
// operator who mistypes their override should learn immediately, not
// discover at connect time that the CLI ignored the flag and fell back
// to the broken system resolver.
func ParseResolveEntries(entries []string) (HostOverrides, error) {
	if len(entries) == 0 {
		return nil, nil
	}
	overrides := make(HostOverrides, len(entries))
	for _, raw := range entries {
		host, port, ip, err := splitResolveEntry(raw)
		if err != nil {
			return nil, fmt.Errorf("--resolve %q: %w", raw, err)
		}
		overrides[net.JoinHostPort(host, port)] = ip
	}
	return overrides, nil
}

// splitResolveEntry parses one `host:port:ip` string. The IP field may
// itself contain colons (IPv6), so we split off the IP as everything
// after the last two structural colons: the trailing field is the IP and
// the field before it is the port. Whatever remains is the host.
func splitResolveEntry(raw string) (host, port, ip string, err error) {
	// Find the port:ip boundary by walking from the front: host and port
	// are the first two colon-delimited fields; the rest is the IP.
	first := strings.IndexByte(raw, ':')
	if first < 0 {
		return "", "", "", errors.New("expected host:port:ip")
	}
	rest := raw[first+1:]
	second := strings.IndexByte(rest, ':')
	if second < 0 {
		return "", "", "", errors.New("expected host:port:ip")
	}
	host = raw[:first]
	port = rest[:second]
	ip = rest[second+1:]

	if host == "" {
		return "", "", "", errors.New("host is empty")
	}
	if _, portErr := net.LookupPort("tcp", port); portErr != nil {
		return "", "", "", fmt.Errorf("port %q is not a valid TCP port", port)
	}
	// Accept a bracketed IPv6 literal on the IP side for symmetry with
	// how hosts are written elsewhere; strip the brackets before parsing.
	ip = strings.TrimPrefix(strings.TrimSuffix(ip, "]"), "[")
	if ip == "" {
		return "", "", "", errors.New("ip is empty")
	}
	if net.ParseIP(ip) == nil {
		return "", "", "", fmt.Errorf("ip %q is not a valid IP address", ip)
	}
	return host, port, ip, nil
}

// HTTPClientWithOverrides returns an *http.Client whose transport pins
// the dialled address for any host:port present in overrides, leaving
// every other connection to the system resolver. Pass a nil/empty map to
// get http.DefaultClient unchanged — callers can therefore build the
// client unconditionally and only pay for a custom transport when the
// operator actually supplied a --resolve entry.
//
// The pin lives in Transport.DialContext, which runs *after* the URL has
// been split into host:port but *before* name resolution. When the
// dialled "host:port" matches an override we substitute the operator's
// IP; the connection still carries the original Host header and SNI
// (net/http builds those from the request URL, not from the dial
// address), so TLS certificate validation continues to check the real
// hostname. This is exactly `curl --resolve`'s behaviour and, crucially,
// does NOT weaken TLS trust the way a resolver rewrite that also changed
// SNI would.
func HTTPClientWithOverrides(overrides HostOverrides) *http.Client {
	if len(overrides) == 0 {
		return http.DefaultClient
	}
	base, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		// http.DefaultTransport is always an *http.Transport in the
		// standard library; the type assertion is defensive. If it ever
		// isn't, fall back to the default client so login still works
		// (minus the override) rather than panicking.
		return http.DefaultClient
	}
	transport := base.Clone()
	dialer := &net.Dialer{}
	transport.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		if pinned, ok := overrides[addr]; ok {
			_, port, err := net.SplitHostPort(addr)
			if err == nil {
				addr = net.JoinHostPort(pinned, port)
			}
		}
		return dialer.DialContext(ctx, network, addr)
	}
	return &http.Client{Transport: transport}
}

// IsHostResolutionError reports whether err was caused by DNS name
// resolution failing (NXDOMAIN / "no such host"). It is used to turn the
// opaque transport error into an actionable, host-named hint that points
// the operator at the --resolve escape hatch.
func IsHostResolutionError(err error) bool {
	var dnsErr *net.DNSError
	return errors.As(err, &dnsErr)
}
