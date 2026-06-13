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
	lastCallParams   *api.PostCallApiV1OperationsCallPostParams
	lastCallBody     *api.CallOperationBody
	lastGroupsParams *api.GetGroupsApiV1OperationsGroupsGetParams
	lastSearchParams *api.GetSearchApiV1OperationsSearchGetParams

	// Sequenced canned responses — pop one per call (per verb). Tests
	// register two responses on the auth-refresh scenarios (first a
	// 401, then the post-refresh outcome) and one on every other
	// path.
	callResponses   []*api.PostCallApiV1OperationsCallPostResponse
	groupsResponses []*api.GetGroupsApiV1OperationsGroupsGetResponse
	searchResponses []*api.GetSearchApiV1OperationsSearchGetResponse

	// Per-verb transport-error queues. Drain in the same order as
	// the response queues so a refresh-then-transport-failure scenario
	// can be authored.
	callErrors   []error
	groupsErrors []error
	searchErrors []error

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

func (f *fakeOperationsClient) Refresh(_ context.Context) error {
	f.refreshCount++
	return f.refreshErr
}

func popCallResp(q *[]*api.PostCallApiV1OperationsCallPostResponse) *api.PostCallApiV1OperationsCallPostResponse {
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
	if p == nil || p.ConnectorId != "vault-1.x" || p.Query != "secret" {
		t.Fatalf("getSearch should pass typed ConnectorId+Query; got %+v", p)
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
	cmd.SetArgs([]string{"argocd-api-3.x", "argocd.app.sync", "--target", "rdc-argocd", "--backplane", "http://x"})
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
	cmd.SetArgs([]string{"argocd-api-3.x", "argocd.app.sync", "--target", "rdc-argocd", "--json", "--backplane", "http://x"})
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
