// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package targets hosts the cobra commands under `meho targets ...`
// for Initiative #224's targets registry (G0.3-T5 / Task #256). v0.2
// ships three operator-facing read verbs:
//
//   - `meho targets list [--product P] [--json]` —
//     GET /api/v1/targets, keyset-paginated, optionally narrowed by
//     product slug.
//   - `meho targets describe <name|alias> [--json]` —
//     GET /api/v1/targets/{name}, alias-aware via the backend's
//     resolve_target. Renders the full Target read shape including
//     `fingerprint` + `preferred_impl_id` (added by G0.3-T1.5).
//   - `meho targets probe <name|alias> [--json]` —
//     POST /api/v1/targets/{name}/probe. The backend calls
//     Connector.fingerprint(), persists the FingerprintResult to
//     targets.fingerprint (so the G0.6 resolver can read it without
//     re-probing), and returns the same envelope to the caller. 501
//     when no connector is registered yet for the target's product.
//
// Each verb wraps one backplane route and renders the response in
// either a human-readable table / key-value summary or `--json` mode.
// Authentication piggybacks on the token meho login wrote — same
// pattern as `meho operation` and `meho retrieval eval`.
//
// Write verbs (`create` / `update` / `delete`) are out of scope for
// v0.2 per the issue body. Bulk-import lives in a sibling task
// (G0.3-T6 / Task #257).
package targets

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

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho targets` parent command. The command
// is grafted onto the top-level meho command tree by cmd/root.go
// alongside `meho operation` and `meho retrieval`. The parent itself
// takes no args and prints its own help; every piece of behaviour
// lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "targets",
		Short:        "Operate the MEHO targets registry (list / describe / probe)",
		Long:         "List, describe, and probe targets registered in the operator's tenant.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newDescribeCmd())
	cmd.AddCommand(newProbeCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" (→ auth_expired exit
// code 2 — the right fix is `meho login`) from URL-parse failures
// (→ unexpected exit code 4 — the right fix is correcting argv).
// Same shape as the helper in cli/internal/cmd/operation/operation.go;
// kept independent because the cmd/operation package can't be
// imported here without an import cycle.
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane re-implements the host-trimming + parsing rules
// the cmd package's resolveBackplaneURL applies. We can't import cmd
// from a subpackage without an import cycle (cmd/root.go grafts this
// package onto the tree), so the resolution shape is mirrored here —
// same shape as operation/operation.go's helper.
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
// operation/operation.go sibling.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail
// fast on garbage input. Mirrors normaliseURL in operation/operation.go.
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
// request helpers into the right output.StructuredError category.
// Same classification ladder as operation/operation.go but with
// extra dispatch for the target-specific 4xx envelopes:
//
//   - 401 (refresh failed / no_refresh_token / token_not_found) →
//     auth_expired with a `meho login <url>` hint.
//   - 403 (RBAC denial) → insufficient_role; the backend's 403
//     detail string names the required role.
//   - 404 with detail.error == "no_target" → unexpected_response,
//     with the target query and any near-miss names surfaced in
//     the detail for operator scannability.
//   - 409 with detail.error == "ambiguous_target" → unexpected,
//     listing the colliding names so the operator can disambiguate.
//   - 501 → unexpected with the "no connector registered for
//     product=X yet" string the operator needs to resolve the gap
//     (file a Goal G3 connector task).
//   - Any other 4xx/5xx → unexpected with the raw body.
//   - Pure transport errors (timeouts, DNS, connection refused) →
//     unreachable.
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
	var he *httpError
	if errors.As(err, &he) {
		return renderHTTPError(cmd, backplaneURL, he, jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPError classifies a non-2xx response into the right
// StructuredError category. Lifted into its own helper so per-verb
// shims (probe wants the 501 message to point at G3) can wrap it.
func renderHTTPError(
	cmd *cobra.Command,
	backplaneURL string,
	he *httpError,
	jsonOut bool,
) error {
	switch he.StatusCode {
	case http.StatusUnauthorized:
		// The 401-refresh-retry path in doAuthedRequest already
		// exhausted the refresh budget. The token is dead; the
		// operator must rerun `meho login`.
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"backplane rejected the stored token; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	case http.StatusForbidden:
		// 403 detail is FastAPI/Starlette's HTTPException shape
		// ({"detail": "<string>"}). The backend's require_role helper
		// writes the required role into the detail string; pass it
		// through verbatim so the operator sees what role they need.
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusNotFound:
		// 404 carries either FastAPI's plain {"detail": "..."} or the
		// targets resolver's structured {"detail": {"error":
		// "no_target", "query": "...", "matches": [...]}}. Render
		// near-miss suggestions when the structured form lands so the
		// operator sees "did you mean rdc-vcenter? rdc-vsphere?".
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatNotFound(he.Body)),
			jsonOut,
		)
	case http.StatusConflict:
		// 409 from resolve_target = ambiguous_target. Surface the
		// colliding names so the operator can re-issue with the
		// disambiguated name.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatAmbiguous(he.Body)),
			jsonOut,
		)
	case http.StatusNotImplemented:
		// 501 from probe_target = "no connector registered for
		// product=<X>". Pointer to G3 connector goals so operators
		// know where to look for the work.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatNoConnector(he.Body)),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, he.StatusCode, he.Body)),
			jsonOut,
		)
	}
}

// detailEnvelope models FastAPI's HTTPException JSON shape, with the
// detail field accepting either a string (FastAPI's default) or the
// targets resolver's nested object ({"error", "query", "matches"}).
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// resolverDetail mirrors the structured detail TargetNotFoundError
// and AmbiguousTargetError raise (resolver.py L57-95).
type resolverDetail struct {
	Error   string                   `json:"error"`
	Query   string                   `json:"query"`
	Matches []resolverMatchOnTheWire `json:"matches"`
}

// resolverMatchOnTheWire mirrors TargetSummary.model_dump(mode='json')
// — only the human-readable fields the CLI surfaces. The id/host/
// product fields are present in the wire shape but the CLI summary
// renders only `name` (with aliases as a parenthesised hint).
type resolverMatchOnTheWire struct {
	Name    string   `json:"name"`
	Aliases []string `json:"aliases"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body when it's a plain string. Falls back to the raw body when the
