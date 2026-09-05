// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/evoila/meho/cli/internal/api"
)

// fakeOperationsClient is a per-package test double satisfying the
// operationsAPI interface. Each verb's WithResponse method records
// the typed params struct it was called with and returns a canned
// *Response; the per-test setup wires the canned response (status,
// body, refresh-counter, error) for the scenario under exercise.
//
// Compared to mocking the full generated ClientWithResponsesInterface
// (~140 methods), the per-package interface keeps this fake tiny:
// three call recorders + one refresh counter + per-call canned
// responses. New G0.12 hygiene Tasks (#1261, #1262, …) get their
// own per-package interface + fake the same shape.
type fakeOperationsClient struct {
	// Recorded params from the most recent call to each verb. Tests
	// inspect these to verify that typed params are passed (not
	// raw-string URL concatenation).
	lastCallParams        *api.PostCallApiV1OperationsCallPostParams
	lastCallBody          *api.CallOperationBody
	lastPreviewParams     *api.PostPreviewApiV1OperationsPreviewPostParams
	lastPreviewBody       *api.PreviewOperationBody
	lastGroupsParams      *api.GetGroupsApiV1OperationsGroupsGetParams
	lastSearchParams      *api.GetSearchApiV1OperationsSearchGetParams
	lastResultQueryParams *api.PostResultQueryApiV1OperationsResultQueryPostParams
	lastResultQueryBody   *api.ResultQueryBody

	// Sequenced canned responses — pop one per call (per verb). Tests
	// register two responses on the auth-refresh scenarios (first a
	// 401, then the post-refresh outcome) and one on every other
	// path.
	callResponses        []*api.PostCallApiV1OperationsCallPostResponse
	previewResponses     []*api.PostPreviewApiV1OperationsPreviewPostResponse
	groupsResponses      []*api.GetGroupsApiV1OperationsGroupsGetResponse
	searchResponses      []*api.GetSearchApiV1OperationsSearchGetResponse
	resultQueryResponses []*api.PostResultQueryApiV1OperationsResultQueryPostResponse

	// Per-verb transport-error queues. Drain in the same order as
	// the response queues so a refresh-then-transport-failure scenario
	// can be authored.
	callErrors        []error
	previewErrors     []error
	groupsErrors      []error
	searchErrors      []error
	resultQueryErrors []error

	// refreshCount tracks how many times Refresh was invoked across
	// the whole client's lifetime. The 401 dance asserts this hits
	// exactly 1.
	refreshCount int
	// refreshErr is returned from Refresh; tests that want to model
	// a no-refresh-token / IdP-rejected refresh set this.
	refreshErr error
}

func (f *fakeOperationsClient) PostCallApiV1OperationsCallPostWithResponse(
	_ context.Context,
	params *api.PostCallApiV1OperationsCallPostParams,
	body api.PostCallApiV1OperationsCallPostJSONRequestBody,
	_ ...api.RequestEditorFn,
) (*api.PostCallApiV1OperationsCallPostResponse, error) {
	f.lastCallParams = params
	bodyCopy := body
	f.lastCallBody = &bodyCopy
	return popCallResp(&f.callResponses), popErr(&f.callErrors)
}

func (f *fakeOperationsClient) PostPreviewApiV1OperationsPreviewPostWithResponse(
	_ context.Context,
	params *api.PostPreviewApiV1OperationsPreviewPostParams,
	body api.PostPreviewApiV1OperationsPreviewPostJSONRequestBody,
	_ ...api.RequestEditorFn,
) (*api.PostPreviewApiV1OperationsPreviewPostResponse, error) {
	f.lastPreviewParams = params
	bodyCopy := body
	f.lastPreviewBody = &bodyCopy
	return popPreviewResp(&f.previewResponses), popErr(&f.previewErrors)
}

func (f *fakeOperationsClient) GetGroupsApiV1OperationsGroupsGetWithResponse(
	_ context.Context,
	params *api.GetGroupsApiV1OperationsGroupsGetParams,
	_ ...api.RequestEditorFn,
) (*api.GetGroupsApiV1OperationsGroupsGetResponse, error) {
	f.lastGroupsParams = params
	return popGroupsResp(&f.groupsResponses), popErr(&f.groupsErrors)
}

func (f *fakeOperationsClient) GetSearchApiV1OperationsSearchGetWithResponse(
	_ context.Context,
	params *api.GetSearchApiV1OperationsSearchGetParams,
	_ ...api.RequestEditorFn,
) (*api.GetSearchApiV1OperationsSearchGetResponse, error) {
	f.lastSearchParams = params
	return popSearchResp(&f.searchResponses), popErr(&f.searchErrors)
}

func (f *fakeOperationsClient) PostResultQueryApiV1OperationsResultQueryPostWithResponse(
	_ context.Context,
	params *api.PostResultQueryApiV1OperationsResultQueryPostParams,
	body api.PostResultQueryApiV1OperationsResultQueryPostJSONRequestBody,
	_ ...api.RequestEditorFn,
) (*api.PostResultQueryApiV1OperationsResultQueryPostResponse, error) {
	f.lastResultQueryParams = params
	bodyCopy := body
	f.lastResultQueryBody = &bodyCopy
	return popResultQueryResp(&f.resultQueryResponses), popErr(&f.resultQueryErrors)
}

func (f *fakeOperationsClient) Refresh(_ context.Context) error {
	f.refreshCount++
	return f.refreshErr
}

