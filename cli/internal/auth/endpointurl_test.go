// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

// docTransport answers every request with one discovery document, for
// any host. It lets a test fetch from a routed host name without a
// network.
type docTransport struct {
	doc  map[string]any
	hits int
}

func (d *docTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	d.hits++
	body, err := json.Marshal(d.doc)
	if err != nil {
		return nil, err
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(string(body))),
		Request:    r,
	}, nil
}

// realmDoc is a valid discovery document for realm, with every
// endpoint on the same host.
func realmDoc(realm string) map[string]any {
	return map[string]any{
		"issuer":                        realm,
		"device_authorization_endpoint": realm + "/protocol/openid-connect/auth/device",
		"token_endpoint":                realm + "/protocol/openid-connect/token",
		"authorization_endpoint":        realm + "/protocol/openid-connect/auth",
	}
}

func fetchWith(t *testing.T, realm string, doc map[string]any) (*DiscoveryDocument, error) {
	t.Helper()
	client := &http.Client{Transport: &docTransport{doc: doc}}
	return FetchDiscovery(context.Background(), client, realm+oidcWellKnownPath)
}

func TestFetchDiscoveryAcceptsMatchingHTTPSIssuer(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	for _, wellKnown := range []string{oidcWellKnownPath, oauthWellKnownPath} {
		client := &http.Client{Transport: &docTransport{doc: realmDoc(realm)}}
		doc, err := FetchDiscovery(context.Background(), client, realm+wellKnown)
		if err != nil {
			t.Fatalf("%s: FetchDiscovery: %v", wellKnown, err)
		}
		if doc.Issuer != realm {
			t.Errorf("%s: issuer = %q, want %q", wellKnown, doc.Issuer, realm)
		}
	}
}

func TestFetchDiscoveryIgnoresATrailingSlashOnTheIssuer(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	doc := realmDoc(realm)
	doc["issuer"] = realm + "/"
	if _, err := fetchWith(t, realm, doc); err != nil {
		t.Fatalf("issuer with a trailing slash: %v", err)
	}
	if _, err := fetchWith(t, realm+"/", realmDoc(realm)); err != nil {
		t.Fatalf("realm with a trailing slash: %v", err)
	}
}

func TestFetchDiscoveryRefusesAnIssuerThatIsNotTheRealm(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	cases := map[string]string{
		"other host":  "https://other.example.test/realms/meho",
		"other realm": "https://kc.example.test/realms/other",
		"http scheme": "http://kc.example.test/realms/meho",
		"empty":       "",
		"sub path":    realm + "/x",
	}
	for name, issuer := range cases {
		t.Run(name, func(t *testing.T) {
			doc := realmDoc(realm)
			doc["issuer"] = issuer
			_, err := fetchWith(t, realm, doc)
			if err == nil {
				t.Fatalf("issuer %q: want an error, got none", issuer)
			}
			var derr *DiscoveryError
			if !errors.As(err, &derr) {
				t.Fatalf("err = %T, want *DiscoveryError", err)
			}
			if !strings.Contains(err.Error(), "issuer") {
				t.Errorf("error %q does not name the issuer", err.Error())
			}
		})
	}
}

func TestFetchDiscoveryRefusesHTTPOnARoutedHost(t *testing.T) {
	realm := "http://kc.example.test/realms/meho"
	_, err := fetchWith(t, realm, realmDoc(realm))
	if err == nil || !strings.Contains(err.Error(), "https") {
		t.Fatalf("http realm on a routed host: err = %v, want an https error", err)
	}
}

func TestFetchDiscoveryRefusesEndpointsThatAreNotHTTPS(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	fields := []string{"device_authorization_endpoint", "token_endpoint", "authorization_endpoint"}
	values := map[string]string{
		"http routed host": "http://kc.example.test/realms/meho/x",
		"file scheme":      "file:///etc/passwd",
		"app scheme":       "someapp://open",
		"no host":          "https:///x",
		"relative":         "/realms/meho/x",
	}
	for _, field := range fields {
		for name, value := range values {
			t.Run(field+"/"+name, func(t *testing.T) {
				doc := realmDoc(realm)
				doc[field] = value
				_, err := fetchWith(t, realm, doc)
				if err == nil {
					t.Fatalf("%s = %q: want an error, got none", field, value)
				}
				if !strings.Contains(err.Error(), field) {
					t.Errorf("error %q does not name %s", err.Error(), field)
				}
			})
		}
	}
}

