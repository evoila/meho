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
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"golang.org/x/oauth2"
)

// DiscoveryDocument is the subset of an OAuth 2.0 / OIDC discovery
// document the device-code flow needs. We deliberately decode only
// the fields we use — the spec permits arbitrary extra claims and we
// don't want to fail because a Keycloak release added a new key.
//
// Field names match RFC 8414 (OAuth 2.0 Authorization Server
// Metadata) and OpenID Connect Discovery 1.0; the JSON tags are the
// canonical names per those specs.
type DiscoveryDocument struct {
	// Issuer is the canonical realm URL the IdP self-identifies as.
	// We compare this against the issuer claim of the issued JWT to
	// reject mis-targeted tokens.
	Issuer string `json:"issuer"`
	// DeviceAuthorizationEndpoint is RFC 8628 §3.1 — where we POST
	// the device-code initiation. Keycloak puts this at
	// {realm}/protocol/openid-connect/auth/device.
	DeviceAuthorizationEndpoint string `json:"device_authorization_endpoint"`
	// TokenEndpoint is where we poll for the token. Keycloak puts
	// this at {realm}/protocol/openid-connect/token.
	TokenEndpoint string `json:"token_endpoint"`
}

// DiscoveryError wraps the failure modes of FetchDiscovery so
// callers can render a useful error without re-implementing the
// classification logic. Returned as a typed value (not a sentinel)
// because every failure carries situational context — the URL
// attempted, the HTTP status, the upstream body excerpt — that
// helps operators figure out which side of the seam is misbehaving.
type DiscoveryError struct {
	// URL is the discovery document URL we attempted.
	URL string
	// Status is the upstream HTTP status, when one came back.
	// Zero for transport-layer failures (connection refused, TLS
	// handshake failure).
	Status int
	// Underlying carries the wrapped lower-level error so callers
	// can errors.Is against it (e.g. context.DeadlineExceeded).
	Underlying error
	// Body excerpts the upstream response body for debugging.
	// Truncated to avoid spraying multi-megabyte HTML error pages
	// into operator terminals.
	Body string
}

func (e *DiscoveryError) Error() string {
	if e.Status != 0 {
		return fmt.Sprintf("meho: discovery %s returned HTTP %d: %s", e.URL, e.Status, e.Body)
	}
	return fmt.Sprintf("meho: discovery %s failed: %v", e.URL, e.Underlying)
}

func (e *DiscoveryError) Unwrap() error { return e.Underlying }

// FetchDiscovery loads the OAuth 2.0 / OIDC discovery document at the
// supplied URL. The URL should end in either
// .well-known/openid-configuration (OIDC) or
// .well-known/oauth-authorization-server (RFC 8414). Both shapes
// expose the device-authorization-endpoint field we depend on, so we
// don't differentiate at the call site.
//
// httpClient is injectable so tests can pass an httptest.Server's
// client; pass nil to use http.DefaultClient (which is what every
// production call site does).
func FetchDiscovery(ctx context.Context, httpClient *http.Client, discoveryURL string) (*DiscoveryDocument, error) {
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, discoveryURL, http.NoBody)
	if err != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Underlying: err}
	}
	// Discovery responses are JSON; some IdPs (Keycloak included)
	// 406-out without an explicit Accept header on certain
	// configurations.
	req.Header.Set("Accept", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Underlying: err}
	}
	defer func() { _ = resp.Body.Close() }()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if readErr != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Status: resp.StatusCode, Underlying: readErr}
	}
	if resp.StatusCode/100 != 2 {
		excerpt := string(body)
		// 256 chars is enough to identify a Keycloak error envelope
		// or an upstream proxy's HTML; anything longer is noise.
		if len(excerpt) > 256 {
			excerpt = excerpt[:256] + "…"
		}
		return nil, &DiscoveryError{URL: discoveryURL, Status: resp.StatusCode, Body: excerpt}
	}

	var doc DiscoveryDocument
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, &DiscoveryError{URL: discoveryURL, Status: resp.StatusCode, Underlying: fmt.Errorf("decode: %w", err)}
	}
	if doc.DeviceAuthorizationEndpoint == "" {
		return nil, &DiscoveryError{
			URL:        discoveryURL,
			Status:     resp.StatusCode,
			Underlying: errors.New("discovery document missing device_authorization_endpoint"),
		}
	}
	if doc.TokenEndpoint == "" {
		return nil, &DiscoveryError{
			URL:        discoveryURL,
			Status:     resp.StatusCode,
			Underlying: errors.New("discovery document missing token_endpoint"),
		}
	}
	return &doc, nil
}

