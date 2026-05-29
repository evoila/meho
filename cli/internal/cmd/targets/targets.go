// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package targets hosts the cobra commands under `meho targets ...`
// for Initiative #224's targets registry (G0.3-T5 / Task #256 +
// G0.3-T6 / Task #257). v0.2 ships three operator-facing read verbs
// plus a bulk-import migration tool:
//
//   - `meho targets list [--product P] [--json]` —
//     GET /api/v1/targets, keyset-paginated, optionally narrowed by
//     product slug.
//
//   - `meho targets describe <name|alias> [--json]` —
//     GET /api/v1/targets/{name}, alias-aware via the backend's
//     resolve_target. Renders the full Target read shape including
//     `fingerprint` + `preferred_impl_id` (added by G0.3-T1.5).
//
//   - `meho targets probe <name|alias> [--json]` —
//     POST /api/v1/targets/{name}/probe. The backend calls
//     Connector.fingerprint(), persists the FingerprintResult to
//     targets.fingerprint (so the G0.6 resolver can read it without
//     re-probing), and returns the same envelope to the caller. 501
//     when no connector is registered yet for the target's product.
//
//   - `meho targets import <file> [--update] [--dry-run] [--json]` —
//     bulk-import a `targets.yaml` file into the backplane via
//     POST/PATCH /api/v1/targets (Task #257, G0.3-T6). YAML key
//     `preferred_impl_id` maps to the top-level field; `fingerprint`
//     is skipped (server-managed; the backplane returns 422 on any
//     `fingerprint` value in TargetCreate / TargetUpdate bodies
//     via `model_config = ConfigDict(extra='forbid')`).
//
//   - `meho targets discover <product> [--seed-target <name>]
//     [--json]` — GET /api/v1/targets/discover (G9.1-T6 / Task #454,
//     the verb #256 explicitly deferred). Lists candidate targets
//     every connector registered for `<product>` can reach but that
//     are not yet registered; never auto-creates rows.
//
// G0.12-T14 #1272 migrated `list` / `describe` / `probe` / `discover`
// off hand-rolled HTTP onto the generated `api.ClientWithResponses`
// surface — verbs now consume `api.TargetSummary`, `api.Target`,
// `api.FingerprintResult`, `api.CandidateHint`, `api.SkippedConnector`,
// and `api.TargetsDiscoverResult` directly, kept in lock-step with the
// FastAPI Pydantic models by the `cli-api-snapshot-freshness` CI gate
// (Initiative #1118). `import` still owns local `httpDoer` /
// `doAuthedRequest` plumbing because its sparse-PATCH + extras-spill
// mapping reads YAML into untyped `map[string]any` bodies (see
// `import.go`'s `entryToCreateBody` / `entryToUpdateBody`), which the
// untyped POST/PATCH path serves more cleanly than coercing through
// `api.TargetCreate` / `api.TargetUpdate`. Each verb wraps one or
// more backplane routes and renders the response in either a human-
// readable form or `--json` mode. Authentication piggybacks on the
// token meho login wrote — same pattern as `meho operation` and
// `meho retrieval eval`.
//
// Write verbs (`create` / `update` / `delete`) are out of scope for
// v0.2 per the issue body — operators use `import --update` for
// bulk reconciliation.
package targets

