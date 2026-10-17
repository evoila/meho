// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package netdiag sorts a failed network call into a small set of
// failure classes: the host name did not resolve (DNS), the TLS
// handshake failed, or the host could not be reached at all.
//
// It is the one shared classifier for client diagnostics. `meho doctor`
// uses it, and the first-start checks of `meho status` are meant to
// use it too, so both commands name the same failure the same way.
// Keep it small: it only looks at the error chain, it never dials.
package netdiag

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"strings"
)

// Class is one failure bucket.
type Class string

const (
	// ClassNone means there was no error.
	ClassNone Class = ""
	// ClassDNS means the host name did not resolve (for example
	// NXDOMAIN, often a split-DNS gap on a VPN).
	ClassDNS Class = "dns"
	// ClassTLS means the TCP connection worked but the TLS handshake
	// failed: an unknown CA, a wrong host name on the certificate, an
	// expired certificate, or a plain-HTTP server on a TLS port.
	ClassTLS Class = "tls"
	// ClassUnreachable means the host name resolved but nothing
	// answered: connection refused, a timeout, or no route.
	ClassUnreachable Class = "unreachable"
	// ClassOther means the error is not a network error this package
	// knows about.
	ClassOther Class = "other"
)

// Classify returns the failure class for err. It walks the wrapped
// error chain, so callers can pass errors wrapped with %w.
func Classify(err error) Class {
	if err == nil {
		return ClassNone
	}
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return ClassDNS
	}
	if isTLSError(err) {
		return ClassTLS
	}
	if isUnreachable(err) {
		return ClassUnreachable
	}
	return ClassOther
}

func isTLSError(err error) bool {
	var (
		unknownAuthority x509.UnknownAuthorityError
		hostname         x509.HostnameError
		invalid          x509.CertificateInvalidError
		verify           *tls.CertificateVerificationError
		recordHeader     tls.RecordHeaderError
		alert            tls.AlertError
	)
	switch {
	case errors.As(err, &unknownAuthority),
		errors.As(err, &hostname),
		errors.As(err, &invalid),
		errors.As(err, &verify),
		errors.As(err, &recordHeader),
		errors.As(err, &alert):
		return true
	}
	// Some TLS failures only surface as text (for example a handshake
	// failure reported by the remote side through a proxy).
	return strings.Contains(err.Error(), "tls: ")
}

func isUnreachable(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

// Hint returns a short, plain-English next step for a failure class
// against host. It returns "" for ClassNone and ClassOther.
func Hint(class Class, host string) string {
	switch class {
	case ClassDNS:
		return "the name " + host + " does not resolve on this machine. Check your VPN and DNS. " +
			"If only some hosts fail (split DNS), `meho login --resolve <host>:443:<ip>` pins a host to an IP"
	case ClassTLS:
		return "the TLS check for " + host + " failed. If the deploy uses an internal CA, add it to " +
			"the system trust store (and set MEHO_CA_CERT for the Claude plugin)"
	case ClassUnreachable:
		return host + " resolves but does not answer. Check the VPN, a firewall, or whether the backplane is running"
	}
	return ""
}
