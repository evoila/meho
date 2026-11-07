// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"

	"golang.org/x/oauth2"
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

// TestFetchDiscoveryRefusesHTTPOnARoutedHost checks that the discovery
// address is checked before the request: no request goes out.
func TestFetchDiscoveryRefusesHTTPOnARoutedHost(t *testing.T) {
	realm := "http://kc.example.test/realms/meho"
	transport := &docTransport{doc: realmDoc(realm)}
	_, err := FetchDiscovery(context.Background(), &http.Client{Transport: transport}, realm+oidcWellKnownPath)
	if err == nil || !strings.Contains(err.Error(), "https") {
		t.Fatalf("http realm on a routed host: err = %v, want an https error", err)
	}
	if transport.hits != 0 {
		t.Errorf("hits = %d, want 0 (the address is checked before the request)", transport.hits)
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

// routeTransport answers each request by its address (scheme, host
// and path) and records every address it sees, so a test can show
// that an address never received a request.
type routeTransport struct {
	routes map[string]func(r *http.Request) *http.Response
	seen   []string
}

func (t *routeTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	key := r.URL.Scheme + "://" + r.URL.Host + r.URL.Path
	t.seen = append(t.seen, key)
	route, ok := t.routes[key]
	if !ok {
		return nil, fmt.Errorf("no route for %s", key)
	}
	resp := route(r)
	resp.Request = r
	return resp, nil
}

func redirectTo(status int, target string) func(*http.Request) *http.Response {
	return func(*http.Request) *http.Response {
		return &http.Response{
			StatusCode: status,
			Header:     http.Header{"Location": []string{target}},
			Body:       http.NoBody,
		}
	}
}

func jsonAnswer(v any) func(*http.Request) *http.Response {
	return func(*http.Request) *http.Response {
		body, _ := json.Marshal(v)
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(string(body))),
		}
	}
}

// redirectStatuses are the answers net/http follows as redirects.
var redirectStatuses = []int{
	http.StatusMovedPermanently,
	http.StatusFound,
	http.StatusSeeOther,
	http.StatusTemporaryRedirect,
	http.StatusPermanentRedirect,
}

func TestFetchDiscoveryRefusesARedirectToHTTP(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	from := realm + oidcWellKnownPath
	to := "http://kc.example.test/realms/meho" + oidcWellKnownPath
	for _, status := range redirectStatuses {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			transport := &routeTransport{routes: map[string]func(*http.Request) *http.Response{
				from: redirectTo(status, to),
				to:   jsonAnswer(realmDoc(realm)),
			}}
			_, err := FetchDiscovery(context.Background(), &http.Client{Transport: transport}, from)
			if err == nil || !strings.Contains(err.Error(), "https") {
				t.Fatalf("err = %v, want an https error", err)
			}
			if slices.Contains(transport.seen, to) {
				t.Errorf("the http address received a request: %v", transport.seen)
			}
		})
	}
}

func TestFetchDiscoveryFollowsARedirectToASafeAddress(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	from := realm + oidcWellKnownPath
	for name, to := range map[string]string{
		"https":         "https://kc.example.test/auth/realms/meho" + oidcWellKnownPath,
		"http loopback": "http://127.0.0.1:8080/realms/meho" + oidcWellKnownPath,
	} {
		t.Run(name, func(t *testing.T) {
			transport := &routeTransport{routes: map[string]func(*http.Request) *http.Response{
				from: redirectTo(http.StatusFound, to),
				to:   jsonAnswer(realmDoc(realm)),
			}}
			doc, err := FetchDiscovery(context.Background(), &http.Client{Transport: transport}, from)
			if err != nil {
				t.Fatalf("FetchDiscovery: %v", err)
			}
			if doc.Issuer != realm {
				t.Errorf("issuer = %q, want %q", doc.Issuer, realm)
			}
			if !slices.Equal(transport.seen, []string{from, to}) {
				t.Errorf("requests = %v, want the redirect followed once", transport.seen)
			}
		})
	}
}