func popResultQueryResp(
	q *[]*api.PostResultQueryApiV1OperationsResultQueryPostResponse,
) *api.PostResultQueryApiV1OperationsResultQueryPostResponse {
	if len(*q) == 0 {
		return nil
	}
	r := (*q)[0]
	*q = (*q)[1:]
	return r
}

func popCallResp(q *[]*api.PostCallApiV1OperationsCallPostResponse) *api.PostCallApiV1OperationsCallPostResponse {
	if len(*q) == 0 {
		return nil
	}
	r := (*q)[0]
	*q = (*q)[1:]
	return r
}

func popPreviewResp(
	q *[]*api.PostPreviewApiV1OperationsPreviewPostResponse,
) *api.PostPreviewApiV1OperationsPreviewPostResponse {
	if len(*q) == 0 {
		return nil
	}
	r := (*q)[0]
	*q = (*q)[1:]
	return r
}

func popGroupsResp(q *[]*api.GetGroupsApiV1OperationsGroupsGetResponse) *api.GetGroupsApiV1OperationsGroupsGetResponse {
	if len(*q) == 0 {
		return nil
	}
	r := (*q)[0]
	*q = (*q)[1:]
	return r
}

func popSearchResp(q *[]*api.GetSearchApiV1OperationsSearchGetResponse) *api.GetSearchApiV1OperationsSearchGetResponse {
	if len(*q) == 0 {
		return nil
	}
	r := (*q)[0]
	*q = (*q)[1:]
	return r
}

func popErr(q *[]error) error {
	if len(*q) == 0 {
		return nil
	}
	e := (*q)[0]
	*q = (*q)[1:]
	return e
}

func makeHTTPResp(status int) *http.Response {
	return &http.Response{StatusCode: status}
}

// ---- getGroups ----

// TestGetGroupsPassesTypedConnectorIdParam — happy path asserts the
// typed ConnectorId field is populated (no raw `?connector_id=`
// URL-concat anywhere).
func TestGetGroupsPassesTypedConnectorIdParam(t *testing.T) {
	body, _ := json.Marshal(GroupsResponse{
		ConnectorID: "vault-1.x",
		Groups:      []GroupSummary{{GroupKey: "kv", Name: "KV", WhenToUse: "secrets", OperationCount: 3}},
	})
	f := &fakeOperationsClient{
		groupsResponses: []*api.GetGroupsApiV1OperationsGroupsGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	}
	got, err := getGroups(context.Background(), f, "vault-1.x")
	if err != nil {
		t.Fatalf("getGroups: %v", err)
	}
	if f.lastGroupsParams == nil || f.lastGroupsParams.ConnectorId != "vault-1.x" {
		t.Fatalf("getGroups should pass typed ConnectorId=%q; got %+v",
			"vault-1.x", f.lastGroupsParams)
	}
	if got.ConnectorID != "vault-1.x" || len(got.Groups) != 1 || got.Groups[0].GroupKey != "kv" {
		t.Fatalf("getGroups returned wrong shape: %+v", got)
	}
}

// TestGetGroupsRefreshesOn401AndRetries — the per-verb 401 dance
// mirrors api.AuthedClient.GetHealth: first call returns 401, Refresh
// runs once, second call returns 200.
func TestGetGroupsRefreshesOn401AndRetries(t *testing.T) {
	body, _ := json.Marshal(GroupsResponse{ConnectorID: "vault-1.x"})
	f := &fakeOperationsClient{
		groupsResponses: []*api.GetGroupsApiV1OperationsGroupsGetResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	}
	if _, err := getGroups(context.Background(), f, "vault-1.x"); err != nil {
		t.Fatalf("getGroups after refresh: %v", err)
	}
	if f.refreshCount != 1 {
		t.Fatalf("expected exactly one Refresh; got %d", f.refreshCount)
	}
}

// TestGetGroupsClassifies403AsApiResponseError — non-401 4xx wraps
// as *apiResponseError; renderRequestError later maps it to
// unexpected_response.
func TestGetGroupsClassifies403AsApiResponseError(t *testing.T) {
	f := &fakeOperationsClient{
		groupsResponses: []*api.GetGroupsApiV1OperationsGroupsGetResponse{
			{HTTPResponse: makeHTTPResp(403), Body: []byte(`{"detail":"forbidden"}`)},
		},
	}
	_, err := getGroups(context.Background(), f, "vault-1.x")
	if err == nil {
		t.Fatalf("expected non-2xx error; got nil")
	}
	var apiErr *apiResponseError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 403 {
		t.Fatalf("expected *apiResponseError{StatusCode:403}; got %+v", err)
	}
	if apiErr.Body != `{"detail":"forbidden"}` {
		t.Fatalf("apiResponseError.Body should preserve the response body; got %q", apiErr.Body)
	}
}

// TestGetGroupsTransportErrorPropagates — pure transport failure
// (DNS / connection-refused etc.) returns directly so
// renderRequestError can classify as unreachable.
func TestGetGroupsTransportErrorPropagates(t *testing.T) {
	transportErr := errors.New("dial tcp: lookup meho.test on 8.8.8.8: no such host")
	f := &fakeOperationsClient{
		groupsErrors: []error{transportErr},
	}
	_, err := getGroups(context.Background(), f, "vault-1.x")
	if !errors.Is(err, transportErr) {
		t.Fatalf("expected transport error to propagate verbatim; got %v", err)
	}
	var apiErr *apiResponseError
	if errors.As(err, &apiErr) {
		t.Fatalf("transport error should not wrap as *apiResponseError")
	}
}

