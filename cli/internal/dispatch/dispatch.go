// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package dispatch is the shared operation-call core for the per-vendor
// CLI command packages (cmd/vault, cmd/harbor, cmd/nsx, ...). Before
// it, every vendor package carried a byte-identical dispatch.go —
// CallResult, dispatchOp, renderCallResult, printGenericResult,
// prettyJSON — duplicated only because sibling cmd/* packages can't
// import one another without an import cycle (cmd/root.go grafts
// each onto the tree). This leaf package breaks that cycle: each
// vendor package binds one Connector and calls its methods.
//
// G0.12-T16 (#1274) promoted Connector to own the authed transport
// — it now lazily builds an *api.AuthedClient per call and issues
// every request through the generated typed surface (oapi-codegen
// *WithResponse helpers). The per-vendor doAuthedRequest / sendRequest /
// httpError trio is gone; per-vendor renderRequestError now matches
// *APIResponseError from this package instead of a local sentinel.
// The one-shot 401-refresh dance mirrors api.AuthedClient.GetHealth.
package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// CallResult mirrors the backend OperationResult Pydantic model.
// Result and Extras stay json.RawMessage because the backend types
// Result as a oneOf(dict, list) union — pretty-printing the raw bytes
// is the cleanest renderer. Set-shaped responses arrive already
// reduced to a JSONFlux handle envelope + sample by the dispatcher,
// so they render verbatim here with the handle hint intact.
//
// Kept hand-typed rather than generator-backed because the FastAPI
// surface types `/api/v1/operations/call`'s response as
// `dict[str, Any]`, so the oapi-codegen generator emits the response
// as `*map[string]interface{}` (see
// PostCallApiV1OperationsCallPostResponse.JSON200 in client.gen.go) —
// no typed model worth using. Promoting the FastAPI response to a
// typed model so the generator picks it up is a separate backend
// Task explicitly out of scope for G0.12-T16 #1274 (the CLI hygiene
// Initiative #1118 is consumer-side only).
type CallResult struct {
	Status     string          `json:"status"`
	OpID       string          `json:"op_id"`
	Result     json.RawMessage `json:"result"`
	Error      *string         `json:"error"`
	Extras     json.RawMessage `json:"extras,omitempty"`
	DurationMs float64         `json:"duration_ms"`
}

// CallRequestBody mirrors the backend CallOperationBody Pydantic model.
// Target is a map so the empty case serialises as null rather than an
// empty struct; the route layer's resolver short-circuits the missing-
// name case for ops that need a target.
//
// Retained even though Connector.Call no longer marshals this struct
// directly (it builds api.CallOperationBody from the generated client
// instead) — per-vendor tests assert on-the-wire shape by decoding
// the request body into this struct via httptest, and the type
// continues to mirror the wire format exactly.
type CallRequestBody struct {
	ConnectorID string         `json:"connector_id"`
	OpID        string         `json:"op_id"`
	Target      map[string]any `json:"target"`
	Params      map[string]any `json:"params,omitempty"`
}

// SearchHit mirrors the backend OperationSearchHit Pydantic model.
// Pointer fields are nullable per the backend's frozen pydantic
// model — preserved so JSON unmarshal keeps the null vs zero-value
// distinction.
//
// Promoted from the 9 per-vendor copies (harbor / hetzner-robot /
// nsx / sddc-manager / vcf-automation / vcf-fleet / vcf-logs /
// vcf-operations / vmware) in G0.12-T16 #1274. Hand-typed for the
// same reason as CallResult: the FastAPI surface types
// `/api/v1/operations/search`'s response as `dict[str, Any]`.
type SearchHit struct {
	OpID             string   `json:"op_id"`
	Summary          *string  `json:"summary"`
	Description      *string  `json:"description"`
	GroupKey         *string  `json:"group_key"`
	SafetyLevel      string   `json:"safety_level"`
	RequiresApproval bool     `json:"requires_approval"`
	FusedScore       float64  `json:"fused_score"`
	Bm25Score        *float64 `json:"bm25_score"`
	CosineScore      *float64 `json:"cosine_score"`
}

// SearchResponse is the JSON envelope returned by
// GET /api/v1/operations/search. Hand-typed for the same reason as
// SearchHit above.
type SearchResponse struct {
	Hits            []SearchHit `json:"hits"`
	QueryDurationMs float64     `json:"query_duration_ms"`
}

// ErrOpError is the sentinel returned when the dispatcher reported a
// structured failure (status == "error" or "denied"). main translates
// a non-nil RunE error into a non-zero exit; SilenceErrors=true on
// each command keeps cobra from double-printing the error string.
var ErrOpError = errors.New("operation status not ok")

