// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
)

// newSkipVerifyTLSConfig returns a *tls.Config with InsecureSkipVerify
// set, isolated to its own file so the bootstrap and command code
// don't carry the import noise. Only called from the
// --insecure-skip-tls-verify code path; the production default is the
// system trust store via http.DefaultTransport's TLSClientConfig=nil.
//
// Prefer --keycloak-ca-bundle (newCABundleTLSConfig) over this flag: a
// CA-bundle pin keeps certificate *and* hostname verification on while
// still trusting an internal realm CA. This blanket skip disables both
// and is the last-resort escape hatch, warned loudly at the call site.
//
// We accept the gosec G402 lint hit at the call site (the only one in
// the package); the install-time use case — operator workstation
// without the realm's CA system-wide — is exactly what this flag is
// for, and it matches the reference shell script's `curl -k`.
//
//nolint:gosec // intentional opt-in to InsecureSkipVerify via flag
func newSkipVerifyTLSConfig() *tls.Config {
	return &tls.Config{InsecureSkipVerify: true} //nolint:gosec
}

// newCABundleTLSConfig returns a *tls.Config that trusts the CA(s) in
// the PEM file at caBundlePath while keeping full certificate-chain and
// hostname verification on (InsecureSkipVerify stays false). This is the
// secure supersession of --insecure-skip-tls-verify for the common
// install-time case: the operator's workstation has no system trust for
// the realm's private CA, but does have that CA's certificate on disk.
// The admin-password grant and every subsequent Bearer-token request
// then run over verified TLS. Mirrors the target-level `tls_ca_pin`
// (the backplane's per-target CA pin): the bundle replaces the system
// roots for this client rather than adding to them.
func newCABundleTLSConfig(caBundlePath string) (*tls.Config, error) {
	pem, err := os.ReadFile(caBundlePath)
	if err != nil {
		return nil, fmt.Errorf("read CA bundle %q: %w", caBundlePath, err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf(
			"CA bundle %q contained no PEM certificates", caBundlePath)
	}
	return &tls.Config{
		RootCAs:    pool,
		MinVersion: tls.VersionTLS12,
	}, nil
}
