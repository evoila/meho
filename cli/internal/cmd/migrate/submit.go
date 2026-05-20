// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"charm.land/huh/v2"
	"charm.land/huh/v2/spinner"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/migrate"
	"github.com/evoila/meho/cli/internal/output"
)

// memoryPostBody is the request body POSTed to POST /api/v1/memory.
// source_id is the deduplication key on the server side — the G5.1
// upsert-by-source_id contract means that re-running with the same
// body is a no-op (updated_at unchanged) and with a changed body is
// an update (updated_at bumped). expires_at is deliberately omitted:
// the G5.2 server-side default-TTL injection owns it for user-scoped
// entries (#374).
type memoryPostBody struct {
	Scope    string         `json:"scope"`
	Slug     string         `json:"slug"`
	Body     string         `json:"body"`
	Metadata map[string]any `json:"metadata"`
	SourceID string         `json:"source_id"`
}

// submitResult tallies the outcome of a submitPlans call.
type submitResult struct {
	Migrated int
	Skipped  int
	Errored  int
	Retried  int
}

func (r submitResult) String() string {
	return fmt.Sprintf("Migrated: %d, Skipped: %d, Errored: %d (retried %d)",
		r.Migrated, r.Skipped, r.Errored, r.Retried)
}

const maxAutoRetry = 3

// runSpinnerFn is the spinner seam: tests replace it with accessible mode
// to avoid opening /dev/tty in a headless environment.
var runSpinnerFn = func(sp *spinner.Spinner) error { return sp.Run() }

// doSubmit orchestrates per-entry submission. The interactive error prompt runs
// outside the spinner to avoid concurrent tea programs sharing the same TTY.
func doSubmit(cmd *cobra.Command, backplaneOverride string, plans []migrate.SubmitPlan) error {
	backplaneURL, err := resolveBackplaneURL(backplaneOverride)
	if err != nil {
		return renderSubmitError(cmd, "", err)
	}

	nonInteractive, _ := cmd.Flags().GetBool("non-interactive")
	var res submitResult

	for i := range plans {
		p := plans[i]
		if err := postWithRetry(cmd, backplaneURL, p, nonInteractive, &res); err != nil {
			if errors.Is(err, errAborted) {
				break
			}
			fmt.Fprintln(cmd.OutOrStdout(), res.String())
			// Route the permanent error through renderSubmitError so the
			// exit code reflects the failure class (auth_expired=2 for
			// expired/missing/refresh-rejected tokens, unexpected=4 for
			// other non-2xx, unreachable=3 for transport errors). Without
			// this, every permanent error would exit 1 — same code as a
			// generic CLI error — and `meho migrate memory` in a cron job
			// would be indistinguishable from an unrelated CLI flag bug.
			return renderSubmitError(cmd, backplaneURL, err)
		}
	}

	fmt.Fprintln(cmd.OutOrStdout(), res.String())
	if res.Errored > 0 {
		noun := "entries"
		if res.Errored == 1 {
			noun = "entry"
		}
		return fmt.Errorf("meho migrate memory: %d %s failed to migrate", res.Errored, noun)
	}
	return nil
}

var errAborted = errors.New("migration aborted by user")

// postWithRetry submits one plan entry. In non-interactive mode it auto-retries
// on transient errors (up to maxAutoRetry attempts). In interactive mode, each
// attempt runs inside its own spinner; the Retry/Skip/Abort prompt is presented
// outside the spinner so no two tea programs run concurrently.
func postWithRetry(
	cmd *cobra.Command,
	backplaneURL string,
	plan migrate.SubmitPlan,
	nonInteractive bool,
	res *submitResult,
) error {
	body := memoryPostBody{
		Scope:    plan.Scope,
		Slug:     plan.Slug,
		Body:     plan.Body,
		Metadata: map[string]any{"tags": []string{}},
		SourceID: buildSourceID(plan),
	}
	raw, err := json.Marshal(body)
	if err != nil {
		res.Errored++
		return nil // marshal failure is a local bug, skip gracefully
	}

	attempt := 0
	for {
		attempt++
		var postErr error
		sp := spinner.New().
			Title(fmt.Sprintf("Migrating %q…", plan.Slug)).
			ActionWithErr(func(ctx context.Context) error {
				_, postErr = doAuthedRequest(ctx, backplaneURL, http.MethodPost, "/api/v1/memory", raw)
				return nil
			})
		if err := runSpinnerFn(sp); err != nil {
			return err
		}

		if postErr == nil {
			res.Migrated++
			return nil
		}

		if !isTransient(postErr) {
			res.Errored++
			// Bubble the raw error up — doSubmit routes it through
			// renderSubmitError for proper exit-code classification.
			// fmt.Errorf with %w preserves errors.As(_, *httpError) so
			// renderSubmitError can pick the right exit class.
			return fmt.Errorf("entry %s: %w", plan.Slug, postErr)
		}

		// Transient error: auto-retry in non-interactive mode.
		if nonInteractive {
			if attempt <= maxAutoRetry {
				res.Retried++
				continue
			}
			res.Errored++
			fmt.Fprintf(cmd.ErrOrStderr(),
				"meho migrate memory: skipping %s after %d retries: %v\n",
				plan.Slug, attempt, postErr)
			return nil
		}

		// Interactive mode: prompt runs outside the spinner (no concurrent tea programs).
		var choice string
		prompt := huh.NewSelect[string]().
			Title(fmt.Sprintf("Error migrating %q: %v", plan.Slug, postErr)).
			Options(
				huh.NewOption("Retry", "retry"),
				huh.NewOption("Skip", "skip"),
				huh.NewOption("Abort", "abort"),
			).
			Value(&choice)
		if runErr := huh.NewForm(huh.NewGroup(prompt)).Run(); runErr != nil {
			return errAborted
		}
		switch choice {
		case "retry":
			res.Retried++
			continue
		case "skip":
			res.Skipped++
			return nil
		default:
			return errAborted
		}
	}
}