// APIResponseError wraps a non-2xx response from the backplane so
// per-vendor renderRequestError can pick the right output category
// (401 → AuthExpired, 403 → InsufficientRole, other non-2xx →
// Unexpected). Constructed by Connector.Call / CallWithTarget /
// Search after the one-shot 401-refresh path has been exhausted.
//
// Replaces the per-vendor *httpError sentinel that the G0.12-T16
// #1274 consolidation deleted. Each vendor package's
// renderRequestError matches via errors.As against this type.
type APIResponseError struct {
	StatusCode int
	Body       string
}

func (e *APIResponseError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// operationsAPI is the minimal slice of api.ClientWithResponsesInterface
// the dispatch package consumes, plus the Refresh hook the one-shot
// 401-retry path invokes. Defined as a package-private interface so
// the dispatch test suite can substitute a tiny fake without reaching
// for the full ~140-method generated surface. *api.AuthedClient
// satisfies this directly: it embeds *ClientWithResponses (which
// provides the two *WithResponse calls) and defines Refresh of its
// own.
type operationsAPI interface {
	PostCallApiV1OperationsCallPostWithResponse(
		ctx context.Context,
		params *api.PostCallApiV1OperationsCallPostParams,
		body api.PostCallApiV1OperationsCallPostJSONRequestBody,
		reqEditors ...api.RequestEditorFn,
	) (*api.PostCallApiV1OperationsCallPostResponse, error)
	GetSearchApiV1OperationsSearchGetWithResponse(
		ctx context.Context,
		params *api.GetSearchApiV1OperationsSearchGetParams,
		reqEditors ...api.RequestEditorFn,
	) (*api.GetSearchApiV1OperationsSearchGetResponse, error)
	Refresh(ctx context.Context) error
}

// newAuthedClient is the production-mode factory Connector defaults to
// for building the authed transport per call. Returning the per-package
// operationsAPI interface (not *api.AuthedClient) keeps the call sites
// typed against the dispatch seam, not the generated client; tests
// override this var with a fake. Variable rather than method so the
// override stays scoped to the test file.
var newAuthedClient = func(ctx context.Context, backplaneURL string) (operationsAPI, error) {
	return api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
}

// Connector binds a pre-baked connector_id and owns the authed
// transport so every alias verb in a vendor package shares one
// dispatch + render implementation. The transport is built lazily
// per Call/Search invocation (mirroring the pre-#1274 doAuthedRequest
// lifecycle) so refreshed tokens land back in the store between
// invocations without per-Connector caching.
type Connector struct {
	ID string
}

// New constructs a Connector with the supplied pre-baked connector_id.
// Vendor packages bind one package-level `var conn = dispatch.New(...)`
// per dir.
func New(connectorID string) Connector {
	return Connector{ID: connectorID}
}

// Call POSTs an OperationCall to the backplane and returns the decoded
// CallResult. An empty targetSlug omits the target field on the wire
// (JSON null) for ops that don't act on a target; nil params omits
// params.
//
// targetSlug serializes as the dict shape `{"name": "<slug>"}` via
// FromCallOperationBodyTarget1 — the wire shape every pre-#1274
// per-vendor verb has been sending. The bare-string target shape
// (FromCallOperationBodyTarget0) is the forward-preferred form per
// G0.13-T2 #1132 but operationally identical (the backend normalises
// both to the same dict before dispatch); keeping the dict shape
// preserves byte-identical wire output for the existing per-vendor
// test suites that assert on body.Target["name"]. CallWithTarget
// below is the explicit entry point for callers (vcf-automation)
// that thread additional dict keys.
func (c Connector) Call(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
) (*CallResult, error) {
	var target map[string]any
	if targetSlug != "" {
		target = map[string]any{"name": targetSlug}
	}
	return c.CallWithTarget(ctx, backplaneURL, opID, target, params)
}

// CallWithTarget is Call for connectors that need extra target keys
// beyond "name" (e.g. cmd/vcf-automation threads a per-call "fqdn"
// vhost override). target is encoded verbatim — pass nil to omit
// the target field (JSON null) for ops that don't act on a target.
//
// Uses the dict-shape target form (FromCallOperationBodyTarget1)
// because the bare-string form can't carry extra keys.
func (c Connector) CallWithTarget(
	ctx context.Context,
	backplaneURL, opID string,
	target map[string]any,
	params map[string]any,
) (*CallResult, error) {
	body := api.CallOperationBody{
		ConnectorId: c.ID,
		OpId:        opID,
	}
	if target != nil {
		var t api.CallOperationBody_Target
		if err := t.FromCallOperationBodyTarget1(target); err != nil {
			return nil, fmt.Errorf("encode target: %w", err)
		}
		body.Target = &t
	}
	if params != nil {
		p := params
		body.Params = &p
	}
	return c.postCall(ctx, backplaneURL, body)
}

// postCall issues the typed POST through the generated *WithResponse
// helper and runs the one-shot 401-refresh dance, mirroring
// api.AuthedClient.GetHealth. Non-2xx outcomes wrap as
// *APIResponseError for renderRequestError to classify.
func (c Connector) postCall(
	ctx context.Context,
	backplaneURL string,
	body api.CallOperationBody,
) (*CallResult, error) {
	client, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	apiParams := &api.PostCallApiV1OperationsCallPostParams{}
	resp, err := client.PostCallApiV1OperationsCallPostWithResponse(ctx, apiParams, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.PostCallApiV1OperationsCallPostWithResponse(ctx, apiParams, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out CallResult
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, fmt.Errorf("decode call response: %w", err)
	}
	return &out, nil
}

// Search GETs the operations-search route and returns the decoded
// envelope. groupKey == "" omits the optional `group` filter; limit
// <= 0 omits the optional `limit` cap (the backend applies its own
// default).
//
// The typed GetSearchApiV1OperationsSearchGetParams carries pointer
// fields for the optional query params — nil leaves them off the
// URL via the generator's omitempty form.
func (c Connector) Search(
	ctx context.Context,
	backplaneURL, query, groupKey string,
	limit int,
) (*SearchResponse, error) {
	client, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.GetSearchApiV1OperationsSearchGetParams{
		ConnectorId: c.ID,
		Query:       query,
	}
	if groupKey != "" {
		g := groupKey
		params.Group = &g
	}
	if limit > 0 {
		l := limit
		params.Limit = &l
	}
	resp, err := client.GetSearchApiV1OperationsSearchGetWithResponse(ctx, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.GetSearchApiV1OperationsSearchGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out SearchResponse
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, fmt.Errorf("decode search response: %w", err)
	}
	return &out, nil
}

// classifyNon2xx wraps the generated *Response fields into the local
// APIResponseError sentinel renderRequestError extracts via
// errors.As. Truncates the body at 1 MiB to match the pre-#1274
// transport's response read cap.
func classifyNon2xx(resp *http.Response, body []byte) *APIResponseError {
	const maxBodyBytes = 1 << 20 // 1 MiB
	b := body
	if len(b) > maxBodyBytes {
		b = b[:maxBodyBytes]
	}
	return &APIResponseError{
		StatusCode: resp.StatusCode,
		Body:       trimSpace(string(b)),
	}
}

// trimSpace mirrors strings.TrimSpace but as a local helper so the
// dispatch package keeps its import surface compact (strings would
// otherwise enter just for one call).
func trimSpace(s string) string {
	start := 0
	end := len(s)
	for start < end && isSpace(s[start]) {
		start++
	}
	for end > start && isSpace(s[end-1]) {
		end--
	}
	return s[start:end]
}

func isSpace(b byte) bool {
	switch b {
	case ' ', '\t', '\n', '\r', '\v', '\f':
		return true
	}
	return false
}

// Render runs the unified post-dispatch path every verb uses: validate
// the status enum, render the envelope (JSON or human), then map
// "error" / "denied" to ErrOpError. When prettyPrinter is nil the
// generic envelope is used.
func (c Connector) Render(
	cmd *cobra.Command,
	opID string,
	r *CallResult,
	jsonOut bool,
	prettyPrinter func(w io.Writer, r *CallResult),
) error {
	switch r.Status {
	case "ok", "error", "denied":
		// fall through.
	default:
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"backplane returned invalid OperationResult.status %q (expected one of: ok / error / denied)",
				r.Status,
			)),
			jsonOut,
		)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), r); err != nil {
			return err
		}
	} else if prettyPrinter != nil {
		prettyPrinter(cmd.OutOrStdout(), r)
	} else {
		c.PrintGeneric(cmd.OutOrStdout(), opID, r)
	}
	if r.Status == "ok" {
		return nil
	}
	return ErrOpError
}

// PrintGeneric renders a CallResult in the generic envelope shape: a
// status line, then the pretty-printed Result on success or the
// connector error + extras on failure. Used as the default
// pretty-printer when a verb passes none.
func (c Connector) PrintGeneric(w io.Writer, opID string, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", c.ID, opID, r.Status, r.DurationMs)
	if r.Status == "ok" {
		if len(r.Result) > 0 && string(r.Result) != "null" {
			pretty, err := PrettyJSON(r.Result)
			if err == nil {
				fmt.Fprintln(w, pretty)
				return
			}
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		pretty, err := PrettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}

// PrettyJSON pretty-prints a json.RawMessage with 2-space indent.
func PrettyJSON(raw json.RawMessage) (string, error) {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return "", err
	}
	out, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return "", err
	}
	return string(out), nil
}
