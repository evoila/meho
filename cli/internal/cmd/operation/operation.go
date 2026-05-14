// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package operation hosts the cobra commands under `meho operation ...`
// for the G0.6 substrate's three meta-tool routes (Initiative #388).
// v0.2 ships:
//
//   - `meho operation groups <connector_id>` — list enabled
//     operation groups via GET /api/v1/operations/groups.
//   - `meho operation search <connector_id> "<query>" [--group K] [--limit N]`
//     — hybrid BM25 + cosine RRF over endpoint_descriptor rows via
//     GET /api/v1/operations/search.
//   - `meho operation call <connector_id> <op_id> --target <slug> [--params ...]`
//     — invoke the dispatcher via POST /api/v1/operations/call.
//
// Each verb wraps one backplane route and renders the response in
// either a human-readable table or `--json` mode. Authentication
// piggybacks on the token meho login wrote — api.NewAuthedClient
// handles the bearer injection + 401 refresh dance, same as
// `meho retrieval eval` and `meho status`.
//
// The fourth route `GET /api/v1/operations/{descriptor_id}` (tenant-
// admin diagnostic) is deferred — the DoD line for G0.6-T13 #481 was
// "three CLI verbs", not four.
package operation

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho operation` parent command. The
// command is grafted onto the top-level meho command tree by
// cmd/root.go alongside `meho retrieval`. The parent itself takes
// no args and prints its own help; every piece of behaviour lives
// in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "operation",
		Short:        "G0.6 operation meta-tool surface (groups / search / call)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newGroupsCmd())
	cmd.AddCommand(newSearchCmd())
	cmd.AddCommand(newCallCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" (→ auth_expired exit
// code 2 — the right fix is `meho login`) from URL-parse failures
// (→ unexpected exit code 4 — the right fix is correcting argv).
// Same shape as the helper in cli/internal/cmd/retrieval/eval.go;
// kept independent because the cmd/retrieval package can't be
// imported here without an import cycle (cmd/root.go grafts both
// packages onto the tree).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane re-implements the host-trimming + parsing rules
// the cmd package's resolveBackplaneURL applies. We can't import
// cmd from a subpackage without an import cycle (cmd/root.go grafts
// this package onto the tree), so the resolution shape is mirrored
// here — same shape as retrieval/eval.go's helper.
func resolveBackplane(override string) (string, error) {
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

// classifyBackplaneError maps a resolveBackplane error to the right
// output.StructuredError category. Identical contract to the
// retrieval/eval.go sibling: missing-config → auth_expired; everything
// else (parse errors, fs errors) → unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail
// fast on garbage input. Mirrors normalizeBackplaneURL in
// cmd/status.go (kept independent because of the import-cycle
// concern noted on resolveBackplane).
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

// renderRequestError translates an error from one of the per-verb
// request helpers into the right output.RenderError category.
// Same classification ladder as retrieval/eval.go: token-not-found
// and no-refresh-token surface as auth_expired with `meho login`
// hints; everything else is unreachable.
func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Returns the
// response body bytes (already drained) on a 2xx outcome, or an
// error categorised by api.IsTokenNotFound / api.IsNoRefreshToken /
// generic transport so renderRequestError can pick the right
// StructuredError category.
//
// Centralised here (rather than duplicated per verb) because all
// three operation verbs share the same auth + refresh pattern. The
// retrieval/eval.go sibling inlines an equivalent helper as
// postEval; this package factors it out so the three verbs stay
// small.
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
		return nil, errors.New("meho: stored token has no access_token")
	}

	resp, err := sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		// One-shot refresh + retry, mirroring api.AuthedClient.GetHealth
		// and retrieval.postEval.
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

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20)) // 1 MiB cap
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// httpError carries a non-2xx response so per-verb runners can
// render the right category (4xx → unexpected with body, 5xx →
// unreachable, etc.). Not an output.StructuredError directly — the
// renderer decides exit-code class based on status.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// sendRequest is the bottom of the stack: build the http.Request,
// stamp bearer + content headers, fire it. Split out so the
// 401-refresh-retry path in doAuthedRequest can reuse the same
// body bytes without re-marshalling.
func sendRequest(
	ctx context.Context,
	client *http.Client,
	backplaneURL, method, path, bearer string,
	body []byte,
) (*http.Response, error) {
	fullURL := backplaneURL + path
	var bodyReader io.Reader
	if body != nil {
		bodyReader = newBodyReader(body)
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

// newBodyReader wraps a fresh io.Reader around the request body
// bytes for each attempt (the 401-retry path needs a fresh reader,
// can't rewind the previous one). bytes.NewReader matches the
// retrieval/eval.go sibling's choice and avoids the string<->bytes
// round-trip strings.NewReader would impose.
func newBodyReader(body []byte) io.Reader {
	return bytes.NewReader(body)
}

// loadParamsFlag parses the --params flag value. Prefixing with '@'
// loads the named file as JSON; otherwise the value itself is parsed
// as inline JSON. Returns nil for an empty value so the caller can
// omit the "params" key from the JSON body.
func loadParamsFlag(val string) (map[string]any, error) {
	if val == "" {
		return nil, nil
	}
	var raw []byte
	if strings.HasPrefix(val, "@") {
		path := strings.TrimPrefix(val, "@")
		var err error
		raw, err = os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read params file %q: %w", path, err)
		}
	} else {
		raw = []byte(val)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse params JSON: %w", err)
	}
	return m, nil
}
