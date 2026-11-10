// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net/http"
	"os"
)

// TrustCAFile adds the PEM certificates in path to the roots of
// http.DefaultTransport, next to the system roots, for this process.
// Every CLI request goes through that transport (http.DefaultClient,
// the bare &http.Client{} of `meho version` and `meho doctor`, and the
// clones the --resolve and response-cap wrappers make), so one call
// covers them all, the Keycloak host included.
//
// Call it only once the process is known to work against the backplane
// the CA belongs to: a CA of one backplane must not vouch for another
// one that is about to receive credentials.
func TrustCAFile(path string) error {
	transport, err := transportTrusting(path)
	if err != nil {
		return err
	}
	http.DefaultTransport = transport
	return nil
}

// ClientTrustingCAFile returns a client that trusts the PEM
// certificates in path, and leaves http.DefaultTransport alone.
func ClientTrustingCAFile(path string) (*http.Client, error) {
	transport, err := transportTrusting(path)
	if err != nil {
		return nil, err
	}
	return &http.Client{Transport: transport}, nil
}

// transportTrusting clones the default transport, so a discovery fetch
// that already used the old one is not disturbed, and adds the
// certificates in path to its roots.
func transportTrusting(path string) (*http.Transport, error) {
	pem, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read CA file: %w", err)
	}
	roots, err := x509.SystemCertPool()
	if err != nil || roots == nil {
		roots = x509.NewCertPool()
	}
	if !roots.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("CA file %s holds no PEM certificate", path)
	}
	base, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return nil, errors.New("the default HTTP transport cannot take a CA file")
	}
	transport := base.Clone()
	if transport.TLSClientConfig == nil {
		transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	}
	transport.TLSClientConfig.RootCAs = roots
	return transport, nil
}