func TestFetchDiscoveryAcceptsHTTPOnLoopback(t *testing.T) {
	for _, realm := range []string{
		"http://127.0.0.1:8080/realms/meho",
		"http://localhost:8080/realms/meho",
		"http://LOCALHOST/realms/meho",
		"http://[::1]:8080/realms/meho",
	} {
		if _, err := fetchWith(t, realm, realmDoc(realm)); err != nil {
			t.Errorf("%s: %v", realm, err)
		}
	}
}

func TestFetchDiscoveryWithoutAuthorizationEndpointStillLoads(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	doc := realmDoc(realm)
	delete(doc, "authorization_endpoint")
	if _, err := fetchWith(t, realm, doc); err != nil {
		t.Fatalf("FetchDiscovery: %v", err)
	}
}

func TestFetchDiscoveryRefusesAnURLWithoutAWellKnownPath(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	client := &http.Client{Transport: &docTransport{doc: realmDoc(realm)}}
	_, err := FetchDiscovery(context.Background(), client, realm+"/config.json")
	if err == nil || !strings.Contains(err.Error(), "well-known") {
		t.Fatalf("err = %v, want a well-known path error", err)
	}
}

// TestFetchDiscoveryFromRealmRefusesAnotherIssuer covers the path that
// token refresh and the approval sign-in use: the stored issuer is the
// realm, and a document naming another issuer is refused on both
// well-known paths.
func TestFetchDiscoveryFromRealmRefusesAnotherIssuer(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	doc := realmDoc("https://other.example.test/realms/meho")
	transport := &docTransport{doc: doc}
	_, err := FetchDiscoveryFromRealm(context.Background(), &http.Client{Transport: transport}, realm)
	if err == nil {
		t.Fatal("want an error, got none")
	}
	if transport.hits != 2 {
		t.Errorf("hits = %d, want 2 (both well-known paths tried)", transport.hits)
	}
}

func TestIsLoopbackHost(t *testing.T) {
	cases := map[string]bool{
		"localhost":       true,
		"LocalHost":       true,
		"127.0.0.1":       true,
		"127.1.2.3":       true,
		"::1":             true,
		"kc.example.test": false,
		"localhost.test":  false,
		"192.0.2.10":      false,
		"":                false,
		"127.0.0.1.test":  false,
		"0:0:0:0:0:0:0:1": true,
		"2001:db8::1":     false,
	}
	for host, want := range cases {
		if got := IsLoopbackHost(host); got != want {
			t.Errorf("IsLoopbackHost(%q) = %v, want %v", host, got, want)
		}
	}
}

// TestRunFreshSignInOpensOnlyWebLinks checks that a sign-in link that
// is not https (or http to this machine) never reaches the opener.
func TestRunFreshSignInOpensOnlyWebLinks(t *testing.T) {
	for _, endpoint := range []string{
		"file:///etc/passwd",
		"someapp://open",
		"http://kc.example.test/realms/meho/auth",
		"javascript:alert(1)",
		"/realms/meho/auth",
	} {
		t.Run(endpoint, func(t *testing.T) {
			opened := false
			doc := &DiscoveryDocument{AuthorizationEndpoint: endpoint, TokenEndpoint: "https://kc.example.test/token"}
			_, err := RunFreshSignIn(context.Background(), doc, "meho-cli", FreshSignInOptions{
				OpenBrowser: func(string) error { opened = true; return nil },
			})
			if err == nil || !strings.Contains(err.Error(), "authorization_endpoint") {
				t.Fatalf("err = %v, want an authorization_endpoint error", err)
			}
			if opened {
				t.Error("the opener was called")
			}
		})
	}
}

func TestRunFreshSignInOpensAnHTTPSLink(t *testing.T) {
	var got string
	doc := &DiscoveryDocument{
		AuthorizationEndpoint: "https://kc.example.test/realms/meho/auth",
		TokenEndpoint:         "https://kc.example.test/realms/meho/token",
	}
	_, err := RunFreshSignIn(context.Background(), doc, "meho-cli", FreshSignInOptions{
		OpenBrowser: func(u string) error { got = u; return errors.New("no browser here") },
	})
	var nb *NoBrowserError
	if !errors.As(err, &nb) {
		t.Fatalf("err = %v, want *NoBrowserError", err)
	}
	if !strings.HasPrefix(got, "https://kc.example.test/realms/meho/auth?") {
		t.Errorf("opened %q, want the https sign-in link", got)
	}
}