// DiscoveryURLs returns the candidate well-known URLs to try in
// order for a given Keycloak realm root. Operators configure the
// CLI with the realm URL (e.g. https://kc.example.com/realms/meho);
// from that, the two standard well-known paths are mechanical
// derivations. We try OIDC first because it's the more common shape
// and Keycloak's primary advertisement; the RFC 8414 path is a
// belt-and-braces fallback for IdPs that don't expose the OIDC
// document but do publish the OAuth one.
func DiscoveryURLs(realmURL string) []string {
	base := strings.TrimRight(realmURL, "/")
	return []string{
		base + "/.well-known/openid-configuration",
		base + "/.well-known/oauth-authorization-server",
	}
}

// FetchDiscoveryFromRealm walks DiscoveryURLs in order, returning
// the first successful document. The combined error preserves every
// attempt's diagnostic so an operator can see exactly what failed
// and where — useful when a misconfigured realm URL produces a 404
// at one path and a 500 at the other.
func FetchDiscoveryFromRealm(ctx context.Context, httpClient *http.Client, realmURL string) (*DiscoveryDocument, error) {
	var errs []error
	for _, u := range DiscoveryURLs(realmURL) {
		doc, err := FetchDiscovery(ctx, httpClient, u)
		if err == nil {
			return doc, nil
		}
		errs = append(errs, err)
	}
	return nil, fmt.Errorf("meho: realm discovery failed: %w", errors.Join(errs...))
}

// LoginResult is the bundle a successful device-code login produces.
// Returned by RunDeviceFlow so the calling cobra command can both
// render the user-facing summary and pass the token through to the
// store without re-parsing oauth2's internals.
type LoginResult struct {
	// Token is the oauth2.Token issued by the IdP. We hold the
	// upstream type rather than copying its fields so future
	// refresh-token flows (v0.2) can use the standard
	// oauth2.TokenSource pattern without converting back.
	Token *oauth2.Token
	// Issuer is the realm URL the discovery document advertised.
	// Persisted alongside the token; see StoredToken.Issuer.
	Issuer string
}

// DeviceFlowPrompter is invoked once the device-code initiation
// succeeds so the caller can show the operator the user_code and
// verification URL. Injectable so tests don't have to scrape stdout
// and so a future "--browser" flag can open xdg-open from a
// concrete cobra-aware implementation. The function is called
// synchronously before polling begins; returning an error short-
// circuits the flow (e.g. operator pressed Ctrl-C).
type DeviceFlowPrompter func(ctx context.Context, resp *oauth2.DeviceAuthResponse) error