import (
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
		Short:        "Operate the MEHO targets registry (list / describe / probe / import / discover)",
		Long:         "List, describe, probe, bulk-import, and discover candidate targets in the operator's tenant.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newDescribeCmd())
	cmd.AddCommand(newProbeCmd())
	cmd.AddCommand(newImportCmd())
	cmd.AddCommand(newDiscoverCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure that renderRequestError maps to
// auth_expired with a `meho login` hint. Mirrors the agent / approvals
// siblings' shape.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient for the supplied backplane
// URL and verifies a non-empty bearer is loaded. Centralised so every
// verb's typed-call path goes through the same "stored-token-loaded +
// non-empty bearer" gate; the caller forwards any returned error to
// renderRequestError for category mapping.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once, and if the typed response carries a
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors the
// behaviour `api.AuthedClient.GetHealth` implements for the
// /api/v1/health endpoint, generalised so every targets verb runs the
// same transparent-retry contract.
//
// statusOf reads the StatusCode off the typed response envelope (the
// generated *Response types expose StatusCode() through their embedded
// *http.Response). A nil response counts as "no retry" — the transport
// already failed and the caller surfaces err directly.
func retryOn401[R any](
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*R, error),
	statusOf func(*R) int,
) (*R, error) {
	resp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if resp == nil || statusOf(resp) != http.StatusUnauthorized {
		return resp, nil
	}
	if rerr := authed.Refresh(ctx); rerr != nil {
		return resp, rerr
	}
	return call(ctx)
}

// retryHTTPOn401 mirrors retryOn401 but for raw *http.Response calls.
// Used by verbs that bypass the generated `*WithResponse` parser when
// the endpoint declares a multi-shape 200 response (envelope=v2
// rollout) that the parser can't bind — see G0.16-T6 Finding A (#1312)
// notes in `list.go`'s `getTargets`. The function drains and closes
// the first response body before re-issuing the call on 401, so the
// caller always sees a single live response.
func retryHTTPOn401(
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*http.Response, error),
) (*http.Response, error) {
	resp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if resp == nil || resp.StatusCode != http.StatusUnauthorized {
		return resp, nil
	}
	// Drain + close the 401 body so the transport can reuse the
	// connection, then refresh + re-issue.
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if rerr := authed.Refresh(ctx); rerr != nil {
		// Surface the refresh failure; no second call.
		return nil, rerr
	}
	return call(ctx)
}

// renderRequestError translates an error from one of the per-verb
// request helpers into the right output.StructuredError category.
// Same classification ladder as the pre-G0.12 shape but acting on
// transport-layer errors only — non-2xx HTTP responses now arrive on
// the typed response envelope and route through renderHTTPStatus
// instead.
//
//   - empty stored bearer → auth_expired with a `meho login` hint.
//   - token never stored (auth.ErrTokenNotFound wrapper) → auth_expired
//     with a `meho login` hint.
//   - refresh impossible (errNoRefreshToken) → auth_expired.
//   - any other error → unreachable (network / transport failure
//     before the backplane responded).
func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if errors.Is(err, errMissingAccessToken) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored credentials for %s are incomplete; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
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

// renderHTTPStatus classifies a non-2xx response into the right
// StructuredError category. Lifted into its own helper so per-verb
// shims (probe wants the 501 message to point at G3) can wrap it.
// Acts on the (statusCode, body) pair the typed-client response
// envelope already buffered.
//
// The 401 case fires when the one-shot refresh inside retryOn401 ran
// and the re-issued call also came back 401 — the stored credentials
// are dead and the operator must rerun `meho login`.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	bodyStr := strings.TrimSpace(string(body))
	switch statusCode {
	case http.StatusUnauthorized:
		// The retryOn401 path already exhausted the refresh budget.
		// The token is dead; the operator must rerun `meho login`.
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
			output.InsufficientRole(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		// 404 carries either FastAPI's plain {"detail": "..."} or the
		// targets resolver's structured {"detail": {"error":
		// "no_target", "query": "...", "matches": [...]}}. Render
		// near-miss suggestions when the structured form lands so the
		// operator sees "did you mean rdc-vcenter? rdc-vsphere?".
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatNotFound(bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		// 409 from resolve_target = ambiguous_target. Surface the
		// colliding names so the operator can re-issue with the
		// disambiguated name.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatAmbiguous(bodyStr)),
			jsonOut,
		)
	case http.StatusNotImplemented:
		// 501 from probe_target = "no connector registered for
		// product=<X>". Pointer to G3 connector goals so operators
		// know where to look for the work.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatNoConnector(bodyStr)),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, statusCode, bodyStr)),
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