// ---- getSearch ----

// TestGetSearchPassesTypedParams — all four params (ConnectorId,
// Query, Group, Limit) land on the typed struct; Group + Limit are
// pointer-typed so the test asserts they're set (not nil).
func TestGetSearchPassesTypedParams(t *testing.T) {
	body, _ := json.Marshal(SearchResponse{
		Hits:            []SearchHit{{OpID: "vault.kv.read", FusedScore: 0.9}},
		QueryDurationMs: 12.0,
	})
	f := &fakeOperationsClient{
		searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	}
	opts := searchOptions{
		ConnectorID: "vault-1.x",
		Query:       "secret",
		GroupKey:    "kv",
		Limit:       7,
	}
	if _, err := getSearch(context.Background(), f, opts); err != nil {
		t.Fatalf("getSearch: %v", err)
	}
	p := f.lastSearchParams
	if p == nil || p.ConnectorId != "vault-1.x" || p.Q == nil || *p.Q != "secret" {
		t.Fatalf("getSearch should pass typed ConnectorId + canonical Q; got %+v", p)
	}
	if p.Group == nil || *p.Group != "kv" {
		t.Fatalf("getSearch should pass typed Group=%q; got %+v", "kv", p.Group)
	}
	if p.Limit == nil || *p.Limit != 7 {
		t.Fatalf("getSearch should pass typed Limit=%d; got %+v", 7, p.Limit)
	}
}

// TestGetSearchOmitsOptionalParamsWhenEmpty — Group + Limit are
// nil-pointer when the operator didn't supply them, so the generator's
// omitempty form keeps them out of the URL.
func TestGetSearchOmitsOptionalParamsWhenEmpty(t *testing.T) {
	body, _ := json.Marshal(SearchResponse{Hits: nil, QueryDurationMs: 0})
	f := &fakeOperationsClient{
		searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	}
	opts := searchOptions{
		ConnectorID: "vault-1.x",
		Query:       "secret",
		GroupKey:    "", // omitted
		Limit:       0,  // omitted
	}
	if _, err := getSearch(context.Background(), f, opts); err != nil {
		t.Fatalf("getSearch: %v", err)
	}
	p := f.lastSearchParams
	if p == nil {
		t.Fatalf("getSearch should populate params struct")
	}
	if p.Group != nil {
		t.Fatalf("empty --group should leave Group=nil; got %v", *p.Group)
	}
	if p.Limit != nil {
		t.Fatalf("zero --limit should leave Limit=nil; got %v", *p.Limit)
	}
}

// ---- postCall ----

// TestPostCallTargetBareString — --target <slug> uses the bare-string
// shape (FromCallOperationBodyTarget0), not the dict shape. Verifies
// the union marshals as `"slug"`, not `{"name":"slug"}`.
func TestPostCallTargetBareString(t *testing.T) {
	cr, _ := json.Marshal(CallResult{
		Status: "ok", OpID: "vault.kv.read",
		Result:     json.RawMessage(`{"value":"secret"}`),
		DurationMs: 23,
	})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	opts := callOptions{
		ConnectorID: "vault-1.x",
		OpID:        "vault.kv.read",
		TargetName:  "rdc-vault",
	}
	if _, err := postCall(context.Background(), f, opts, nil); err != nil {
		t.Fatalf("postCall: %v", err)
	}
	b := f.lastCallBody
	if b == nil {
		t.Fatalf("postCall should populate body")
	}
	if b.ConnectorId != "vault-1.x" || b.OpId != "vault.kv.read" {
		t.Fatalf("body should carry typed connector_id + op_id; got %+v", b)
	}
	if b.Target == nil {
		t.Fatalf("body.Target should be non-nil when --target is set")
	}
	// Round-trip the union via its MarshalJSON to verify the bare-string
	// shape — the union's internal json.RawMessage is set by
	// FromCallOperationBodyTarget0 so MarshalJSON should emit `"rdc-vault"`.
	raw, err := b.Target.MarshalJSON()
	if err != nil {
		t.Fatalf("target.MarshalJSON: %v", err)
	}
	want := `"rdc-vault"`
	if string(raw) != want {
		t.Fatalf("--target should marshal as bare string %q; got %q", want, string(raw))
	}
	// AsCallOperationBodyTarget0 should round-trip the same value.
	bare, err := b.Target.AsCallOperationBodyTarget0()
	if err != nil {
		t.Fatalf("AsCallOperationBodyTarget0: %v", err)
	}
	if bare != "rdc-vault" {
		t.Fatalf("round-tripped bare-string target: got %q; want %q", bare, "rdc-vault")
	}
}

// TestPostCallTargetNilWhenOmitted — --target omitted leaves the
// generated body's Target pointer nil, so the JSON serialiser emits
// `"target": null` (the generator's CallOperationBody.Target carries
// a `json:"target"` tag without omitempty; the route accepts null).
func TestPostCallTargetNilWhenOmitted(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok", OpID: "k8s.about", DurationMs: 1})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	opts := callOptions{
		ConnectorID: "k8s-1.x",
		OpID:        "k8s.about",
		TargetName:  "",
	}
	if _, err := postCall(context.Background(), f, opts, nil); err != nil {
		t.Fatalf("postCall: %v", err)
	}
	if f.lastCallBody.Target != nil {
		t.Fatalf("--target omitted should leave body.Target=nil; got %+v", f.lastCallBody.Target)
	}
	// Marshal the whole body and verify "target":null is on the wire.
	raw, err := json.Marshal(f.lastCallBody)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	if !bytes.Contains(raw, []byte(`"target":null`)) {
		t.Fatalf("expected `\"target\":null` on the wire; got %s", string(raw))
	}
}