// DeviceFlowOptions configures a device-code login. Every field has
// a working zero value so callers only set what they need.
type DeviceFlowOptions struct {
	// HTTPClient is the http.Client both discovery and the oauth2
	// device flow use. Pass nil for http.DefaultClient. Set
	// explicitly in tests against an httptest.Server.
	HTTPClient *http.Client
	// Scopes requests specific OAuth scopes. Keycloak's device flow
	// defaults to "openid" — that's the minimum we need (gives us
	// the JWT). Operators / future ops can add more (offline_access
	// for refresh tokens, etc.).
	Scopes []string
	// Prompter renders the user_code / verification_uri to the
	// operator. Must be non-nil — the device flow is unusable
	// without one. The cobra command in cmd/login.go supplies a
	// stdout-writing implementation; tests pass an in-memory one.
	Prompter DeviceFlowPrompter
	// ParentContext is the caller's ambient context (`cmd.Context()`
	// in the cobra layer). It is **not** used to govern the polling
	// wait — that role belongs to `ctx`, which the caller should
	// build via NewDeviceFlowContext so a short ambient deadline
	// cannot truncate the operator's approval window. ParentContext
	// is consulted *only* in the error path: if the polling wait
	// ends with `context.DeadlineExceeded` and ParentContext has
	// also timed out, the returned error names the ambient deadline
	// explicitly instead of the generic IdP-shaped message.
	// Optional — leave nil if the caller has no separate parent
	// (tests, embedded uses).
	ParentContext context.Context
}

// RunDeviceFlow performs RFC 8628 against the supplied discovery
// document. The function blocks until the IdP returns a token, the
// device code expires, the user denies the grant, or the context is
// cancelled.
//
// The oauth2 package's DeviceAuth helper starts the flow. The
// polling is our own (pollDeviceToken), for two reasons:
//
//  1. Our config used to leave Endpoint.AuthStyle at zero
//     ("auto-detect"). The package then sent every pending check
//     twice — once with the client id in an Authorization header,
//     then again in the form body — and the second request drew a
//     slow_down answer on every check. Pinning AuthStyleInParams
//     alone fixes that part (refresh.go does exactly that).
//  2. The package's DeviceAccessToken loop cannot make a last check
//     just before the code's end, and it has no upper limit for the
//     wait after slow_down answers. Both are needed so an approval
//     late in the code's life is still seen.
func RunDeviceFlow(ctx context.Context, doc *DiscoveryDocument, clientID string, opts DeviceFlowOptions) (*LoginResult, error) {
	if opts.Prompter == nil {
		return nil, errors.New("meho: RunDeviceFlow requires a Prompter")
	}

	// Push the HTTPClient through the oauth2 context handle — that's
	// the package's documented mechanism for injecting an
	// http.Client. http.DefaultClient is fine when no override is
	// supplied; oauth2 falls back to it on its own when the context
	// key is absent, but we set it explicitly for symmetry with the
	// FetchDiscovery call earlier in the chain.
	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	flowCtx := context.WithValue(ctx, oauth2.HTTPClient, httpClient)

	scopes := opts.Scopes
	if len(scopes) == 0 {
		// "openid" is the minimum Keycloak's device-code flow
		// requires when the realm enforces OIDC semantics. Empty
		// scope lists trigger Keycloak to return an
		// invalid_scope error; rather than discover that
		// at runtime, we lock the default here.
		scopes = []string{"openid"}
	}

	cfg := oauth2.Config{
		ClientID: clientID,
		Endpoint: oauth2.Endpoint{
			DeviceAuthURL: doc.DeviceAuthorizationEndpoint,
			TokenURL:      doc.TokenEndpoint,
		},
		Scopes: scopes,
	}

	deviceResp, err := cfg.DeviceAuth(flowCtx)
	if err != nil {
		return nil, fmt.Errorf("meho: device auth initiation: %w", err)
	}

	if err := opts.Prompter(ctx, deviceResp); err != nil {
		return nil, err
	}

	tok, err := pollDeviceToken(ctx, httpClient, doc.TokenEndpoint, clientID, deviceResp)
	if err != nil {
		return nil, classifyDeviceTokenError(opts.ParentContext, err)
	}

	return &LoginResult{Token: tok, Issuer: doc.Issuer}, nil
}

// deviceCodeGrantType is the grant_type of the device access token
// request (RFC 8628 §3.4).
const deviceCodeGrantType = "urn:ietf:params:oauth:grant-type:device_code"

