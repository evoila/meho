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
// http.DefaultTransport, next to the system roots. Every CLI request
// goes through that transport (http.DefaultClient, the bare
// &http.Client{} of `meho version` and `meho doctor`, and the clones
// the --resolve and response-cap wrappers make), so one call covers
// them all, the Keycloak host included.
//
// It swaps in a clone rather than editing the transport in place: the
// discovery fetch may already have used the old one.
func TrustCAFile(path string) error {
	pem, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read CA file: %w", err)
	}
	roots, err := x509.SystemCertPool()
	if err != nil || roots == nil {
		roots = x509.NewCertPool()
	}
	if !roots.AppendCertsFromPEM(pem) {
		return fmt.Errorf("CA file %s holds no PEM certificate", path)
	}
	base, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return errors.New("the default HTTP transport cannot take a CA file")
	}
	transport := base.Clone()
	if transport.TLSClientConfig == nil {
		transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	}
	transport.TLSClientConfig.RootCAs = roots
	http.DefaultTransport = transport
	return nil
}