// TestPostCallParamsSetWhenSupplied — non-nil params land on the
// body.Params pointer; nil params leave it nil so the wire omits
// the key (the generator's CallOperationBody.Params carries
// `json:"params,omitempty"`).
func TestPostCallParamsSetWhenSupplied(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok", OpID: "vault.kv.read", DurationMs: 1})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	opts := callOptions{
		ConnectorID: "vault-1.x",
		OpID:        "vault.kv.read",
		TargetName:  "rdc-vault",
	}
	params := map[string]any{"path": "secret/foo"}
	if _, err := postCall(context.Background(), f, opts, params); err != nil {
		t.Fatalf("postCall: %v", err)
	}
	if f.lastCallBody.Params == nil {
		t.Fatalf("params should be set on body.Params; got nil")
	}
	got := *f.lastCallBody.Params
	if got["path"] != "secret/foo" {
		t.Fatalf("params not threaded through; got %v", got)
	}
}

// TestPostCallParamsNilWhenOmitted — empty --params leaves the
// body.Params pointer nil so the wire omits the key entirely.
func TestPostCallParamsNilWhenOmitted(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok", OpID: "k8s.about", DurationMs: 1})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	opts := callOptions{
		ConnectorID: "k8s-1.x",
		OpID:        "k8s.about",
	}
	if _, err := postCall(context.Background(), f, opts, nil); err != nil {
		t.Fatalf("postCall: %v", err)
	}
	if f.lastCallBody.Params != nil {
		t.Fatalf("nil params should leave body.Params=nil; got %+v", f.lastCallBody.Params)
	}
	raw, err := json.Marshal(f.lastCallBody)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	if bytes.Contains(raw, []byte(`"params"`)) {
		t.Fatalf("omitted params should not appear on the wire; got %s", string(raw))
	}
}

// TestPostCallRefreshOn401 — same one-shot refresh dance as
// TestGetGroupsRefreshesOn401AndRetries, exercised through postCall.
func TestPostCallRefreshOn401(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok", OpID: "vault.kv.read", DurationMs: 1})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	opts := callOptions{ConnectorID: "vault-1.x", OpID: "vault.kv.read", TargetName: "rdc-vault"}
	if _, err := postCall(context.Background(), f, opts, nil); err != nil {
		t.Fatalf("postCall after refresh: %v", err)
	}
	if f.refreshCount != 1 {
		t.Fatalf("expected exactly one Refresh; got %d", f.refreshCount)
	}
}

// TestPostCallRefreshFailurePropagates — Refresh returning a
// no-refresh-token error propagates so the verb's renderer can map
// it to auth_expired.
func TestPostCallRefreshFailurePropagates(t *testing.T) {
	refreshErr := errors.New("meho: stored token has no refresh_token")
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
		},
		refreshErr: refreshErr,
	}
	opts := callOptions{ConnectorID: "vault-1.x", OpID: "vault.kv.read"}
	_, err := postCall(context.Background(), f, opts, nil)
	if !errors.Is(err, refreshErr) {
		t.Fatalf("expected refreshErr to propagate; got %v", err)
	}
}

// TestPostCallNon2xxAfterRefreshClassifiesAsApiResponseError —
// 401 → Refresh succeeds → second call returns 500 → wrapped as
// *apiResponseError so renderRequestError maps to
// unexpected_response.
func TestPostCallNon2xxAfterRefreshClassifiesAsApiResponseError(t *testing.T) {
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			{HTTPResponse: makeHTTPResp(500), Body: []byte(`{"detail":"backplane unavailable"}`)},
		},
	}
	opts := callOptions{ConnectorID: "vault-1.x", OpID: "vault.kv.read"}
	_, err := postCall(context.Background(), f, opts, nil)
	if err == nil {
		t.Fatalf("expected error; got nil")
	}
	var apiErr *apiResponseError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 500 {
		t.Fatalf("expected *apiResponseError{StatusCode:500}; got %+v", err)
	}
}

// withFakeClient swaps newAuthedClient for a factory returning f and
// restores the original on cleanup, so a full runCall path can be
// exercised without a live backplane or token store.
func withFakeClient(t *testing.T, f operationsAPI) {
	t.Helper()
	orig := newAuthedClient
	newAuthedClient = func(_ context.Context, _ string) (operationsAPI, error) { return f, nil }
	t.Cleanup(func() { newAuthedClient = orig })
}

// TestRunCallAwaitingApprovalRealPath — the generic `operation call`
// verb treats status=awaiting_approval as a parked, non-error,
// exit-0 outcome on the REAL runCall path (not just printCallResult):
// stdout carries the parked hint and stderr never carries the
// invalid-status diagnostic.
func TestRunCallAwaitingApprovalRealPath(t *testing.T) {
	cr, _ := json.Marshal(CallResult{
		Status: "awaiting_approval", OpID: "argocd.app.sync", DurationMs: 7,
		Extras: json.RawMessage(`{"approval_request_id":"ar-op-1"}`),
	})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	withFakeClient(t, f)

	cmd := newCallCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{"argocd-api-3.x", "argocd.app.sync", "--target", "rdc-argocd", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("awaiting_approval must not be an error (parked, exit 0); got %v", err)
	}
	if !strings.Contains(out.String(), "parked for human approval") {
		t.Errorf("expected parked hint on stdout; got %q", out.String())
	}
	if strings.Contains(errBuf.String(), "invalid OperationResult") {
		t.Errorf("awaiting_approval was wrongly rejected as invalid status: %s", errBuf.String())
	}
}

