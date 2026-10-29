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
	"github.com/evoila/meho/cli/internal/version"
)

// CallResult mirrors the backend OperationResult Pydantic model.
// Result and Extras stay json.RawMessage because the backend types
// Result as a oneOf(dict, list) union — pretty-printing the raw bytes
// is the cleanest renderer. Set-shaped responses arrive already
// reduced to a JSONFlux handle envelope + sample by the dispatcher,
// so they render verbatim here with the handle hint intact.
//
// AuditID is the id of the committed audit row, or nil when the
// backplane did not commit one (an old backplane, or a call that
// waits for approval; #3884). The backend sends it for a completed
// call, a failed call and a refused call (#4230, #4801), so the
// human render shows it on every status (#4833).
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
	AuditID    *string         `json:"audit_id"`
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
// distinction. The score fields are only sent to a tenant_admin who
// asks for them (#3668), so they are omitempty here.
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
	FusedScore       float64  `json:"fused_score,omitempty"`
	Bm25Score        *float64 `json:"bm25_score,omitempty"`
	CosineScore      *float64 `json:"cosine_score,omitempty"`
}

// SearchResponse is the JSON envelope returned by
// GET /api/v1/operations/search. Hand-typed for the same reason as
// SearchHit above.
type SearchResponse struct {
	Hits            []SearchHit `json:"hits"`
	QueryDurationMs float64     `json:"query_duration_ms"`
}

// ErrOpError is the sentinel returned when the dispatcher reported a
// structured failure (status == "error", "denied", "rate_limited" or
// "not_found"). main translates a non-nil RunE error into a non-zero
// exit; SilenceErrors=true on
// each command keeps cobra from double-printing the error string.
var ErrOpError = errors.New("operation status not ok")

// StatusAwaitingApproval is the change-class park status the dispatcher
// returns for an approval-gated op that has not yet been approved
// (G11.7-T1 #1401). The backplane has durably written an ApprovalRequest
// row and parked the call; it is a first-class non-error outcome that
// Render surfaces as a parked dispatch and maps to exit 0.
const StatusAwaitingApproval = "awaiting_approval"

// StatusRateLimited is the refusal the dispatcher returns when the caller
// is over its rate or concurrency limit (#3500). The operation did not
// run. The envelope carries extras.retry_after_seconds; the CLI renders
// it like "error" / "denied" and exits 1. It never retries by itself.
const StatusRateLimited = "rate_limited"

// StatusNotFound is the result the dispatcher returns when the connector
// confirmed that the object of the operation does not exist (#3591). The
// operation ran and found nothing. The envelope carries
// extras.resource_ids with the ids that were looked up; the CLI renders
// it like "error", names the ids, and exits 1.
const StatusNotFound = "not_found"

// ParkedHint is the operator-facing one-liner Render prints under the
// status header when a dispatch parks. Exported so the parallel
// operation/call.go render path (a separate package that can't share
// Render) emits the byte-identical hint.
//
// It must NOT tell the operator to re-dispatch: approving a parked
// request EXECUTES the operation — the approve endpoint re-hydrates the
// target and re-dispatches internally with _approved=True (see
// docs/codebase/approvals.md; #1503 / #3548). Re-running the command
// after approval would create a second park / double execution.
const ParkedHint = "parked for human approval — approving it executes the operation; do not re-dispatch"

// ParkedApprovalID returns the approval_request_id the backend stamps on
// a parked (awaiting_approval) envelope's extras blob, or "" when the
// blob is absent, null, or does not carry the field. Best-effort: a
// decode failure yields "" rather than an error, because the id is a
// convenience hint on the human render path, never load-bearing (the
// --json envelope always carries the full extras verbatim).
func ParkedApprovalID(extras json.RawMessage) string {
	if len(extras) == 0 || string(extras) == "null" {
		return ""
	}
	var e struct {
		ApprovalRequestID string `json:"approval_request_id"`
	}
	if err := json.Unmarshal(extras, &e); err != nil {
		return ""
	}
	return e.ApprovalRequestID
}

// WriteParkedHint writes the operator-facing parked guidance every human
// render path shares: the ParkedHint one-liner, then — when the parked
// envelope carried extras.approval_request_id — the approval id plus a
// `meho approvals show <id>` inspect hint so the operator never has to
// hunt the request via `meho approvals list`. Centralised so the
// dispatch.Render path and the parallel operation/call.go path stay
// byte-identical.
func WriteParkedHint(w io.Writer, extras json.RawMessage) {
	fmt.Fprintf(w, "  %s\n", ParkedHint)
	if id := ParkedApprovalID(extras); id != "" {
		fmt.Fprintf(w, "  approval id: %s — inspect with: meho approvals show %s\n", id, id)
	}
}

