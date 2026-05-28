// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package topology hosts the cobra commands under `meho topology ...`
// for G9.1-T6 (#454) of Initiative #363 (read/traversal verbs) and
// G9.2-T6 (#599) of Initiative #364 (curated-edge write + listing
// verbs). v0.2 ships seven operator-facing verbs over the T5 REST
// surface (#453, #597) shipped by
// `backend/src/meho_backplane/api/v1/topology.py`:
//
//   - `meho topology refresh <target> [--json]` —
//     POST /api/v1/topology/refresh/{target}. Rediscovers one
//     target's topology and reconciles it into the graph; renders
//     the per-target added/removed/updated node + edge counts. Role:
//     operator.
//   - `meho topology dependents <name|alias> [--depth N]
//     [--kind <edge_kind>] [--json]` — GET
//     /api/v1/topology/dependents/{name}. Reverse closure ("what
//     depends on me"). Role: operator.
//   - `meho topology dependencies <name|alias> [--depth N]
//     [--kind <edge_kind>] [--json]` — GET
//     /api/v1/topology/dependencies/{name}. Forward closure ("what I
//     depend on"). Role: operator.
//   - `meho topology path <from> <to> [--max-hops N] [--json]` —
//     GET /api/v1/topology/path?from=A&to=B. Shortest unweighted
//     path between two named nodes, or the no-path line when
//     unreachable. Role: operator.
//   - `meho topology annotate <from> <kind> <to> [--note "..."]
//     [--evidence-url URL] [--json]` — POST /api/v1/topology/edges.
//     Operator-curated cross-system edge assertion. Idempotent.
//     Role: tenant_admin.
//   - `meho topology unannotate <edge-id> | <from> <kind> <to>` —
//     DELETE /api/v1/topology/edges/{edge_id}. The tuple form is
//     client-side: a GET resolves the unique curated edge id, then
//     DELETE removes it. Auto rows refuse with 409 + remediation
//     hint. Role: tenant_admin.
//   - `meho topology list-edges [--kind K] [--source curated|auto]
//     [--from N] [--to N] [--conflicts] [--json]` — GET
//     /api/v1/topology/edges. Flat filterable listing of the
//     tenant's edges. Role: operator.
//   - `meho topology bulk-import <file> [--dry-run] [--json]` —
//     POST /api/v1/topology/edges/bulk. Reads a YAML/JSON file
//     shaped as `{edges: [...]}` and posts the whole batch in one
//     transaction. The server runs validation first; a single bad
//     row aborts the entire batch (no partial apply). Re-running
//     the same file is a per-row no-op. Role: tenant_admin.
//
// G9.3 timeline / diff / history verbs round out the topology surface.
//
// The fifth G9.1-T6 verb, `meho targets discover <product>`, lives
// under `cli/internal/cmd/targets/discover.go` because it sits under
// the canonical `/api/v1/targets` prefix next to the other target-
// scoped verbs (mirroring where the backend registers
// GET /api/v1/targets/discover on the targets router).
//
// Each verb wraps one backplane route and renders the response in
// either a human-readable form (count summary / node table / path
// chain) or `--json` mode. Authentication piggybacks on the token
// `meho login` wrote — same pattern as `meho audit`, `meho targets`,
// and `meho kb`.
//
// G0.12-T15 #1273 migrated this package off the sibling-verb pattern
// of hand-rolled HTTP + hand-typed copies of the backend pydantic
// models. Every verb here drives the generated
// `api.ClientWithResponses` surface directly: `api.NewAuthedClient`
// wires the bearer + lazy 401-refresh editor onto the embedded
// `ClientWithResponses`, and the verbs call the typed `*WithResponse`
// methods (`PathApiV1TopologyPathGetWithResponse` etc.). Consumer-side
// struct drift — the #1069 root cause Initiative #1118 targets —
// can't recur because we now consume `api.TopologyNode`,
// `api.TopologyEdge`, `api.TopologyPath`, `api.TopologyHistoryResult`,
// `api.TopologyTimelineResult`, `api.TopologyDiffResult`, and
// `api.RefreshResult` directly. Opts into the T10 #1268
// transport-layer response-body cap (1 MiB) so the generated parsers
// can't be pinned by an unbounded backplane response.
//
// Per [CLAUDE.md](../../CLAUDE.md) postulate 5 these verbs are
// operator-facing ergonomics; the agent surface for the same data is
// the single `query_topology` MCP meta-tool (T7), not per-verb tools.
package topology

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