// TestRunCallAwaitingApprovalJSON — with --json the parked envelope
// round-trips as the full OperationResult JSON (incl.
// extras.approval_request_id) and the command exits 0.
func TestRunCallAwaitingApprovalJSON(t *testing.T) {
	cr, _ := json.Marshal(CallResult{
		Status: "awaiting_approval", OpID: "argocd.app.sync",
		Extras: json.RawMessage(`{"approval_request_id":"ar-op-1"}`),
	})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	withFakeClient(t, f)

	cmd := newCallCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"argocd-api-3.x", "argocd.app.sync", "--target", "rdc-argocd", "--json", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatalf("--json output is not valid JSON: %v\n%s", err, out.String())
	}
	if decoded["status"] != "awaiting_approval" {
		t.Errorf("json status: got %v want awaiting_approval", decoded["status"])
	}
	extras, ok := decoded["extras"].(map[string]any)
	if !ok || extras["approval_request_id"] != "ar-op-1" {
		t.Errorf("json envelope must carry extras.approval_request_id; got %v", decoded["extras"])
	}
}

// ---- checks_alert_advisory (#2718) reach on the operator CLI ----
//
// The backend attaches extras["checks_alert_advisory"] to SUCCESSFUL
// dispatch responses (status=ok). These two tests pin what each operator
// output mode actually does with it, because the two disagree and
// docs/codebase/checks-advisory.md § "Operator reach" documents the gap:
// --json passes the envelope through verbatim, while printCallResult
// returns inside its status=="ok" branch before reaching the extras
// block, so the default human render drops it. Teaching the human render
// to print extras on success is a CLI-wide UX change (it would also start
// printing #2550's target_activity_advisory, and the vendor verbs render
// through dispatch.Render), so #2718 scoped it out — but the boundary is
// now asserted rather than assumed, and either half failing means the
// boundary moved without the doc moving with it.

const checksAdvisoryExtras = `{"checks_alert_advisory":[` +
	`{"dashboard_id":"d1","name":"prod-health","state":"critical"}]}`

// TestCallJSONCarriesChecksAlertAdvisoryOnOK — with --json, a status=ok
// envelope round-trips the advisory fragment untouched. This is the
// operator-reachable path today.
func TestCallJSONCarriesChecksAlertAdvisoryOnOK(t *testing.T) {
	cr, _ := json.Marshal(CallResult{
		Status: "ok", OpID: "vault.kv.read",
		Result:     json.RawMessage(`{"value":"secret"}`),
		Extras:     json.RawMessage(checksAdvisoryExtras),
		DurationMs: 12,
	})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	withFakeClient(t, f)

	cmd := newCallCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"vault-1.x", "vault.kv.read", "--target", "rdc-vault", "--json", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("status=ok must exit 0; got %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatalf("--json output is not valid JSON: %v\n%s", err, out.String())
	}
	extras, ok := decoded["extras"].(map[string]any)
	if !ok {
		t.Fatalf("--json envelope must carry extras on status=ok; got %v", decoded["extras"])
	}
	advisory, ok := extras["checks_alert_advisory"].([]any)
	if !ok || len(advisory) != 1 {
		t.Fatalf("expected one checks_alert_advisory entry; got %v", extras["checks_alert_advisory"])
	}
	entry, ok := advisory[0].(map[string]any)
	if !ok {
		t.Fatalf("advisory entry should be an object; got %T", advisory[0])
	}
	for k, want := range map[string]string{
		"dashboard_id": "d1", "name": "prod-health", "state": "critical",
	} {
		if entry[k] != want {
			t.Errorf("advisory entry %s: got %v want %q", k, entry[k], want)
		}
	}
}

// TestCallHumanRenderOmitsExtrasOnOK — without --json the default human
// render prints the result and returns; extras (advisory included) are
// printed only for non-ok statuses. Pinning the gap keeps
// docs/codebase/checks-advisory.md honest: close it and this test tells
// you the doc's table needs updating.
func TestCallHumanRenderOmitsExtrasOnOK(t *testing.T) {
	cr, _ := json.Marshal(CallResult{
		Status: "ok", OpID: "vault.kv.read",
		Result:     json.RawMessage(`{"value":"secret"}`),
		Extras:     json.RawMessage(checksAdvisoryExtras),
		DurationMs: 12,
	})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	withFakeClient(t, f)

	cmd := newCallCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"vault-1.x", "vault.kv.read", "--target", "rdc-vault", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("status=ok must exit 0; got %v", err)
	}
	if !strings.Contains(out.String(), "status=ok") || !strings.Contains(out.String(), "secret") {
		t.Fatalf("human render should show the status line + result; got %q", out.String())
	}
	if strings.Contains(out.String(), "checks_alert_advisory") {
		t.Errorf("human render printed extras on status=ok — the CLI gap closed; "+
			"update docs/codebase/checks-advisory.md § Operator reach and the "+
			"CHANGELOG bullet, then flip this assertion. Output: %q", out.String())
	}
}

// ---- postResultQuery (#3179) ----

