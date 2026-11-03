// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"time"

	"golang.org/x/oauth2"
)

// FreshSignInTimeout is how long RunFreshSignIn waits for the person
// to finish the browser sign-in.
const FreshSignInTimeout = 5 * time.Minute

// NoBrowserError means no browser could be opened for the fresh
// sign-in. URL is the sign-in link, so the caller can print it.
type NoBrowserError struct {
	URL string
	Err error
}

func (e *NoBrowserError) Error() string {
	return fmt.Sprintf("meho: could not open a browser for the sign-in: %v", e.Err)
}

func (e *NoBrowserError) Unwrap() error { return e.Err }

// FreshSignInOptions configures RunFreshSignIn. Every field has a
// working zero value.
type FreshSignInOptions struct {
	// HTTPClient runs the code exchange. nil means http.DefaultClient.
	HTTPClient *http.Client
	// OpenBrowser opens the sign-in link. nil means OpenSystemBrowser.
	// Tests pass a fake that plays the browser.
	OpenBrowser func(url string) error
	// Timeout bounds the wait for the browser. Zero means
	// FreshSignInTimeout.
	Timeout time.Duration
}

// freshSignInConfig is the OAuth client of the fresh sign-in. The scope
// is openid only: no offline_access, so the sign-in never gives a
// long-lived login (#5515).
func freshSignInConfig(doc *DiscoveryDocument, clientID, redirectURL string) *oauth2.Config {
	return &oauth2.Config{
		ClientID: clientID,
		Endpoint: oauth2.Endpoint{
			AuthURL:   doc.AuthorizationEndpoint,
			TokenURL:  doc.TokenEndpoint,
			AuthStyle: oauth2.AuthStyleInParams,
		},
		RedirectURL: redirectURL,
		Scopes:      []string{"openid"},
	}
}