// UnknownStatusError is the error for an OperationResult.status this
// CLI does not know. The usual cause is a backplane that is newer than
// the CLI and has added a status (a CLI from before the approval flow
// read "awaiting_approval" as invalid, although the operation had
// parked fine). So the message names the version gap as the likely
// cause and warns that the operation may have run or be waiting for
// approval, instead of calling the response invalid. The exit code
// stays unexpected_response (4): the CLI cannot say whether it worked.
func UnknownStatusError(status string) *output.StructuredError {
	return output.Unexpected(fmt.Sprintf(
		"backplane returned OperationResult.status %q, which this CLI (%s) does not know. "+
			"The backplane is probably newer than this CLI. "+
			"This is not necessarily a failure: the operation may have run or may be waiting for approval, so check before you retry. "+
			"Run `meho status` to compare versions, then upgrade the CLI.",
		status, version.Get().Version))
}

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
	PostPreviewApiV1OperationsPreviewPostWithResponse(
		ctx context.Context,
		params *api.PostPreviewApiV1OperationsPreviewPostParams,
		body api.PostPreviewApiV1OperationsPreviewPostJSONRequestBody,
		reqEditors ...api.RequestEditorFn,
	) (*api.PostPreviewApiV1OperationsPreviewPostResponse, error)
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
	return c.CallWithTarget(ctx, backplaneURL, opID, slugTarget(targetSlug), params)
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
	body, err := c.callBody(opID, target, params)
	if err != nil {
		return nil, err
	}
	return c.postCall(ctx, backplaneURL, body)
}

// CallWithPreviewHash is Call for an op on the destructive risk level. It
// also sends previewHash: the hash a prior Preview of the same op, target
// and params returned. The backplane refuses a destructive op without a
// matching hash, so a verb runs Preview first and passes its hash here.
func (c Connector) CallWithPreviewHash(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
	previewHash string,
) (*CallResult, error) {
	body, err := c.callBody(opID, slugTarget(targetSlug), params)
	if err != nil {
		return nil, err
	}
	if previewHash != "" {
		h := previewHash
		body.PreviewHash = &h
	}
	return c.postCall(ctx, backplaneURL, body)
}

// slugTarget returns the dict-shape target {"name": slug}, or nil for an
// empty slug (the target field is then sent as JSON null).
func slugTarget(targetSlug string) map[string]any {
	if targetSlug == "" {
		return nil
	}
	return map[string]any{"name": targetSlug}
}

// callBody builds the OperationCall body shared by every Call variant.
func (c Connector) callBody(
	opID string,
	target map[string]any,
	params map[string]any,
) (api.CallOperationBody, error) {
	body := api.CallOperationBody{
		ConnectorId: c.ID,
		OpId:        opID,
	}
	if target != nil {
		var t api.CallOperationBody_Target
		if err := t.FromCallOperationBodyTarget1(target); err != nil {
			return body, fmt.Errorf("encode target: %w", err)
		}
		body.Target = &t
	}
	if params != nil {
		p := params
		body.Params = &p
	}
	return body, nil
}

// PreviewResult is the part of the POST /api/v1/operations/preview
// envelope a vendor verb reads: the status, the preview_hash a destructive
// call must send, and the error + extras when no preview was made. Raw
// keeps the whole envelope for --json output. Hand-typed for the same
// reason as CallResult (the route returns `dict[str, Any]`).
type PreviewResult struct {
	Status      string          `json:"status"`
	OpID        string          `json:"op_id"`
	PreviewHash string          `json:"preview_hash"`
	Error       *string         `json:"error"`
	Extras      json.RawMessage `json:"extras,omitempty"`
	Raw         json.RawMessage `json:"-"`
}