// NewRootCmd returns the `meho topology` parent command. The command
// is grafted onto the top-level meho command tree by cmd/root.go
// alongside `meho audit`, `meho targets`, `meho kb`, and the rest.
// The parent itself takes no args and prints its own help; every
// piece of behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "topology",
		Short: "Query and refresh the MEHO topology graph (refresh / dependents / dependencies / path / timeline / diff / history / annotate / unannotate / list-edges)",
		Long: "Operate the tenant-scoped topology graph wired by G9.1 + " +
			"G9.2 + G9.3. Refresh a target's discovered topology, walk " +
			"what depends on a node (dependents), walk what a node " +
			"depends on (dependencies), find the shortest path between " +
			"two nodes, walk the tenant's chronological change feed " +
			"from the diff-on-write history substrate (timeline), " +
			"compute the net per-resource delta between two timestamps " +
			"(diff), walk " +
			"the per-resource history of one node with optional incident " +
			"edges (history), assert a curated cross-system edge " +
			"(annotate), delete a curated edge (unannotate), or list " +
			"the tenant's edges (list-edges). Read verbs are operator-" +
			"level; annotate / unannotate require tenant_admin. Tenant " +
			"scoping is enforced server-side via the JWT — no surface " +
			"accepts a tenant id, and cross-tenant queries return the " +
			"same empty/404 shape as a node that does not exist.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRefreshCmd())
	cmd.AddCommand(newDependentsCmd())
	cmd.AddCommand(newDependenciesCmd())
	cmd.AddCommand(newPathCmd())
	cmd.AddCommand(newAnnotateCmd())
	cmd.AddCommand(newUnannotateCmd())
	cmd.AddCommand(newListEdgesCmd())
	cmd.AddCommand(newBulkImportCmd())
	cmd.AddCommand(newTimelineCmd())
	cmd.AddCommand(newDiffCmd())
	cmd.AddCommand(newHistoryCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns
// when the stored token row exists but its access_token field is
// empty. Routed to auth_expired (exit 2) with a `meho login` hint
// rather than the generic transport-error path. Mirrors the kb /
// memory siblings.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the topology verb tree's transport
// will read off any backplane response body. 1 MiB matches the
// per-call ceiling the substrate enforces server-side on the listing /
// diff / history routes (timeline cursor pages, history rows capped at
// 5000, diff entries at 1000) — generous headroom for a JSON payload
// whose contract caps the row count well below the byte cap. Without
// the cap, an adversarial or runaway backplane response could OOM the
// CLI because the generated `Parse*Response` helpers call
// `io.ReadAll(rsp.Body)` on an unbounded body before constructing the
// typed envelope. The cap is installed at the transport layer via
// `capRoundTripper` below so it applies uniformly to every typed verb
// on the same `AuthedClient`. The sibling G0.12-T9 #1267 / G0.12-T10
// #1268 / G0.12-T12 #1270 verb trees all install the same wrapper
// locally rather than reach into `cli/internal/api/client.go` — the
// shared `ResponseBodyLimit` option in that file is destined to
// supersede every per-tree copy in a follow-on refactor; until then,
// the inline wrapper keeps this migration's blast radius inside the
// topology verb tree.
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
// transport layer, and verifies a non-empty bearer is loaded.
// Centralised so every verb's typed-call path goes through the same
// "stored-token-loaded + non-empty bearer" gate; the caller forwards
// any returned error to renderRequestError for category mapping.
// Mirrors the sibling verb-tree migrations (G0.12-T9 #1267 kb,
// G0.12-T10 #1268 memory, G0.12-T12 #1270 retrieval).
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
// 401, runs a one-shot bearer refresh and re-issues call. Same
// shape `kb` adopted in G0.12-T9 #1267 and `memory` re-used in
// G0.12-T10 #1268 so every topology verb runs the same
// transparent-retry contract.
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

// renderRequestError translates a transport-layer request error
// into the right output.StructuredError category. Maps the topology
// REST surface's pre-response failures: missing bearer, no-refresh-
// token, token-not-found, body-cap / parse failures bubbling out of
// the generated `*WithResponse` parsers, plus the generic transport-
// down case. Non-2xx status codes carried in a typed response
// envelope are classified by renderHTTPStatus instead.
//
// Parse / cap failures route to `output.Unexpected` (exit 4 —
// `unexpected_response`) rather than `output.Unreachable` (exit 3 —
// `network_unreachable`). A 1 MiB body cap firing or a JSON decode
// rejecting a malformed payload is a contract / shape failure on
// the server side, not a transport-down failure on the operator's
// side; surfacing it as "unreachable" would send operators chasing
// a network ghost. The cap is installed by `newAuthedClient` via
// `api.AuthedClientOptions.ResponseBodyLimit` (responseBodyCap).
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
	// Transport-layer body-cap firing (*http.MaxBytesReader returned
	// from capRoundTripper) and JSON shape failures bubbling out of
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

// renderHTTPStatus classifies a non-2xx response carried in the
// typed envelope into the right StructuredError category. Same
// classification ladder as the pre-migration `renderHTTPError` switch
// but acts on the (statusCode, body) pair lifted off the generated
// `*Response.HTTPResponse` + `Body` fields rather than a sentinel
// `httpError` value:
//
//   - 401 → auth_expired with a `meho login <url>` hint.
//   - 403 (RBAC denial) → insufficient_role; the backend's 403 detail
//     string names the required role (operator).
//   - 404 → unexpected_response. `refresh` resolves the target via
//     resolve_target, so its 404 carries the structured
//     `{"error":"no_target","query":...,"matches":[...]}` envelope
//     (near-misses surfaced); the read verbs never 404 (a missing
//     node is an empty list / null path, 200), so a 404 there is a
//     routing surprise surfaced verbatim.
//   - 409 with detail.error == "ambiguous_node" → unexpected, listing
//     the colliding kinds so the operator can re-issue with
//     --node-kind (the anchor `kind` pin; --kind is the edge filter
//     and does not clear this 409).
//   - Any other 4xx/5xx → unexpected with the raw body.
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
		// 403 detail is FastAPI/Starlette's HTTPException shape
		// ({"detail": "<string>"}). require_role writes the required
		// role into the detail string; pass it through verbatim.
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		// Only `refresh` can 404 — resolve_target's structured
		// `{"error":"no_target","query":...,"matches":[...]}`
		// envelope. Render near-miss suggestions when present so the
		// operator sees "did you mean rdc-vcenter?". This is also the
		// cross-tenant boundary surface for refresh: a target in
		// another tenant resolves to no_target, identical to a typo.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatNotFound(bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		// 409 from the query layer's AmbiguousNodeError: a bare name
		// resolved to more than one graph_node.kind. Surface the
		// colliding kinds so the operator re-issues with --node-kind
		// (the anchor `kind` pin — not --kind, which is the edge
		// filter `kind_filter` and cannot clear the ambiguity).
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(formatAmbiguousNode(bodyStr)),
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
// detail field accepting either a string (FastAPI's default) or a
// nested object (resolver's no_target / query's ambiguous_node).
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// resolverDetail mirrors the structured detail TargetNotFoundError
// raises (resolver.py): {"error","query","matches":[...]}.
type resolverDetail struct {
	Error   string                   `json:"error"`
	Query   string                   `json:"query"`
	Matches []resolverMatchOnTheWire `json:"matches"`
}

// resolverMatchOnTheWire mirrors the human-readable fields of the
// resolver's near-miss matches (TargetSummary.model_dump).
type resolverMatchOnTheWire struct {
	Name    string   `json:"name"`
	Aliases []string `json:"aliases"`
}

// ambiguousNodeDetail mirrors the 409 body topology.py's
// _ambiguous_node_http emits: {"error":"ambiguous_node",
// "name":"<n>","kinds":["host","vm"]}.
type ambiguousNodeDetail struct {
	Error string   `json:"error"`
	Name  string   `json:"name"`
	Kinds []string `json:"kinds"`
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

// formatNotFound renders a refresh 404 envelope into one operator-
// readable line. Structured form (resolver) → "Target not found: <q>;
// did you mean: <a, b, c>"; plain string fallback → raw detail.
func formatNotFound(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var detail resolverDetail
		if jerr := json.Unmarshal(env.Detail, &detail); jerr == nil && detail.Error != "" {
			if len(detail.Matches) == 0 {
				return fmt.Sprintf("Target not found: %q (no near-misses)", detail.Query)
			}
			names := make([]string, 0, len(detail.Matches))
			for _, m := range detail.Matches {
				names = append(names, m.Name)
			}
			return fmt.Sprintf("Target not found: %q; did you mean: %s",
				detail.Query, strings.Join(names, ", "))
		}
	}
	return "not found: " + decodeDetailString(body)
}

// formatAmbiguousNode renders a 409 ambiguous_node envelope into one
// line that names the colliding kinds and the --node-kind remedy.
// The anchor pin is --node-kind (the route's `kind` param); --kind is
// the edge filter (`kind_filter`) and does not resolve this 409.
func formatAmbiguousNode(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var detail ambiguousNodeDetail
		if jerr := json.Unmarshal(env.Detail, &detail); jerr == nil && detail.Error == "ambiguous_node" {
			return fmt.Sprintf(
				"node %q is ambiguous across kinds: %s — re-run with --node-kind <one of them>",
				detail.Name, strings.Join(detail.Kinds, ", "))
		}
	}
	return "ambiguous node: " + decodeDetailString(body)
}

// pathEscape escapes a single path segment for use inside a backend
// URL. url.PathEscape escapes path-segment-unsafe characters without
// touching the unreserved set.
//
// Still useful for the small handful of CLI-side paths that the
// G0.12-T15 migration kept hand-rolled (refresh's path-shape test
// fixture, plus the test plumbing that pre-dates the typed-client
// transport).
func pathEscape(segment string) string {
	return url.PathEscape(segment)
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Rune-aware so multi-byte UTF-8 survives the
// cut. Same shape as the targets-package helper; kept here to avoid
// the import-cycle the cmd → cmd/topology → cmd path would create.
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
