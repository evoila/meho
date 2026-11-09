// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func restoreTransport(t *testing.T) {
	t.Helper()
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
}

func TestTrustCAFileMakesTheServerTrusted(t *testing.T) {
	restoreTransport(t)
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()

	if _, err := http.Get(srv.URL); err == nil { //nolint:noctx,bodyclose
		t.Fatal("a self-signed server must fail before TrustCAFile")
	}
	path := filepath.Join(t.TempDir(), "ca.crt")
	block := &pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}
	if err := os.WriteFile(path, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := TrustCAFile(path); err != nil {
		t.Fatalf("TrustCAFile: %v", err)
	}
	resp, err := http.Get(srv.URL) //nolint:noctx
	if err != nil {
		t.Fatalf("after TrustCAFile: %v", err)
	}
	_ = resp.Body.Close()
}

func TestTrustCAFileRefusesAFileWithNoCertificate(t *testing.T) {
	restoreTransport(t)
	path := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(path, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	err := TrustCAFile(path)
	if err == nil || !strings.Contains(err.Error(), "no PEM certificate") {
		t.Fatalf("err = %v", err)
	}
	if err := TrustCAFile(path + ".missing"); err == nil {
		t.Fatal("a missing file must fail")
	}
}
