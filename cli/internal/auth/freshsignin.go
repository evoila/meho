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

// RunFreshSignIn asks the person to sign in again in the browser and
// returns the new token. It uses the OIDC authorization code flow with
// PKCE and a loopback redirect (RFC 8252 §7.3) on 127.0.0.1 with a
// random port. The request carries prompt=login and max_age=0 (OIDC
// Core §3.1.2.1), so the identity provider asks for the credentials
// again even when the browser already has a session.
//
// The caller uses the token for one request and must not store it:
// the token proves a fresh sign-in, and that proof is meant for one
// approval (#3979).
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
	cfg := &oauth2.Config{
		ClientID: clientID,
		Endpoint: oauth2.Endpoint{
			AuthURL:   doc.AuthorizationEndpoint,
			TokenURL:  doc.TokenEndpoint,
			AuthStyle: oauth2.AuthStyleInParams,
		},
		RedirectURL: fmt.Sprintf("http://127.0.0.1:%d/callback", port),
		Scopes:      []string{"openid"},
	}
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

// OpenSystemBrowser opens url in the default browser. On Linux and
// other Unix systems without a graphical session (no DISPLAY and no
// WAYLAND_DISPLAY) it returns an error instead of trying.
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
	return cmd.Start()
}