// RunFreshSignIn asks the person to sign in again in the browser and
// returns the new token. It uses the OIDC authorization code flow with
// PKCE and a loopback redirect (RFC 8252 §7.3) on 127.0.0.1 with a
// random port. The request carries prompt=login and max_age=0 (OIDC
// Core §3.1.2.1), so the identity provider asks for the credentials
// again even when the browser already has a session.
//
// The caller keeps the token in memory only and must not store it: the
// token proves a fresh sign-in for approvals (#3979, #5515).
func RunFreshSignIn(ctx context.Context, doc *DiscoveryDocument, clientID string, opts FreshSignInOptions) (*oauth2.Token, error) {
	if doc.AuthorizationEndpoint == "" {
		return nil, errors.New("meho: the identity provider has no authorization_endpoint")
	}
	timeout := opts.Timeout
	if timeout == 0 {
		timeout = FreshSignInTimeout
	}
	open := opts.OpenBrowser
	if open == nil {
		open = OpenSystemBrowser
	}
	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = http.DefaultClient
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, fmt.Errorf("meho: listen for the sign-in redirect: %w", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	cfg := freshSignInConfig(doc, clientID, fmt.Sprintf("http://127.0.0.1:%d/callback", port))
	state, err := randomState()
	if err != nil {
		_ = ln.Close()
		return nil, err
	}
	verifier := oauth2.GenerateVerifier()
	authURL := cfg.AuthCodeURL(state,
		oauth2.S256ChallengeOption(verifier),
		oauth2.SetAuthURLParam("prompt", "login"),
		oauth2.SetAuthURLParam("max_age", "0"),
	)

	results := make(chan callbackResult, 1)
	srv := &http.Server{
		Handler:           callbackHandler(state, results),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() { _ = srv.Serve(ln) }()
	defer func() { _ = srv.Close() }()

	if err := open(authURL); err != nil {
		return nil, &NoBrowserError{URL: authURL, Err: err}
	}

	waitCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var res callbackResult
	select {
	case res = <-results:
	case <-waitCtx.Done():
		return nil, fmt.Errorf("meho: the browser sign-in did not finish: %w", waitCtx.Err())
	}
	if res.err != nil {
		return nil, res.err
	}
	exchangeCtx := context.WithValue(ctx, oauth2.HTTPClient, httpClient)
	tok, err := cfg.Exchange(exchangeCtx, res.code, oauth2.VerifierOption(verifier))
	if err != nil {
		return nil, fmt.Errorf("meho: exchange the sign-in code: %w", err)
	}
	return tok, nil
}

// ErrFreshSignInExpired means the token of a fresh sign-in has run out
// and cannot be renewed, because the identity provider gave no refresh
// token. The person must sign in again in the browser.
var ErrFreshSignInExpired = errors.New("meho: the approval sign-in has run out; sign in again in the browser")

// FreshSignInTokenSource returns a token source that starts with tok, a
// token from RunFreshSignIn (#5515). The token stays in memory and is
// never stored.
//
// The approval client of MEHO's realm gives no refresh token: its access
// token lasts as long as the approval window. When that token runs out,
// Token returns ErrFreshSignInExpired. When the identity provider did
// give a refresh token, the source renews the token with it. The
// identity provider keeps the auth_time of the sign-in on a renewal, so
// renewing does not make the sign-in any newer.
func FreshSignInTokenSource(ctx context.Context, doc *DiscoveryDocument, clientID string, tok *oauth2.Token, httpClient *http.Client) oauth2.TokenSource {
	if tok.RefreshToken == "" {
		return noRenewalSource{tok: tok}
	}
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	ctx = context.WithValue(ctx, oauth2.HTTPClient, httpClient)
	return freshSignInConfig(doc, clientID, "").TokenSource(ctx, tok)
}

// noRenewalSource hands out one token until it runs out.
type noRenewalSource struct{ tok *oauth2.Token }

func (s noRenewalSource) Token() (*oauth2.Token, error) {
	if !s.tok.Valid() {
		return nil, ErrFreshSignInExpired
	}
	return s.tok, nil
}

type callbackResult struct {
	code string
	err  error
}

// callbackHandler answers the identity provider's redirect. It takes
// the first answer only and checks the state value against CSRF.
func callbackHandler(state string, results chan<- callbackResult) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/callback", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		var res callbackResult
		switch {
		case q.Get("state") != state:
			res.err = errors.New("meho: the sign-in answer has a wrong state value")
		case q.Get("error") != "":
			res.err = fmt.Errorf("meho: the sign-in failed: %s %s", q.Get("error"), q.Get("error_description"))
		case q.Get("code") == "":
			res.err = errors.New("meho: the sign-in answer has no code")
		default:
			res.code = q.Get("code")
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		if res.err != nil {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = fmt.Fprintln(w, "The sign-in did not work. Go back to the terminal.")
		} else {
			_, _ = fmt.Fprintln(w, "You are signed in. You can close this window and go back to the terminal.")
		}
		select {
		case results <- res:
		default:
		}
	})
	return mux
}

func randomState() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("meho: make sign-in state: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// launcherGrace is how long OpenSystemBrowser waits for the browser
// launcher to fail. Launchers such as xdg-open, open and rundll32 end
// within a moment when they cannot open a browser. Some keep running
// while the browser is open; after this time that counts as success.
const launcherGrace = 3 * time.Second

// OpenSystemBrowser opens url in the default browser. On Linux and
// other Unix systems without a graphical session (no DISPLAY and no
// WAYLAND_DISPLAY) it returns an error instead of trying. It also
// returns an error when the launcher starts but ends with an error, so
// the caller can print the link instead of waiting for a browser that
// never opened.
func OpenSystemBrowser(url string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		if os.Getenv("DISPLAY") == "" && os.Getenv("WAYLAND_DISPLAY") == "" {
			return errors.New("no graphical session")
		}
		cmd = exec.Command("xdg-open", url)
	}
	return runLauncher(cmd, launcherGrace)
}

// runLauncher starts cmd and waits up to grace for it to end. It
// returns an error when cmd cannot start or ends with an error within
// grace. A launcher still running after grace counts as success; a
// goroutine waits for it so it does not stay behind as a zombie.
func runLauncher(cmd *exec.Cmd, grace time.Duration) error {
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timer := time.NewTimer(grace)
	defer timer.Stop()
	select {
	case err := <-done:
		if err != nil {
			return fmt.Errorf("the browser launcher failed: %w", err)
		}
		return nil
	case <-timer.C:
		return nil
	}
}