// defaultPollInterval is the wait between two checks when the server
// sends no interval (RFC 8628 §3.2).
const defaultPollInterval = 5 * time.Second

// Polling knobs. Package variables, not constants, so tests can
// shorten them and still exercise the real loop.
var (
	// slowDownStep is added to the wait after a slow_down answer, for
	// that check and all later ones (RFC 8628 §3.5).
	slowDownStep = 5 * time.Second
	// maxPollInterval caps how far slow_down answers can stretch the
	// wait. A server interval longer than this is honoured as is.
	maxPollInterval = 30 * time.Second
	// lastCheckMargin is how long before the code's end the final
	// check runs when the regular cadence would miss it.
	lastCheckMargin = 2 * time.Second
)

// errDeviceCodeExpired is returned when the code's lifetime ran out
// before the IdP approved it — either because the IdP said so
// (expired_token) or because our own last check before the end was
// still pending.
var errDeviceCodeExpired = errors.New("meho: device code expired before authorisation; rerun `meho login`")

// pollDeviceToken asks the token endpoint for the device-code grant
// until the IdP issues a token, refuses the grant, the code's
// lifetime ends, or ctx is done.
//
// One request per check, with client_id in the form body and no
// Authorization header: the CLI is a public client (RFC 8628 §3.4).
// The wait starts when an answer has arrived, so two checks never
// reach the server closer together than the wait. The wait follows
// §3.5: the server's interval first, plus slowDownStep after every
// slow_down, capped at maxPollInterval (or the server's interval, if
// longer). The last check before the code's end is planned ahead
// (see nextCheck) so that it, too, keeps the full wait to the check
// before it; the IdP answers slow_down to a check that comes sooner,
// which would hide an approval.
//
// The code's end is also a deadline for the sleeps and the requests,
// as it was in the oauth2 package's own loop. An answer that stalls
// past the end stops the login with the "device code expired"
// message, not with the outer PollTimeout message.
func pollDeviceToken(ctx context.Context, httpClient *http.Client, tokenURL, clientID string, da *oauth2.DeviceAuthResponse) (*oauth2.Token, error) {
	wait := defaultPollInterval
	if da.Interval > 0 {
		wait = time.Duration(da.Interval) * time.Second
	}
	maxWait := max(maxPollInterval, wait)

	pollCtx := ctx
	if !da.Expiry.IsZero() {
		var cancel context.CancelFunc
		pollCtx, cancel = context.WithDeadline(ctx, da.Expiry)
		defer cancel()
	}
	// codeEnded reports whether the code's own deadline, and not the
	// caller's context, ended the wait or the request.
	codeEnded := func() bool { return pollCtx.Err() != nil && ctx.Err() == nil }

	form := url.Values{
		"grant_type":  {deviceCodeGrantType},
		"device_code": {da.DeviceCode},
		"client_id":   {clientID},
	}

	first := true
	for {
		delay, last, ok := nextCheck(time.Now(), wait, first, da.Expiry)
		if !ok {
			return nil, errDeviceCodeExpired
		}
		if err := sleepCtx(pollCtx, delay); err != nil {
			if codeEnded() {
				return nil, errDeviceCodeExpired
			}
			return nil, err
		}
		first = false

		tok, err := requestDeviceToken(pollCtx, httpClient, tokenURL, form)
		if err == nil {
			return tok, nil
		}
		if codeEnded() {
			return nil, errDeviceCodeExpired
		}
		var retr *oauth2.RetrieveError
		if !errors.As(err, &retr) {
			return nil, err
		}
		switch retr.ErrorCode {
		case "slow_down":
			wait = min(wait+slowDownStep, maxWait)
		case "authorization_pending":
			// Not approved yet; check again after the wait.
		default:
			// access_denied, expired_token, or anything unexpected.
			return nil, err
		}
		if last {
			return nil, errDeviceCodeExpired
		}
	}
}

