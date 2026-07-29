// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package docs hosts the cobra commands under `meho docs ...` for
// G4.5-T5 (#1524) of Initiative #1518 (the meho-docs add-on), extended
// for collection-scoped search by G4.6-T3 (#1552). It ships one
// operator-facing verb that mirrors the `search_docs` MCP tool:
//
//   - `meho docs search <query> --collection <c> [--product <p>]
//     [--version <v>] [--limit N] [--json]` — collection-scoped
//     vendor-document retrieval via POST /api/v1/search_docs (T3,
//     #1552). Role: operator. --collection is the mandatory binary
//     scope (it routes to a backend and gates per-collection
//     entitlement); the CLI fails fast on a missing --collection
//     before the round-trip. --product / --version are optional
//     refinements within the collection.
//
// Gating — server-side only, CLI ↔ REST parity (#2109). The `meho
// docs` tree compiles into every CLI binary and is always visible; it
// carries no client-side capability pre-check. Access is decided by
// the backplane exactly as it is for `POST /api/v1/search_docs`: the
// per-collection `meho-docs:<collection>` entitlement is enforced
// server-side (a miss is a 403 `not_entitled` the CLI renders as
// `insufficient_role`). The CLI is a thin shell over the same route
// the REST surface exposes, so the same `(query, collection, tenant)`
// gets the same verdict on either surface.
//
// Why no client-side capability gate: an earlier shape read the bare
// `meho-docs` capability out of the stored JWT and hid / refused the
// whole tree when it was absent (option B). That gate had no
// counterpart on the REST route (which never checks the bare
// capability — only the per-collection one), so the two surfaces
// diverged: a tenant entitled to a collection via REST could still hit
// `addon_not_provisioned` on the CLI. The operator decision on #2109
// was option A — reconcile to one server-gated op — so the client-side
// pre-check is gone and the server is the single gate. A forged claim
// never mattered here anyway: the CLI gate was only a visibility
// affordance; the backplane RBAC + corpus federation always enforced
// the real boundary.
//
// Like the sibling kb verb tree (G0.12-T9 #1267), every call drives
// the generated `api.ClientWithResponses` surface directly:
// `api.NewAuthedClient` wires the bearer + lazy 401-refresh editor,
// and the verb consumes the generated `api.SearchDocsRequest`,
// `api.SearchDocsResponse`, and `api.DocsChunk` types — no
// consumer-side copies of the backend pydantic models.
package docs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho docs` parent command. The command tree
// compiles into every CLI binary and is always visible: there is no
// client-side capability gate (#2109). Access is decided server-side
// by the backplane, identically to `POST /api/v1/search_docs` — the
// per-collection `meho-docs:<collection>` entitlement is enforced on
// the route, so an unentitled collection surfaces as a 403 the CLI
// renders as `insufficient_role`, not a client-side refusal.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "docs",
		Short: "Search the meho-docs vendor-document add-on",
		Long: "Operate the meho-docs add-on — backplane-federated " +
			"retrieval over the external vendor-document corpus. Search " +
			"routes through the backplane so every query is audited and " +
			"the mandatory collection scope + per-collection entitlement " +
			"are enforced centrally, server-side. Access is gated by the " +
			"per-collection `meho-docs:<collection>` capability the " +
			"backplane checks on every request — the same gate " +
			"`POST /api/v1/search_docs` applies — so a collection you " +
			"cannot search returns a typed 403 rather than a silent or " +
			"divergent client-side refusal.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSearchCmd())
	cmd.AddCommand(newCollectionsCmd())
	return cmd
}

// errMissingAccessToken mirrors the kb verb tree's sentinel: the
// stored token row exists but its access_token field is empty. A
// credential-state failure (auth_expired, exit 2) rather than a
// transport failure (unreachable, exit 3).
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the docs transport reads off any
// backplane response before surfacing `*http.MaxBytesError`. 1 MiB is
// generous for a search_docs response: the backend caps `limit` at 50
// and each DocsChunk is a single corpus chunk (content + citation +
// score). Without the cap an adversarial or runaway backplane could
// OOM the CLI because the generated `Parse*Response` helpers call
// `io.ReadAll(rsp.Body)` before constructing the typed envelope.
const responseBodyCap int64 = 1 << 20

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Mirrors
// the kb verb tree's helper (same stored-token + non-empty-bearer
// gate, same transport-layer response-body cap) so the docs verbs
// share one auth path.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{
		ResponseBodyLimit: responseBodyCap,
	})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once and, on a 401, runs a one-shot bearer
// refresh and re-issues call. Identical contract to the kb verb
// tree's helper, generalised over the typed response envelope.
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

// renderRequestError translates a transport-layer request error into
// the right output.StructuredError category. Same mapping the kb
// verb tree uses: missing bearer / no-refresh-token / token-not-found
// route to auth_expired (exit 2); body-cap and JSON-shape failures
// route to unexpected_response (exit 4, a server-side contract
// failure, not a transport-down failure); everything else is
// unreachable (exit 3).
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
	var maxBytesErr *http.MaxBytesError
	var syntaxErr *json.SyntaxError
	var unmarshalErr *json.UnmarshalTypeError
	if errors.As(err, &maxBytesErr) ||
		errors.As(err, &syntaxErr) ||
		errors.As(err, &unmarshalErr) ||
		errors.Is(err, io.ErrUnexpectedEOF) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: %v", backplaneURL, err)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPStatus classifies a non-2xx search_docs response carried
// in the typed envelope. The status set search_docs can return (per
// the T3 route contract):
//
//   - 401 → auth_expired (token rejected / refresh impossible).
//   - 403 → insufficient_role (read_only operator, OR the tenant is not
//     entitled to the named collection — it lacks the
//     `meho-docs:<collection>` capability), UNLESS the body carries the
//     structured `detail.error == "collection_disabled"` marker, in which
//     case it is unexpected wrapping a "collection is disabled" detail (an
//     operator hid the collection — a terminal readiness rejection, not a
//     role/entitlement miss).
//   - 409 → unexpected wrapping the not-ready detail (the collection is
//     known + entitled but transiently provisioning / rebuilding — a
//     disabled collection is the terminal 403 above, not a 409).
//   - 422 → unexpected wrapping the collection-scope detail (missing
//     --collection — the CLI fails fast before the call, so a 422 here
//     means an unknown collection or a contract drift worth surfacing)
//     or a too-long query / out-of-range limit.
//   - 503 → unexpected with the backend-unavailable detail (the
//     collection's backend is unconfigured / unreachable / non-2xx).
//   - Other non-2xx → unexpected with the raw body.
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
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"backplane rejected the stored token; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	case http.StatusForbidden:
		if detailErrorMarker(bodyStr) == "collection_disabled" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"the doc collection is disabled: %s",
					decodeDetailString(bodyStr),
				)),
				jsonOut,
			)
		}
		if detailErrorMarker(bodyStr) == "global_collection" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"the doc collection is global (platform-owned) and cannot be "+
						"deleted by a tenant admin: %s",
					decodeDetailString(bodyStr),
				)),
				jsonOut,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", decodeDetailString(bodyStr))),
			jsonOut,
		)
	case http.StatusConflict:
		if detailErrorMarker(bodyStr) == "collection_not_disabled" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"the doc collection is not disabled; disable it before "+
						"deleting: %s",
					decodeDetailString(bodyStr),
				)),
				jsonOut,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"the doc collection is not ready: %s",
				decodeDetailString(bodyStr),
			)),
			jsonOut,
		)
	case http.StatusServiceUnavailable:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"the collection's backend is unavailable: %s",
				decodeDetailString(bodyStr),
			)),
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

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body when it's a plain string, falling back to the trimmed raw body
// when the JSON shape doesn't match (non-JSON body or a structured
// `detail` such as the FastAPI validation list). Mirrors the kb verb
// tree's helper.
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

// detailErrorMarker returns the structured `detail.error` marker a docs
// route attaches to a typed rejection (e.g. `collection_disabled` on a
// disabled-collection search 403, `global_collection` on a delete of a
// platform-owned row, `collection_not_disabled` on a delete of a
// still-enabled collection), or "" when the body carries no such marker.
// It lets the shared HTTP-status renderer surface each typed rejection
// distinctly rather than as a generic `insufficient_role` / not-ready
// message. A plain-string `detail` (the entitlement-miss 403, a FastAPI
// validation list) never matches, so the caller falls through to the
// default rendering.
func detailErrorMarker(body string) string {
	var outer detailEnvelope
	if err := json.Unmarshal([]byte(body), &outer); err != nil {
		return ""
	}
	var inner struct {
		Error string `json:"error"`
	}
	if err := json.Unmarshal(outer.Detail, &inner); err != nil {
		return ""
	}
	return inner.Error
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Rune-aware so multi-byte UTF-8 survives the
// cut. Same shape as the sibling-package helpers.
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