func makeHandleUUID(t *testing.T, s string) openapi_types.UUID {
	t.Helper()
	var u openapi_types.UUID
	if err := u.UnmarshalText([]byte(s)); err != nil {
		t.Fatalf("parse handle uuid %q: %v", s, err)
	}
	return u
}

// TestPostResultQueryPassesTypedBody — the happy path asserts the typed
// body carries HandleId + Offset + Limit pointers (no raw URL/body
// concatenation) and the 200 envelope decodes into ResultQueryResult.
func TestPostResultQueryPassesTypedBody(t *testing.T) {
	rq, _ := json.Marshal(ResultQueryResult{
		HandleID:     "11111111-1111-1111-1111-111111111111",
		Rows:         []json.RawMessage{json.RawMessage(`{"i":5}`)},
		Offset:       5,
		Limit:        50,
		ReturnedRows: 1,
		TotalRows:    60,
		StoredRows:   60,
		Truncated:    false,
	})
	f := &fakeOperationsClient{
		resultQueryResponses: []*api.PostResultQueryApiV1OperationsResultQueryPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: rq},
		},
	}
	handle := makeHandleUUID(t, "11111111-1111-1111-1111-111111111111")
	got, err := postResultQuery(context.Background(), f, handle, 5, 50, nil)
	if err != nil {
		t.Fatalf("postResultQuery: %v", err)
	}
	b := f.lastResultQueryBody
	if b == nil {
		t.Fatalf("postResultQuery should populate body")
	}
	if b.HandleId != handle {
		t.Fatalf("body should carry typed HandleId=%v; got %v", handle, b.HandleId)
	}
	if b.Offset == nil || *b.Offset != 5 {
		t.Fatalf("body should carry Offset=5; got %+v", b.Offset)
	}
	if b.Limit == nil || *b.Limit != 50 {
		t.Fatalf("body should carry Limit=50; got %+v", b.Limit)
	}
	if got.TotalRows != 60 || got.ReturnedRows != 1 || got.Offset != 5 {
		t.Fatalf("decoded result-query envelope wrong shape: %+v", got)
	}
}

// TestPostResultQueryRefreshOn401 — same one-shot refresh dance as the
// sibling verbs, exercised through postResultQuery.
func TestPostResultQueryRefreshOn401(t *testing.T) {
	rq, _ := json.Marshal(ResultQueryResult{HandleID: "h", TotalRows: 0})
	f := &fakeOperationsClient{
		resultQueryResponses: []*api.PostResultQueryApiV1OperationsResultQueryPostResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			{HTTPResponse: makeHTTPResp(200), Body: rq},
		},
	}
	handle := makeHandleUUID(t, "11111111-1111-1111-1111-111111111111")
	if _, err := postResultQuery(context.Background(), f, handle, 0, 50, nil); err != nil {
		t.Fatalf("postResultQuery after refresh: %v", err)
	}
	if f.refreshCount != 1 {
		t.Fatalf("expected exactly one Refresh; got %d", f.refreshCount)
	}
}

// TestPostResultQueryNotFoundClassifiesAsApiResponseError — the 404
// handle-not-found miss wraps as *apiResponseError (so renderRequestError
// maps it to unexpected_response) and preserves the structured
// reason=handle_not_found detail in the body for the operator to read.
func TestPostResultQueryNotFoundClassifiesAsApiResponseError(t *testing.T) {
	notFound := `{"detail":{"reason":"handle_not_found","handle_id":"11111111-1111-1111-1111-111111111111"}}`
	f := &fakeOperationsClient{
		resultQueryResponses: []*api.PostResultQueryApiV1OperationsResultQueryPostResponse{
			{HTTPResponse: makeHTTPResp(404), Body: []byte(notFound)},
		},
	}
	handle := makeHandleUUID(t, "11111111-1111-1111-1111-111111111111")
	_, err := postResultQuery(context.Background(), f, handle, 0, 50, nil)
	if err == nil {
		t.Fatalf("expected non-2xx error; got nil")
	}
	var apiErr *apiResponseError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 404 {
		t.Fatalf("expected *apiResponseError{StatusCode:404}; got %+v", err)
	}
	if !strings.Contains(apiErr.Body, "handle_not_found") {
		t.Fatalf("404 body should preserve the reason=handle_not_found detail; got %q", apiErr.Body)
	}
}

// TestPostResultQueryTransportErrorPropagates — a pure transport failure
// returns verbatim so renderRequestError can classify it as unreachable.
func TestPostResultQueryTransportErrorPropagates(t *testing.T) {
	transportErr := errors.New("dial tcp: connection refused")
	f := &fakeOperationsClient{resultQueryErrors: []error{transportErr}}
	handle := makeHandleUUID(t, "11111111-1111-1111-1111-111111111111")
	_, err := postResultQuery(context.Background(), f, handle, 0, 50, nil)
	if !errors.Is(err, transportErr) {
		t.Fatalf("expected transport error to propagate verbatim; got %v", err)
	}
	var apiErr *apiResponseError
	if errors.As(err, &apiErr) {
		t.Fatalf("transport error should not wrap as *apiResponseError")
	}
}