// nextCheck plans the next check. It returns how long to wait before
// it, whether it is the last one before the code's end, and false as
// the third value when no check fits before the end any more.
//
// A zero expiry means the server sent no expires_in; then the regular
// cadence continues until the caller's context ends. Otherwise the
// last check is meant to run lastCheckMargin before the end. The
// regular cadence goes on only while another full wait still fits
// between the next check and that moment; else the next check is the
// last one. The last check keeps the full wait to the check before
// it, even when that puts it inside the margin, because a check that
// comes sooner draws slow_down and hides the approval. first is true
// before the first check: the server has seen no request yet, so no
// minimum gap applies and the check may run at once.
func nextCheck(now time.Time, wait time.Duration, first bool, expiry time.Time) (time.Duration, bool, bool) {
	if expiry.IsZero() {
		return wait, false, true
	}
	lastCheckAt := expiry.Add(-lastCheckMargin)
	regular := now.Add(wait)
	if !regular.Add(wait).After(lastCheckAt) {
		return wait, false, true
	}
	earliest := regular
	if first {
		earliest = now
	}
	checkAt := lastCheckAt
	if earliest.After(checkAt) {
		checkAt = earliest
	}
	if !checkAt.Before(expiry) {
		return 0, true, false
	}
	return checkAt.Sub(now), true, true
}

// sleepCtx waits for d or until ctx is done, whichever comes first.
func sleepCtx(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// requestDeviceToken sends one device access token request (RFC 8628
// §3.4) and decodes the answer. A non-2xx answer becomes an
// *oauth2.RetrieveError carrying the RFC 6749 §5.2 error fields, so
// the loop and classifyDeviceTokenError read it the same way they
// read the oauth2 package's own errors.
func requestDeviceToken(ctx context.Context, httpClient *http.Client, tokenURL string, form url.Values) (*oauth2.Token, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("meho: read token response: %w", err)
	}
	if resp.StatusCode/100 != 2 {
		return nil, tokenErrorFromBody(resp, body)
	}
	return tokenFromBody(body)
}

func tokenErrorFromBody(resp *http.Response, body []byte) *oauth2.RetrieveError {
	retr := &oauth2.RetrieveError{Response: resp, Body: body}
	var payload struct {
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
		ErrorURI         string `json:"error_uri"`
	}
	if json.Unmarshal(body, &payload) == nil {
		retr.ErrorCode = payload.Error
		retr.ErrorDescription = payload.ErrorDescription
		retr.ErrorURI = payload.ErrorURI
	}
	return retr
}

