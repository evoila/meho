// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import "crypto/tls"

// newSkipVerifyTLSConfig returns a *tls.Config with InsecureSkipVerify
// set, isolated to its own file so the bootstrap and command code
// don't carry the import noise. Only called from the
// --insecure-skip-tls-verify code path; the production default is the
// system trust store via http.DefaultTransport's TLSClientConfig=nil.
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