// JSON shape doesn't match — better to surface the raw error than to
// swallow it.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var s string
		if jerr := json.Unmarshal(env.Detail, &s); jerr == nil && s != "" {
			return s
		}
	}
	return strings.TrimSpace(body)
}

// formatNotFound renders a 404 envelope into a single operator-
// readable line. Structured form (resolver) → "Target not found:
// <q>; did you mean: <a, b, c>"; plain string fallback → raw detail.
func formatNotFound(body string) string {
	if matches, query, ok := parseResolverDetail(body); ok {
		if len(matches) == 0 {
			return fmt.Sprintf("Target not found: %q (no near-misses)", query)
		}
		names := make([]string, 0, len(matches))
		for _, m := range matches {
			names = append(names, m.Name)
		}
		return fmt.Sprintf("Target not found: %q; did you mean: %s",
			query, strings.Join(names, ", "))
	}
	return "Target not found: " + decodeDetailString(body)
}

// formatAmbiguous renders a 409 envelope into a single line.
func formatAmbiguous(body string) string {
	if matches, query, ok := parseResolverDetail(body); ok {
		names := make([]string, 0, len(matches))
		for _, m := range matches {
			names = append(names, m.Name)
		}
		return fmt.Sprintf("Ambiguous query %q matches: %s",
			query, strings.Join(names, ", "))
	}
	return "Ambiguous query: " + decodeDetailString(body)
}

// formatNoConnector renders a 501 envelope. Detail looks like
// `no connector registered for product='vcenter'` — append a G3
// pointer so operators know where the connector work lives.
func formatNoConnector(body string) string {
	detail := decodeDetailString(body)
	return detail + " — connector work tracks under Goal G3 (per-product connectors); try again after the relevant connector lands"
}

// parseResolverDetail attempts to decode the targets resolver's
// structured detail. Returns (matches, query, true) on success;
// (_, _, false) when the body isn't the structured shape (FastAPI
// plain-string detail, or a totally different error envelope).
func parseResolverDetail(body string) ([]resolverMatchOnTheWire, string, bool) {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err != nil {
		return nil, "", false
	}
	var detail resolverDetail
	if err := json.Unmarshal(env.Detail, &detail); err != nil {
		return nil, "", false
	}
	if detail.Error == "" {
		return nil, "", false
	}
	return detail.Matches, detail.Query, true
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection + one-shot 401-refresh-retry. Returns the
// response body bytes (already drained) on a 2xx outcome, or an
// *httpError when the backplane returned a non-2xx, or an error
// categorised by api.IsTokenNotFound / api.IsNoRefreshToken / generic
// transport so renderRequestError can pick the right StructuredError
// category.
//
// Mirrors cli/internal/cmd/operation/operation.go::doAuthedRequest.
// Kept independent of the operation package for the import-cycle
// reason called out on resolveBackplane.
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
		// and operation/operation.go doAuthedRequest.
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
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// httpError carries a non-2xx response so per-verb runners can
// render the right category (404 → not-found with near-misses, 501 →
// no-connector pointer, etc.). Not an output.StructuredError directly
// — renderHTTPError decides exit-code class based on status.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// sendRequest is the bottom of the stack: build the http.Request,
// stamp bearer + content headers, fire it. Split out so the
// 401-refresh-retry path in doAuthedRequest can reuse the same body
// bytes without re-marshalling.
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

// pathEscape escapes a single path segment for use inside a backend
// URL. url.PathEscape escapes path-segment-unsafe characters
// (spaces, slashes, ?, #) without touching the unreserved set.
// Wrapped here to keep target verbs independent of net/url's
// import-spread.
func pathEscape(segment string) string {
	return url.PathEscape(segment)
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Rune-aware so multi-byte UTF-8 survives the
// cut. Same shape as the operation-package helper; kept here to
// avoid the import-cycle the cmd → cmd/operation → cmd path would
// create.
func truncate(s string, maxLen int) string {
	if maxLen < 1 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	return string(runes[:maxLen-1]) + "…"
}

// strDeref returns *s or empty string when s is nil. Backend Pydantic
// models declare many fields as Optional[str]; null lands as nil
// after JSON decode and the formatter consumes the empty string.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
