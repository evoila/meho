// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// writeSelfSignedCA writes a minimal self-signed CA certificate to a
// temp PEM file and returns its path. Enough for AppendCertsFromPEM to
// accept it as a trust anchor — the F08 CA-bundle-pin path.
func writeSelfSignedCA(t *testing.T) string {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate CA key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "meho-test-ca"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create CA cert: %v", err)
	}
	path := filepath.Join(t.TempDir(), "ca.pem")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatalf("write CA pem: %v", err)
	}
	return path
}

func TestNewCABundleTLSConfig_TrustsBundleWithVerificationOn(t *testing.T) {
	path := writeSelfSignedCA(t)

	cfg, err := newCABundleTLSConfig(path)
	if err != nil {
		t.Fatalf("newCABundleTLSConfig: %v", err)
	}
	if cfg.InsecureSkipVerify {
		t.Error("CA-bundle TLS config must keep InsecureSkipVerify false")
	}
	if cfg.RootCAs == nil {
		t.Error("CA-bundle TLS config must set a RootCAs pool")
	}
	if cfg.MinVersion < tls.VersionTLS12 {
		t.Errorf("MinVersion = %d, want >= TLS 1.2 (%d)", cfg.MinVersion, tls.VersionTLS12)
	}
}

func TestNewCABundleTLSConfig_RejectsNonPEM(t *testing.T) {
	path := filepath.Join(t.TempDir(), "garbage.pem")
	if err := os.WriteFile(path, []byte("not a certificate"), 0o600); err != nil {
		t.Fatalf("write garbage: %v", err)
	}
	if _, err := newCABundleTLSConfig(path); err == nil {
		t.Fatal("expected an error for a file with no PEM certificates")
	}
}

func TestNewCABundleTLSConfig_MissingFileErrors(t *testing.T) {
	if _, err := newCABundleTLSConfig(filepath.Join(t.TempDir(), "absent.pem")); err == nil {
		t.Fatal("expected an error for a missing CA bundle file")
	}
}

// TestBootstrapClientsCmd_CABundleAndInsecureAreMutuallyExclusive drives
// the cobra RunE far enough to reach the TLS-trust selection and asserts
// the two flags are refused together (before any network call).
func TestBootstrapClientsCmd_CABundleAndInsecureAreMutuallyExclusive(t *testing.T) {
	t.Setenv("KEYCLOAK_ADMIN_PASSWORD", "pw") //nolint:gosec // test-only literal
	cmd := newBootstrapClientsCmd()
	var out, errOut bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	cmd.SetArgs([]string{
		"--keycloak-base-url", "https://kc.example.com",
		"--realm", "evba",
		"--admin-username", "admin",
		"--skip-user-provisioning",
		"--keycloak-ca-bundle", writeSelfSignedCA(t),
		"--insecure-skip-tls-verify",
		"--dry-run",
	})

	err := cmd.Execute()
	if err == nil {
		t.Fatal("expected mutually-exclusive error, got nil")
	}
	if !strings.Contains(err.Error(), "mutually exclusive") {
		t.Errorf("error = %q, want it to mention mutual exclusion", err.Error())
	}
}

// TestBootstrapClientsCmd_InsecureFlagWarnsLoudly asserts the escape
// hatch prints a loud stderr warning (F08 AC: the flag stays opt-in and
// warned). Runs under --dry-run so no Keycloak call is made.
func TestBootstrapClientsCmd_InsecureFlagWarnsLoudly(t *testing.T) {
	t.Setenv("KEYCLOAK_ADMIN_PASSWORD", "pw") //nolint:gosec // test-only literal
	cmd := newBootstrapClientsCmd()
	var out, errOut bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	cmd.SetArgs([]string{
		"--keycloak-base-url", "https://kc.example.com",
		"--realm", "evba",
		"--admin-username", "admin",
		"--mcp-resource-uri", "https://meho.example.com/mcp",
		"--skip-user-provisioning",
		"--insecure-skip-tls-verify",
		"--dry-run",
	})

	if err := cmd.Execute(); err != nil {
		t.Fatalf("dry-run with --insecure-skip-tls-verify: %v", err)
	}
	if !strings.Contains(errOut.String(), "WARNING: --insecure-skip-tls-verify") {
		t.Errorf("stderr missing loud warning; got:\n%s", errOut.String())
	}
}

// TestBootstrapClientsCmd_CABundleReadErrorSurfaces asserts the
// --keycloak-ca-bundle path is wired into RunE: an unreadable bundle
// fails the command rather than silently falling back to system trust.
func TestBootstrapClientsCmd_CABundleReadErrorSurfaces(t *testing.T) {
	t.Setenv("KEYCLOAK_ADMIN_PASSWORD", "pw") //nolint:gosec // test-only literal
	cmd := newBootstrapClientsCmd()
	var out, errOut bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	cmd.SetArgs([]string{
		"--keycloak-base-url", "https://kc.example.com",
		"--realm", "evba",
		"--admin-username", "admin",
		"--mcp-resource-uri", "https://meho.example.com/mcp",
		"--skip-user-provisioning",
		"--keycloak-ca-bundle", filepath.Join(t.TempDir(), "absent.pem"),
		"--dry-run",
	})

	err := cmd.Execute()
	if err == nil {
		t.Fatal("expected a CA-bundle read error, got nil")
	}
	if !strings.Contains(err.Error(), "read CA bundle") {
		t.Errorf("error = %q, want it to mention the CA bundle read failure", err.Error())
	}
}