func TestWithRedirectCheck(t *testing.T) {
	t.Run("nil client copies the default client", func(t *testing.T) {
		c := WithRedirectCheck(nil)
		if c == http.DefaultClient || c.CheckRedirect == nil {
			t.Fatal("want a copy of http.DefaultClient with a redirect check")
		}
		if http.DefaultClient.CheckRedirect != nil {
			t.Error("http.DefaultClient was changed")
		}
	})
	t.Run("keeps the transport and stops after ten redirects", func(t *testing.T) {
		loop := "https://kc.example.test/loop"
		transport := &routeTransport{routes: map[string]func(*http.Request) *http.Response{
			loop: redirectTo(http.StatusFound, loop),
		}}
		c := WithRedirectCheck(&http.Client{Transport: transport})
		resp, err := c.Get(loop)
		if err == nil {
			_ = resp.Body.Close()
			t.Fatal("want an error after ten redirects")
		}
		if !strings.Contains(err.Error(), "10 redirects") {
			t.Errorf("err = %v, want the redirect limit", err)
		}
		if len(transport.seen) != maxRedirects {
			t.Errorf("requests = %d, want %d", len(transport.seen), maxRedirects)
		}
	})
	t.Run("a redirect policy of the client runs after the check", func(t *testing.T) {
		from := "https://kc.example.test/from"
		transport := &routeTransport{routes: map[string]func(*http.Request) *http.Response{
			from: redirectTo(http.StatusFound, "https://kc.example.test/to"),
		}}
		c := WithRedirectCheck(&http.Client{
			Transport:     transport,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		})
		resp, err := c.Get(from)
		if err != nil {
			t.Fatalf("Get: %v", err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusFound || len(transport.seen) != 1 {
			t.Errorf("status = %d, requests = %v; want the redirect answer itself", resp.StatusCode, transport.seen)
		}
		transport.routes[from] = redirectTo(http.StatusFound, "http://kc.example.test/to")
		resp, err = c.Get(from)
		if err == nil {
			_ = resp.Body.Close()
			t.Fatal("want an error for a redirect to http, before the client's own policy")
		}
	})
}

// TestRunDeviceFlowRefusesARedirectToHTTP checks the device-code
// request and the token request: a redirect to plain http on a routed
// host ends the login, and the http address receives no request.
func TestRunDeviceFlowRefusesARedirectToHTTP(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	doc := &DiscoveryDocument{
		Issuer:                      realm,
		DeviceAuthorizationEndpoint: realm + "/protocol/openid-connect/auth/device",
		TokenEndpoint:               realm + "/protocol/openid-connect/token",
	}
	deviceAnswer := jsonAnswer(map[string]any{
		"device_code": "dc", "user_code": "ABCD-EFGH",
		"verification_uri": realm + "/device", "expires_in": 600, "interval": 1,
	})
	tokenAnswer := jsonAnswer(map[string]any{"access_token": "at", "token_type": "Bearer", "expires_in": 300})
	for name, redirecting := range map[string]string{
		"device request": doc.DeviceAuthorizationEndpoint,
		"token request":  doc.TokenEndpoint,
	} {
		t.Run(name, func(t *testing.T) {
			to := "http://" + strings.TrimPrefix(redirecting, "https://")
			transport := &routeTransport{routes: map[string]func(*http.Request) *http.Response{
				doc.DeviceAuthorizationEndpoint: deviceAnswer,
				doc.TokenEndpoint:               tokenAnswer,
				to:                              tokenAnswer,
			}}
			transport.routes[redirecting] = redirectTo(http.StatusTemporaryRedirect, to)
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_, err := RunDeviceFlow(ctx, doc, "meho-cli", DeviceFlowOptions{
				HTTPClient: &http.Client{Transport: transport},
				Prompter:   func(context.Context, *oauth2.DeviceAuthResponse) error { return nil },
			})
			if err == nil || !strings.Contains(err.Error(), "https") {
				t.Fatalf("err = %v, want an https error", err)
			}
			if slices.Contains(transport.seen, to) {
				t.Errorf("the http address received a request: %v", transport.seen)
			}
		})
	}
}

// TestRunFreshSignInRefusesARedirectToHTTP checks the code exchange of
// the browser sign-in.
func TestRunFreshSignInRefusesARedirectToHTTP(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	doc := &DiscoveryDocument{
		Issuer:                realm,
		AuthorizationEndpoint: realm + "/protocol/openid-connect/auth",
		TokenEndpoint:         realm + "/protocol/openid-connect/token",
	}
	to := "http://kc.example.test/realms/meho/protocol/openid-connect/token"
	transport := &routeTransport{routes: map[string]func(*http.Request) *http.Response{
		doc.TokenEndpoint: redirectTo(http.StatusTemporaryRedirect, to),
		to:                jsonAnswer(map[string]any{"access_token": "at", "token_type": "Bearer", "expires_in": 300}),
	}}
	open := func(link string) error {
		u, err := url.Parse(link)
		if err != nil {
			return err
		}
		q := u.Query()
		go func() {
			resp, err := http.Get(q.Get("redirect_uri") + "?code=the-code&state=" + url.QueryEscape(q.Get("state")))
			if err == nil {
				_ = resp.Body.Close()
			}
		}()
		return nil
	}
	_, err := RunFreshSignIn(context.Background(), doc, "meho-cli", FreshSignInOptions{
		HTTPClient:  &http.Client{Transport: transport},
		OpenBrowser: open,
		Timeout:     10 * time.Second,
	})
	if err == nil || !strings.Contains(err.Error(), "https") {
		t.Fatalf("err = %v, want an https error", err)
	}
	if slices.Contains(transport.seen, to) {
		t.Errorf("the http address received a request: %v", transport.seen)
	}
}

// TestFreshSignInTokenSourceRefusesARedirectToHTTP checks the renewal
// of an approval sign-in token.
func TestFreshSignInTokenSourceRefusesARedirectToHTTP(t *testing.T) {
	realm := "https://kc.example.test/realms/meho"
	doc := &DiscoveryDocument{Issuer: realm, TokenEndpoint: realm + "/protocol/openid-connect/token"}
	to := "http://kc.example.test/realms/meho/protocol/openid-connect/token"
	transport := &routeTransport{routes: map[string]func(*http.Request) *http.Response{
		doc.TokenEndpoint: redirectTo(http.StatusTemporaryRedirect, to),
		to:                jsonAnswer(map[string]any{"access_token": "at", "token_type": "Bearer", "expires_in": 300}),
	}}
	expired := &oauth2.Token{AccessToken: "first", RefreshToken: "r1", Expiry: time.Now().Add(-time.Minute)}
	_, err := FreshSignInTokenSource(context.Background(), doc, "meho-cli-approve", expired, &http.Client{Transport: transport}).Token()
	if err == nil || !strings.Contains(err.Error(), "https") {
		t.Fatalf("err = %v, want an https error", err)
	}
	if slices.Contains(transport.seen, to) {
		t.Errorf("the http address received a request: %v", transport.seen)
	}
}