// buildSourceID constructs the stable source_id for a plan's body hash.
// Matches the dry-run output from the sourceID function in memory.go.
func buildSourceID(plan migrate.SubmitPlan) string {
	prefix := plan.File.BodySHA256
	n := migrate.SourceIDPrefix
	if len(prefix) > n {
		prefix = prefix[:n]
	}
	return "laptop-migration/" + prefix
}

// isTransient returns true for errors that are worth retrying: network
// timeouts, 500/502/503/504 from the backplane.
func isTransient(err error) bool {
	var he *httpError
	if errors.As(err, &he) {
		switch he.StatusCode {
		case http.StatusInternalServerError,
			http.StatusBadGateway,
			http.StatusServiceUnavailable,
			http.StatusGatewayTimeout:
			return true
		}
		return false
	}
	// Transport errors (connection refused, timeout) are transient.
	return true
}

// ── In-package HTTP helper trio (copied from cli/internal/cmd/kb/kb.go) ──────
// This duplication is intentional: a shared helper would close an import
// cycle via cmd/root.go. Each cmd/* verb tree carries its own copy per
// the established convention (audit/, kb/, targets/, operation/ all do the
// same).

var errMissingAccessToken = errors.New("meho: stored token has no access_token")

type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

func resolveBackplaneURL(override string) (string, error) {
	if override != "" {
		return normaliseURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", &errNoBackplaneConfigured{inner: err}
		}
		return "", err
	}
	return normaliseURL(cfg.BackplaneURL)
}

func normaliseURL(s string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(s), "/")
	if trimmed == "" {
		return "", errors.New("backplane URL is empty")
	}
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid backplane URL %q: %w", s, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
}

func renderSubmitError(cmd *cobra.Command, backplaneURL string, err error) error {
	if errors.Is(err, errMissingAccessToken) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored credentials for %s are incomplete; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			false,
		)
	}
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			false,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			false,
		)
	}
	var he *httpError
	if errors.As(err, &he) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, he.StatusCode, he.Body)),
			false,
		)
	}
	var nbc *errNoBackplaneConfigured
	if errors.As(err, &nbc) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(nbc.Error()),
			false,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		false,
	)
}

const responseBodyCap int64 = 1 << 20

type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

func doAuthedRequest(
	ctx context.Context,
	backplaneURL, method, path string,
	body []byte,
) ([]byte, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	httpClient := authed.HTTPClient()
	bearer := authed.AccessToken()
	if bearer == "" {
		return nil, errMissingAccessToken
	}

	resp, err := sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close()
			return nil, rerr
		}
		resp.Body.Close()
		bearer = authed.AccessToken()
		resp, err = sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close()

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, responseBodyCap+1))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if int64(len(raw)) > responseBodyCap {
		return nil, fmt.Errorf(
			"response body exceeds %d-byte cap; refusing to decode possibly-truncated response",
			responseBodyCap,
		)
	}
	if resp.StatusCode == http.StatusNoContent || resp.StatusCode == http.StatusCreated {
		return raw, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

func sendRequest(
	ctx context.Context,
	client *http.Client,
	backplaneURL, method, path, bearer string,
	body []byte,
) (*http.Response, error) {
	fullURL := backplaneURL + path
	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, fullURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
}