// tokenFromBody decodes an RFC 6749 §5.1 JSON token response. Every
// field of the body is kept in the token's Extra map, so id_token
// (an OIDC addition) stays readable through Token.Extra as before.
func tokenFromBody(body []byte) (*oauth2.Token, error) {
	var payload struct {
		AccessToken  string `json:"access_token"`
		TokenType    string `json:"token_type"`
		RefreshToken string `json:"refresh_token"`
		ExpiresIn    int64  `json:"expires_in"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("meho: decode token response: %w", err)
	}
	if payload.AccessToken == "" {
		return nil, errors.New("meho: token response has no access_token")
	}
	var raw map[string]any
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, fmt.Errorf("meho: decode token response: %w", err)
	}
	tok := &oauth2.Token{
		AccessToken:  payload.AccessToken,
		TokenType:    payload.TokenType,
		RefreshToken: payload.RefreshToken,
	}
	if payload.ExpiresIn > 0 {
		tok.Expiry = time.Now().Add(time.Duration(payload.ExpiresIn) * time.Second)
	}
	return tok.WithExtra(raw), nil
}

// classifyDeviceTokenError unwraps the oauth2.RetrieveError the
// package returns when the IdP refuses the token. We surface the
// device-flow-specific error codes (expired_token, access_denied)
// as Go-native sentinels so the cobra command can render them with
// operator-friendly hints rather than dumping the raw upstream
// payload.
//
// parentCtx is the cobra `cmd.Context()` (i.e. the ambient process /
// CI / wrapper context). The polling wait runs on a context that's
// deliberately detached from parentCtx (see NewDeviceFlowContext) so
// a short ambient deadline can no longer truncate the operator's
// approval window. But if a non-IdP timeout fires (our own
// PollTimeout cap, or — for diagnostic value — an ambient deadline
// that was previously the failure mode), we want the operator to see
// which deadline was at fault rather than the generic
// `context deadline exceeded`. A nil parentCtx is treated the same
// as a non-cancelled parent.
func classifyDeviceTokenError(parentCtx context.Context, err error) error {
	if errors.Is(err, errDeviceCodeExpired) {
		return err
	}
	var retr *oauth2.RetrieveError
	if errors.As(err, &retr) {
		switch retr.ErrorCode {
		case "expired_token":
			return errDeviceCodeExpired
		case "access_denied":
			return fmt.Errorf("meho: authorisation denied by the operator at the verification URI")
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		// Our own PollTimeout cap fired (10m). The ambient parent
		// context having ALSO timed out is the historical
		// misattribution case — name it so the operator stops
		// chasing the IdP for a CI/wrapper timeout.
		if parentCtx != nil && errors.Is(parentCtx.Err(), context.DeadlineExceeded) {
			return fmt.Errorf("meho: login timed out waiting for approval; the parent process imposed a deadline shorter than the %s device-flow cap — rerun in a terminal without a wrapping timeout, or approve faster", PollTimeout)
		}
		return fmt.Errorf("meho: login timed out waiting for approval after %s; rerun and approve sooner, or pass a longer device-code lifetime via your IdP", PollTimeout)
	}
	if errors.Is(err, context.Canceled) {
		return fmt.Errorf("meho: login cancelled before approval (received SIGINT/SIGTERM or explicit cancel)")
	}
	return fmt.Errorf("meho: token exchange failed: %w", err)
}

// NewDeviceFlowContext returns the context that should govern the
// device-code approval wait, decoupled from the caller's ambient
// context (cobra's `cmd.Context()`, a CI job step, the wrapping
// shell's process group, the IDE's bash-tool timeout, …).
//
// Why detach. RFC 8628's device-code lifetime (`expires_in`) is
// typically minutes — Keycloak defaults to 600s. The whole point of
// the device-code flow is that the operator may be on a different
// machine, may walk to a kiosk, may need to fetch a hardware token.
// If the polling wait inherits the parent's deadline, a 30-second
// CI-step timeout (or any wrapper that imposed a similar bound)
// silently truncates the approval window and the operator sees
// `context deadline exceeded` — misattributed to the device code,
// which was still valid. The original consumer report (Initiative
// G0.9.1, Addendum II Wall #4) hit this twice; the fix is structural,
// not a value bump.
//
// What we keep:
//
//   - Context values from `parent` (e.g. `oauth2.HTTPClient`) ride
//     through via `context.WithoutCancel` — values propagate, the
//     parent's Done channel does not.
//   - SIGINT / SIGTERM still abort promptly via `signal.NotifyContext`
//     — a Ctrl-C at the terminal must not be ignored just because the
//     caller's process context was already detached.
//   - An absolute upper bound of `PollTimeout` (10 minutes) prevents
//     a wedged IdP from holding the CLI forever.
//
// What we drop:
//
//   - The parent's deadline. Whether the caller is `bash` with a
//     `timeout 30s` prefix, a CI step with a default 60s budget, or
//     a test harness with `context.WithTimeout`, the device-flow
//     wait now ignores it. The parent's deadline still applies to
//     network calls that ran before this point (discovery,
//     auth-config); only the interactive wait is exempt.
//   - The parent's `cancel` call. A caller that explicitly cancels
//     `parent` does not abort the device-flow wait. This is
//     deliberate — the device flow is uncancellable from outside the
//     terminal (the user has already been shown the verification
//     URL). For genuine cancellation, send SIGINT.
//
// The returned cancel func must always be called (use defer) — it
// releases both the signal handler and the PollTimeout timer.
func NewDeviceFlowContext(parent context.Context) (context.Context, context.CancelFunc) {
	// Step 1: shed the parent's Done channel but inherit its values.
	// Available since Go 1.21; semantics documented at
	// https://pkg.go.dev/context#WithoutCancel.
	detached := context.WithoutCancel(parent)

	// Step 2: re-attach a clean cancellation source rooted in OS
	// signals only. signal.NotifyContext registers the signals on
	// the returned context and stops the relay when the cancel func
	// runs, so we don't leak a signal handler across logins.
	signalCtx, stopSignals := signal.NotifyContext(detached, os.Interrupt, syscall.SIGTERM)

	// Step 3: cap the whole interactive wait at PollTimeout. Keeps
	// the worst case bounded if the IdP wedges and the operator
	// walks away. pollDeviceToken additionally stops at the code's
	// own end (`deviceResp.Expiry`), so this is the *outer* bound.
	flowCtx, cancelTimeout := context.WithTimeout(signalCtx, PollTimeout)

	// Compose both cleanup hooks into one cancel func. Callers only
	// have to defer the returned func and both inner resources
	// release. Order is timeout first (releases the timer), then
	// signal relay (releases the signal.Notify registration).
	return flowCtx, func() {
		cancelTimeout()
		stopSignals()
	}
}

// ConvertOAuthToken collapses an oauth2.Token into the StoredToken
// shape the persistence layer expects. Kept in its own function so
// the field mapping is in one place — adding (or renaming) a
// persisted field happens here and the call sites stay short.
func ConvertOAuthToken(tok *oauth2.Token, backplaneURL, issuer, clientID string) StoredToken {
	st := StoredToken{
		BackplaneURL: backplaneURL,
		Issuer:       issuer,
		ClientID:     clientID,
		AccessToken:  tok.AccessToken,
		RefreshToken: tok.RefreshToken,
		TokenType:    tok.TokenType,
		Expiry:       tok.Expiry,
	}
	// id_token rides in the oauth2.Token Extra() map — it's not a
	// first-class field because the OAuth 2.0 spec doesn't define it
	// (it's an OIDC extension). Lift it manually.
	if raw := tok.Extra("id_token"); raw != nil {
		if s, ok := raw.(string); ok {
			st.IDToken = s
		}
	}
	return st
}

// KeyForBackplane returns the (service, user) addressing the store uses
// for a given backplane URL. Centralised here so login and (future)
// status agree on the layout without copy-pasting the string format.
// Service stays at DefaultService; user is the backplane URL so
// multi-backplane support is a free upgrade once the rest of the CLI
// catches up.
func KeyForBackplane(backplaneURL string) (service, user string) {
	// Normalise the URL minimally — drop trailing slash so
	// "https://meho.example/" and "https://meho.example" collapse
	// to the same key. Anything more aggressive (case-folding the
	// host, stripping default ports) belongs in a v0.2
	// canonicalisation pass; keep it small for now.
	u, err := url.Parse(backplaneURL)
	if err != nil {
		// Unparseable URL: just trim slashes so we still get a
		// deterministic key. The caller will hit a real failure on
		// the actual HTTP request anyway.
		return DefaultService, strings.TrimRight(backplaneURL, "/")
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return DefaultService, u.String()
}

// PollTimeout is the default upper bound on the whole device-code
// dance — initiation plus polling plus token issuance. RFC 8628
// suggests 5 minutes as a typical expires_in; we double it so a
// distracted operator who walks away from their terminal still has
// a chance to come back and complete the flow. The cobra command
// wraps the context with this timeout so the bound is enforced
// regardless of what the IdP advertises in expires_in.
const PollTimeout = 10 * time.Minute
