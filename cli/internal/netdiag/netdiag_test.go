// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package netdiag

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestClassify_Table(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want Class
	}{
		{"nil", nil, ClassNone},
		{"dns", fmt.Errorf("get: %w", &net.DNSError{Err: "no such host", Name: "x.invalid", IsNotFound: true}), ClassDNS},
		{"unknown ca", fmt.Errorf("get: %w", x509.UnknownAuthorityError{}), ClassTLS},
		{"refused", fmt.Errorf("get: %w", &net.OpError{Op: "dial", Err: errors.New("connection refused")}), ClassUnreachable},
		{"deadline", fmt.Errorf("get: %w", context.DeadlineExceeded), ClassUnreachable},
		{"other", errors.New("boom"), ClassOther},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Classify(tc.err); got != tc.want {
				t.Errorf("Classify(%v) = %q, want %q", tc.err, got, tc.want)
			}
		})
	}
}

// TestClassify_RealTLSFailure dials a TLS test server with a client that
// does not trust its certificate, so the error comes from the real
// crypto/tls stack, not a hand-built value.
func TestClassify_RealTLSFailure(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()
	_, err := (&http.Client{}).Get(srv.URL)
	if err == nil {
		t.Fatal("expected a TLS error")
	}
	if got := Classify(err); got != ClassTLS {
		t.Errorf("Classify = %q, want tls (err=%v)", got, err)
	}
}

func TestHint_NamesHost(t *testing.T) {
	for _, c := range []Class{ClassDNS, ClassTLS, ClassUnreachable} {
		if h := Hint(c, "backplane.example"); h == "" {
			t.Errorf("Hint(%q) is empty", c)
		}
	}
	if Hint(ClassOther, "h") != "" || Hint(ClassNone, "h") != "" {
		t.Error("Hint should be empty for other/none")
	}
}