// TestRunResultQueryInvalidUUIDShortCircuits — a malformed handle_id is
// caught CLI-side (before the backplane is even resolved), surfacing as
// unexpected_response with the "not a valid UUID" hint. No client call
// is made.
func TestRunResultQueryInvalidUUIDShortCircuits(t *testing.T) {
	f := &fakeOperationsClient{}
	withFakeClient(t, f)

	cmd := newResultQueryCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{"not-a-uuid", "--backplane", "https://x"})
	// The invalid-UUID branch returns the rendered StructuredError (nil
	// process error on the human path); assert the diagnostic reached stderr.
	_ = cmd.Execute()
	if !strings.Contains(errBuf.String(), "not a valid UUID") {
		t.Fatalf("expected 'not a valid UUID' diagnostic on stderr; got %q", errBuf.String())
	}
	if f.lastResultQueryBody != nil {
		t.Fatalf("invalid UUID should short-circuit before any client call; got body %+v", f.lastResultQueryBody)
	}
}

// TestRunResultQueryHappyPathRendersWindow — the full runResultQuery path
// (real cobra command + fake client) renders the window header + rows on
// stdout and exits 0.
func TestRunResultQueryHappyPathRendersWindow(t *testing.T) {
	rq, _ := json.Marshal(ResultQueryResult{
		HandleID:     "11111111-1111-1111-1111-111111111111",
		Rows:         []json.RawMessage{json.RawMessage(`{"i":0}`), json.RawMessage(`{"i":1}`)},
		Offset:       0,
		Limit:        50,
		ReturnedRows: 2,
		TotalRows:    2,
		StoredRows:   2,
	})
	f := &fakeOperationsClient{
		resultQueryResponses: []*api.PostResultQueryApiV1OperationsResultQueryPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: rq},
		},
	}
	withFakeClient(t, f)

	cmd := newResultQueryCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"11111111-1111-1111-1111-111111111111", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	for _, want := range []string{"rows 0..2 of 2", `"i": 0`, `"i": 1`} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("result-query render missing %q in output:\n%s", want, out.String())
		}
	}
}

// ---- postPreview ----

// okPreviewBody is the canned status=ok preview envelope the postPreview
// / runPreview tests decode. It carries the load-bearing preview_hash
// plus the resolved-request projection.
func okPreviewBody(t *testing.T) []byte {
	t.Helper()
	pr, err := json.Marshal(PreviewResult{
		Status:       "ok",
		OpID:         "vmware.composite.vm.destroy",
		ConnectorID:  "vmware-rest-9.0",
		SourceKind:   "composite",
		Method:       "COMPOSITE",
		ResolvedPath: "vmware.composite.vm.destroy",
		RedactedBody: json.RawMessage(`{"vm":"vm-1812"}`),
		PreviewHash:  "abc123def456",
	})
	if err != nil {
		t.Fatalf("marshal preview body: %v", err)
	}
	return pr
}

// TestPostPreviewThreadsTargetAndParams — --target lands on the
// bare-string oneOf shape and non-nil params thread onto body.Params,
// mirroring postCall's plumbing.
func TestPostPreviewThreadsTargetAndParams(t *testing.T) {
	f := &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: okPreviewBody(t)},
		},
	}
	opts := previewOptions{
		ConnectorID: "vmware-rest-9.0",
		OpID:        "vmware.composite.vm.destroy",
		TargetName:  "rdc-vcenter",
	}
	params := map[string]any{"vm": "vm-1812"}
	if _, err := postPreview(context.Background(), f, opts, params); err != nil {
		t.Fatalf("postPreview: %v", err)
	}
	if f.lastPreviewBody.Target == nil {
		t.Fatalf("--target should set body.Target; got nil")
	}
	gotTarget, err := f.lastPreviewBody.Target.AsPreviewOperationBodyTarget0()
	if err != nil {
		t.Fatalf("target not the bare-string shape: %v", err)
	}
	if gotTarget != "rdc-vcenter" {
		t.Fatalf("target not threaded; got %q", gotTarget)
	}
	if f.lastPreviewBody.Params == nil || (*f.lastPreviewBody.Params)["vm"] != "vm-1812" {
		t.Fatalf("params not threaded; got %+v", f.lastPreviewBody.Params)
	}
}

// TestPostPreviewTargetOmittedNullOnWire — omitting --target leaves
// body.Target nil so the wire emits `"target":null`, same contract as
// postCall.
func TestPostPreviewTargetOmittedNullOnWire(t *testing.T) {
	f := &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: okPreviewBody(t)},
		},
	}
	opts := previewOptions{ConnectorID: "k8s-1.x", OpID: "k8s.about"}
	if _, err := postPreview(context.Background(), f, opts, nil); err != nil {
		t.Fatalf("postPreview: %v", err)
	}
	if f.lastPreviewBody.Target != nil {
		t.Fatalf("omitted --target should leave body.Target=nil; got %+v", f.lastPreviewBody.Target)
	}
	raw, err := json.Marshal(f.lastPreviewBody)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	if !bytes.Contains(raw, []byte(`"target":null`)) {
		t.Fatalf("expected `\"target\":null` on the wire; got %s", string(raw))
	}
}

// TestPostPreviewRefreshOn401 — the one-shot refresh dance fires exactly
// once on a 401, then the retried call decodes cleanly.
func TestPostPreviewRefreshOn401(t *testing.T) {
	f := &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			{HTTPResponse: makeHTTPResp(200), Body: okPreviewBody(t)},
		},
	}
	opts := previewOptions{ConnectorID: "vmware-rest-9.0", OpID: "vmware.composite.vm.destroy", TargetName: "rdc-vcenter"}
	if _, err := postPreview(context.Background(), f, opts, nil); err != nil {
		t.Fatalf("postPreview after refresh: %v", err)
	}
	if f.refreshCount != 1 {
		t.Fatalf("expected exactly one Refresh; got %d", f.refreshCount)
	}
}