// Preview POSTs the same op, target and params a Call would send to the
// preview route. Nothing is sent to the vendor and nothing waits for
// approval; the result carries the preview_hash that CallWithPreviewHash
// needs. Uses the same dict-shape target and 401-refresh dance as Call.
func (c Connector) Preview(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
) (*PreviewResult, error) {
	client, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := api.PreviewOperationBody{
		ConnectorId: c.ID,
		OpId:        opID,
	}
	if target := slugTarget(targetSlug); target != nil {
		var t api.PreviewOperationBody_Target
		if err := t.FromPreviewOperationBodyTarget1(target); err != nil {
			return nil, fmt.Errorf("encode target: %w", err)
		}
		body.Target = &t
	}
	if params != nil {
		p := params
		body.Params = &p
	}
	apiParams := &api.PostPreviewApiV1OperationsPreviewPostParams{}
	resp, err := client.PostPreviewApiV1OperationsPreviewPostWithResponse(ctx, apiParams, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.PostPreviewApiV1OperationsPreviewPostWithResponse(ctx, apiParams, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out PreviewResult
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, fmt.Errorf("decode preview response: %w", err)
	}
	out.Raw = append(json.RawMessage(nil), resp.Body...)
	return &out, nil
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
		// `q` is the canonical free-text param (#1854); the legacy
		// `query` alias is deprecated. `q` is a pointer (optional on
		// the wire) but Search's contract requires a non-empty query,
		// so the value is always present here.
		Q: &query,
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
// "error" / "denied" / "rate_limited" / "not_found" to ErrOpError. When
// prettyPrinter is nil the generic envelope is used.
//
// awaiting_approval (parked) is intercepted here — ahead of the
// per-verb prettyPrinter — so the park is a first-class non-error
// outcome on every dispatch render path (the generic verb, every
// argocd/keycloak approval-gated write verb, secret move, and any
// future requires_approval verb) without each verb re-implementing
// it. A parked dispatch renders the status header plus the ParkedHint
// (or the full envelope under --json, incl. extras.approval_request_id)
// and returns nil → exit 0.
func (c Connector) Render(
	cmd *cobra.Command,
	opID string,
	r *CallResult,
	jsonOut bool,
	prettyPrinter func(w io.Writer, r *CallResult),
) error {
	if r.Status == StatusAwaitingApproval {
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), r)
		}
		w := cmd.OutOrStdout()
		fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", c.ID, opID, r.Status, r.DurationMs)
		WriteParkedHint(w, r.Extras)
		// A multi-step operation that stops for approval already has
		// its own audit row; show its id like every other status (#4833).
		WriteAuditReceipt(w, r.AuditID)
		return nil // parked, not failed — exit 0.
	}
	switch r.Status {
	case "ok", "error", "denied", StatusRateLimited, StatusNotFound:
		// fall through.
	default:
		return output.RenderError(cmd.ErrOrStderr(), UnknownStatusError(r.Status), jsonOut)
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
// connector error + extras + audit receipt on failure (see
// WriteErrorTrailer). Used as the default pretty-printer when a verb
// passes none.
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
	WriteErrorTrailer(w, r)
}

// WriteErrorTrailer writes the human render of a non-ok envelope
// (status error, denied, rate_limited or not_found): the connector
// error, the ids a not_found envelope names, the extras blob, then the
// audit receipt when the backplane committed one. Every vendor
// package's printErrorTrailer and the operation/call.go render path
// delegate here, so a failed call shows its audit id on every render
// path (#4833). The receipt is the row that says why the call failed,
// so it matters most on this branch.
func WriteErrorTrailer(w io.Writer, r *CallResult) {
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if r.Status == StatusNotFound {
		WriteNotFoundIDs(w, r.Extras)
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
	WriteAuditReceipt(w, r.AuditID)
}

// NotFoundResourceIDs returns the ids a not_found envelope names in
// extras.resource_ids, or nil when the blob is absent, null, or does
// not carry the field. Best-effort like ParkedApprovalID: the ids are a
// convenience on the human render path, and --json carries the full
// extras verbatim.
func NotFoundResourceIDs(extras json.RawMessage) []string {
	if len(extras) == 0 || string(extras) == "null" {
		return nil
	}
	var e struct {
		ResourceIDs []string `json:"resource_ids"`
	}
	if err := json.Unmarshal(extras, &e); err != nil {
		return nil
	}
	return e.ResourceIDs
}

// WriteNotFoundIDs writes the `not found: <id>[, <id>...]` line that
// names the missing object in plain words, or nothing when the extras
// blob carries no resource_ids.
func WriteNotFoundIDs(w io.Writer, extras json.RawMessage) {
	ids := NotFoundResourceIDs(extras)
	if len(ids) == 0 {
		return
	}
	line := "not found: " + ids[0]
	for _, id := range ids[1:] {
		line += ", " + id
	}
	fmt.Fprintln(w, line)
}

// WriteAuditReceipt writes the `audit receipt: <id>` line, or nothing
// when auditID is nil. Shared by WriteErrorTrailer and the parallel
// operation/call.go render path so the line stays byte-identical
// everywhere.
func WriteAuditReceipt(w io.Writer, auditID *string) {
	if auditID == nil || *auditID == "" {
		return
	}
	fmt.Fprintf(w, "audit receipt: %s\n", *auditID)
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
