// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package retrieval hosts the cobra commands under `meho retrieval ...`
// for the G4.3 retrieval-quality / migration-decision tooling
// (Initiative #373). v0.2 ships:
//
//   - `meho retrieval eval` — corpus-driven precision@5 / MRR /
//     coverage report against /api/v1/retrieve/eval (T2 #441).
//   - `meho retrieval usage` — audit-log-backed daily-use telemetry
//     (T5b #464) against /api/v1/retrieve/usage (T5 #444).
//   - `meho retrieval retire-checklist` — combined retire-decision
//     verb (T6 #445).
//
// G0.12-T12 #1270 migrated this package off the per-verb hand-rolled
// `postXxxWithBearer` helpers + hand-typed copies of backend pydantic
// models. Every verb here drives the generated `api.ClientWithResponses`
// surface directly: `api.NewAuthedClient` wires the bearer + lazy
// 401-refresh editor onto the embedded `ClientWithResponses`, and the
// verbs call the typed `*WithResponse` methods
// (`EvalEndpointApiV1RetrieveEvalPostWithResponse` etc.). Consumer-side
// struct drift — the #1069 root cause Initiative #1118 targets —
// can't recur because we now consume `api.EvalResult`,
// `api.UsageReport`, `api.RetireChecklistReport`, and friends
// directly.
//
// Exception: `ghIssueLabel` and `ghIssue` in retire_checklist.go stay
// hand-typed. Those describe the JSON shape returned by the `gh issue
// list --json number,labels` subprocess, not a meho backplane API —
// they're consumed by `lookupBlockerCounts` running gh as a child
// process and have no place in the openapi.json snapshot.
package retrieval

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