// TestPostPreviewNon2xxClassifiesAsApiResponseError — a 500 wraps as
// *apiResponseError so renderRequestError maps it to unexpected_response.
func TestPostPreviewNon2xxClassifiesAsApiResponseError(t *testing.T) {
	f := &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(500), Body: []byte(`{"detail":"backplane unavailable"}`)},
		},
	}
	opts := previewOptions{ConnectorID: "vmware-rest-9.0", OpID: "vmware.composite.vm.destroy"}
	_, err := postPreview(context.Background(), f, opts, nil)
	var apiErr *apiResponseError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 500 {
		t.Fatalf("expected *apiResponseError{StatusCode:500}; got %+v", err)
	}
}

// TestRunPreviewOkPrintsHashRealPath — the full runPreview path (real
// cobra command + fake client) prints the preview_hash prominently on
// stdout and exits 0.
func TestRunPreviewOkPrintsHashRealPath(t *testing.T) {
	f := &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: okPreviewBody(t)},
		},
	}
	withFakeClient(t, f)

	cmd := newPreviewCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"vmware-rest-9.0", "vmware.composite.vm.destroy",
		"--target", "rdc-vcenter", "--params", `{"vm":"vm-1812"}`, "--backplane", "https://x",
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("ok preview must exit 0; got %v", err)
	}
	for _, want := range []string{"status=ok", "preview_hash: abc123def456", "--preview-hash abc123def456"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("preview render missing %q in output:\n%s", want, out.String())
		}
	}
}

// TestRunPreviewErrorExitsNonZero — a status=error envelope (unknown op,
// invalid params, unresolvable target) surfaces the error and exits 1
// via errOpError, same gate-failed semantic as `operation call`.
func TestRunPreviewErrorExitsNonZero(t *testing.T) {
	errMsg := "unknown_op: vmware.bogus"
	pr, _ := json.Marshal(PreviewResult{
		Status:      "error",
		OpID:        "vmware.bogus",
		ConnectorID: "vmware-rest-9.0",
		Error:       &errMsg,
		Extras:      json.RawMessage(`{"error_code":"unknown_op"}`),
	})
	f := &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: pr},
		},
	}
	withFakeClient(t, f)

	cmd := newPreviewCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"vmware-rest-9.0", "vmware.bogus", "--backplane", "https://x"})
	if err := cmd.Execute(); !errors.Is(err, errOpError) {
		t.Fatalf("status=error should exit via errOpError; got %v", err)
	}
	for _, want := range []string{"status=error", "unknown_op: vmware.bogus", "error_code"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("error preview render missing %q in output:\n%s", want, out.String())
		}
	}
}

// TestRunPreviewInvalidStatusRejected — a status the preview contract
// does not define (ok/error/unavailable) is a malformed response:
// surface as unexpected_response on stderr, print no envelope on stdout.
func TestRunPreviewInvalidStatusRejected(t *testing.T) {
	pr, _ := json.Marshal(PreviewResult{Status: "weird", OpID: "x", ConnectorID: "c"})
	f := &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: pr},
		},
	}
	withFakeClient(t, f)

	cmd := newPreviewCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{"c", "x", "--backplane", "https://x"})
	_ = cmd.Execute()
	if !strings.Contains(errBuf.String(), "invalid preview status") {
		t.Errorf("expected invalid-status diagnostic on stderr; got %q", errBuf.String())
	}
	if strings.Contains(out.String(), "status=weird") {
		t.Errorf("malformed status must not render an envelope on stdout; got %q", out.String())
	}
}

// TestPostCallThreadsPreviewHash — --preview-hash lands on
// body.PreviewHash so a destructive-tier dispatch carries the binding
// (#3197); an unset flag leaves it nil (bare call byte-identical to
// pre-#3197).
func TestPostCallThreadsPreviewHash(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok", OpID: "vmware.composite.vm.destroy", DurationMs: 1})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	opts := callOptions{
		ConnectorID: "vmware-rest-9.0",
		OpID:        "vmware.composite.vm.destroy",
		TargetName:  "rdc-vcenter",
		PreviewHash: "abc123def456",
	}
	if _, err := postCall(context.Background(), f, opts, map[string]any{"vm": "vm-1812"}); err != nil {
		t.Fatalf("postCall: %v", err)
	}
	if f.lastCallBody.PreviewHash == nil || *f.lastCallBody.PreviewHash != "abc123def456" {
		t.Fatalf("preview hash not threaded onto body.PreviewHash; got %+v", f.lastCallBody.PreviewHash)
	}
}

// TestPostCallPreviewHashNilWhenOmitted — no --preview-hash leaves
// body.PreviewHash nil.
func TestPostCallPreviewHashNilWhenOmitted(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok", OpID: "vault.kv.read", DurationMs: 1})
	f := &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	}
	opts := callOptions{ConnectorID: "vault-1.x", OpID: "vault.kv.read", TargetName: "rdc-vault"}
	if _, err := postCall(context.Background(), f, opts, nil); err != nil {
		t.Fatalf("postCall: %v", err)
	}
	if f.lastCallBody.PreviewHash != nil {
		t.Fatalf("omitted --preview-hash should leave body.PreviewHash=nil; got %+v", f.lastCallBody.PreviewHash)
	}
}