// NewRootCmd returns the `meho retrieval` parent command. The
// command is grafted onto the top-level meho command tree by
// cmd/root.go.
//
// The parent itself takes no args and prints its own help; every
// piece of behaviour lives in the per-subcommand RunE closures.
// Callers add new G4.3 verbs by appending to the AddCommand list
// inside this constructor.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "retrieval",
		Short:        "Retrieval-quality + migration-decision tooling (G4.3 #373)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newEvalCmd())
	cmd.AddCommand(newUsageCmd())
	cmd.AddCommand(newRetireChecklistCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its `access_token` field is empty.
// It's a credential-state failure rather than a transport failure,
// so renderRequestError maps it to auth_expired (exit 2) with a
// `meho login` hint — not unreachable (exit 3). Mirrors the shape
// adopted by the sibling typed-client migrations on Initiative
// #1118 (T1 #1251 approvals, T4 #1262 agent-principal, T9 #1267
// kb).
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the retrieval verb tree's
// transport will read off any backplane response body before
// surfacing `*http.MaxBytesError`. 1 MiB is generous for every
// documented retrieval payload (eval reports cap at three surfaces
// × ~25 queries × small per-query records; usage reports cap at a
// 90-day window × three surfaces ≈ ~270 buckets × ~80-byte rows;
// retire-checklist reports cap at three surfaces × five criteria).
// Without the cap, an adversarial or runaway backplane response
// could OOM the CLI because the generated `Parse*Response` helpers
// call `io.ReadAll(rsp.Body)` on an unbounded body before
// constructing the typed envelope.
//
// The cap is installed at the transport layer via the inline
// `capRoundTripper` below so it applies uniformly to every typed
// verb on the same `AuthedClient`. The kb sibling (#1282) installs
// the same cap via `api.AuthedClientOptions.ResponseBodyLimit`; we
// duplicate the wrapper locally rather than reach into
// `cli/internal/api/client.go` to keep this PR's blast radius
// inside the retrieval verb tree (the shared options struct will
// land separately when the sibling initiative settles).
const responseBodyCap int64 = 1 << 20

// capRoundTripper wraps an http.RoundTripper so every response body
// is re-bound to an http.MaxBytesReader before the typed-client
// parsers (oapi-codegen's generated `Parse*Response` helpers, which
// `io.ReadAll(rsp.Body)` to populate `*Response.Body []byte`) get a
// chance to drain it. A read at or past `limit` surfaces as
// `*http.MaxBytesError`, which `renderRequestError` maps to
// `output.Unexpected` (exit 4 — `unexpected_response`) rather than
// `output.Unreachable` (exit 3 — `network_unreachable`).
//
// The `*http.MaxBytesError` shape was added in Go 1.19 and is the
// canonical signal for "transport refused to read past N bytes."
// The wrapper applies the cap server-wide on the underlying
// transport so every typed verb on the same AuthedClient inherits
// it uniformly.
type capRoundTripper struct {
	base  http.RoundTripper
	limit int64
}

func (c *capRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := c.base.RoundTrip(req)
	if err != nil {
		return resp, err
	}
	if resp.Body != nil && c.limit > 0 {
		// http.MaxBytesReader returns an io.ReadCloser whose Close
		// closes the underlying body, so the existing close
		// discipline on the caller (oapi-codegen's `defer
		// rsp.Body.Close()` inside every `*WithResponse` method)
		// still drains the original body cleanly.
		resp.Body = http.MaxBytesReader(nil, resp.Body, c.limit)
	}
	return resp, nil
}

// cappedHTTPClient returns an http.Client whose Transport caps every
// response body at responseBodyCap. The clone keeps Timeout / Jar /
// CheckRedirect intact and only swaps the Transport for the capped
// wrapper so callers don't mutate http.DefaultClient (which is
// process-global). Passing the returned client to
// `api.AuthedClientOptions.HTTPClient` threads the cap through both
// the bearer-injecting editor and the oauth2 refresh exchange.
func cappedHTTPClient(base *http.Client) *http.Client {
	if base == nil {
		base = http.DefaultClient
	}
	clone := *base
	transport := clone.Transport
	if transport == nil {
		transport = http.DefaultTransport
	}
	clone.Transport = &capRoundTripper{base: transport, limit: responseBodyCap}
	return &clone
}

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL with the 1 MiB response-body cap installed at the
// transport layer, and verifies a non-empty bearer is loaded. The
// caller forwards any returned error to renderRequestError for
// category mapping. Mirrors the helper sibling verb-tree migrations
// (G0.12-T4 #1262, G0.12-T9 #1267) adopted for the same reason.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{
		HTTPClient: cappedHTTPClient(nil),
	})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once, and if the typed response carries a
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors
// the behaviour `api.AuthedClient.GetHealth` implements for the
// /api/v1/health endpoint, generalised so every retrieval verb runs
// the same transparent-retry contract.
//
// statusOf reads the StatusCode off the typed response envelope (the
// generated *Response types expose StatusCode() through their
// embedded *http.Response). A nil response counts as "no retry" —
// the transport already failed and the caller surfaces err directly.
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
// the right output.StructuredError category. Maps the retrieval REST
// surface's pre-response failures: missing bearer, no-refresh-token,
// token-not-found, body-cap / parse failures bubbling out of the
// generated `*WithResponse` parsers, plus the generic transport-down
// case. Non-2xx status codes carried in a typed response envelope
// are classified by renderHTTPStatus instead.
//
// Parse / cap failures route to `output.Unexpected` (exit 4 —
// `unexpected_response`) rather than `output.Unreachable` (exit 3
// — `network_unreachable`). A 1 MiB body cap firing or a JSON
// decode rejecting a malformed payload is a contract / shape
// failure on the server side, not a transport-down failure on the
// operator's side; surfacing it as "unreachable" would send
// operators chasing a network ghost.
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
	// Transport-layer body-cap firing (*http.MaxBytesError out of
	// the capRoundTripper) and JSON shape failures bubbling out of
	// the generated parsers are server-side contract failures, not
	// transport-down failures — surface them as unexpected_response
	// (exit 4) with the backplane URL so the operator sees the
	// origin without chasing a network ghost.
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

// renderHTTPStatus classifies a non-2xx response (or 401 after a
// failed refresh) carried in the typed envelope into the right
// StructuredError category. Acts on the (statusCode, body) pair
// lifted off the generated `*Response.HTTPResponse` + `Body`
// fields. The mapping preserved from the pre-migration shape:
//
//   - 401 → auth_expired (refresh impossible / token rejected).
//   - 403 → insufficient_role with the backend's detail string
//     (usage's `tenant_filter_requires_tenant_admin` case lands
//     here with the "ask tenant_admin for the role grant" hint).
//   - 400 → unexpected with the backend's detail (usage's
//     malformed-since path lands here with the actionable backend
//     hint).
//   - 422 → unexpected wrapping the FastAPI validation envelope.
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
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(fmt.Sprintf(
				"call %s: HTTP 403: %s",
				backplaneURL, decodeDetailString(bodyStr),
			)),
			jsonOut,
		)
	case http.StatusBadRequest:
		// usage's --since=tomorrow path lands here (the backplane
		// emits `{"detail":"invalid since: ..."}`). Surface the
		// detail verbatim so the operator sees the actionable
		// backend hint.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		// 422 from a typo in --surface or the
		// blocker_counts / baseline_overrides shape. The backend
		// emits the FastAPI validation envelope.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", bodyStr)),
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
// body when it's a plain string. Falls back to the trimmed raw body
// when the JSON shape doesn't match (non-JSON body or `detail` is a
// structured value such as the FastAPI validation list).
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
