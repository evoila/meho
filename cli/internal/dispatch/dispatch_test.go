// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

// fakeOperationsClient is the dispatch-test double satisfying the
// package-private operationsAPI interface. Per-test setup wires
// canned responses (status, body, error) for each verb; the fake
// records the typed params + body so wire-shape assertions don't
// have to go through httptest.
type fakeOperationsClient struct {
	lastCallParams   *api.PostCallApiV1OperationsCallPostParams
	lastCallBody     *api.CallOperationBody
	lastSearchParams *api.GetSearchApiV1OperationsSearchGetParams

	// Sequenced canned responses — pop one per call. Tests register
	// two responses on the auth-refresh scenarios (first a 401, then
	// the post-refresh outcome) and one on every other path.
	callResponses   []*api.PostCallApiV1OperationsCallPostResponse
	searchResponses []*api.GetSearchApiV1OperationsSearchGetResponse

	// Per-verb transport-error queues. Drained in lockstep with the
	// response queues so a refresh-then-transport-failure scenario
	// can be authored.
	callErrors   []error
	searchErrors []error

	// refreshCount tracks how many times Refresh was invoked across
	// the fake's lifetime. The 401 dance asserts this hits exactly 1.
	refreshCount int
	// refreshErr is returned from Refresh; tests that want to model a
	// no-refresh-token / IdP-rejected refresh set this.
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

// withFake swaps the package-level newAuthedClient factory for the
// supplied fake for the duration of one test, then restores the
// production factory via t.Cleanup. Returns the fake so the test
// body can inspect recorded params + tweak canned responses.
func withFake(t *testing.T, fake *fakeOperationsClient) *fakeOperationsClient {
	t.Helper()
	prev := newAuthedClient
	newAuthedClient = func(_ context.Context, _ string) (operationsAPI, error) {
		return fake, nil
	}
	t.Cleanup(func() { newAuthedClient = prev })
	return fake
}

// ---- Call / CallWithTarget ----

// TestCallBakesConnectorIDAndDictTarget — happy path asserts the
// typed body carries the pre-baked connector_id and the dict-shape
// target form (FromCallOperationBodyTarget1) — the wire shape every
// pre-#1274 per-vendor verb has been sending. The bare-string shape
// (FromCallOperationBodyTarget0) is operationally identical (the
// backend normalises both); keeping the dict shape preserves byte-
// identical wire output for the existing per-vendor test suites
// that assert on body.Target["name"].
func TestCallBakesConnectorIDAndDictTarget(t *testing.T) {
	cr, _ := json.Marshal(CallResult{
		Status: "ok", OpID: "vault.kv.read", DurationMs: 1,
	})
	f := withFake(t, &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	})
	c := New("vault-1.x")
	if _, err := c.Call(context.Background(), "https://bp", "vault.kv.read", "rdc-vault", map[string]any{"path": "p"}); err != nil {
		t.Fatalf("Call: %v", err)
	}
	if f.lastCallBody == nil {
		t.Fatalf("Call should populate body")
	}
	if f.lastCallBody.ConnectorId != "vault-1.x" || f.lastCallBody.OpId != "vault.kv.read" {
		t.Fatalf("body should carry typed connector_id + op_id; got %+v", f.lastCallBody)
	}
	if f.lastCallBody.Target == nil {
		t.Fatalf("body.Target should be non-nil when --target is set")
	}
	got, err := f.lastCallBody.Target.AsCallOperationBodyTarget1()
	if err != nil {
		t.Fatalf("AsCallOperationBodyTarget1: %v", err)
	}
	if got["name"] != "rdc-vault" {
		t.Fatalf("Call should encode target as dict {\"name\": \"rdc-vault\"}; got %+v", got)
	}
	if f.lastCallBody.Params == nil || (*f.lastCallBody.Params)["path"] != "p" {
		t.Fatalf("params not threaded: %+v", f.lastCallBody.Params)
	}
}

// TestCallEmptyTargetSerialisesNull — empty targetSlug leaves the
// body.Target pointer nil so the JSON serialiser emits `"target":null`
// (the generator's CallOperationBody.Target carries `json:"target"`
// without omitempty; the route accepts null).
func TestCallEmptyTargetSerialisesNull(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok"})
	f := withFake(t, &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	})
	c := New("c")
	if _, err := c.Call(context.Background(), "https://bp", "op", "", nil); err != nil {
		t.Fatalf("Call: %v", err)
	}
	if f.lastCallBody.Target != nil {
		t.Fatalf("empty target should leave body.Target=nil; got %+v", f.lastCallBody.Target)
	}
	raw, err := json.Marshal(f.lastCallBody)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	if !bytes.Contains(raw, []byte(`"target":null`)) {
		t.Fatalf("expected `\"target\":null` on the wire; got %s", string(raw))
	}
}

// TestCallWithTargetThreadsExtraKeysAsDict — CallWithTarget uses the
// dict-shape target form (FromCallOperationBodyTarget1) so callers
// can thread extra keys like "fqdn" alongside "name".
func TestCallWithTargetThreadsExtraKeysAsDict(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok"})
	f := withFake(t, &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	})
	c := New("vcfa-rest-9.0")
	target := map[string]any{"name": "vcfa", "fqdn": "vra.lab"}
	if _, err := c.CallWithTarget(context.Background(), "https://bp", "op", target, nil); err != nil {
		t.Fatalf("CallWithTarget: %v", err)
	}
	if f.lastCallBody.Target == nil {
		t.Fatalf("body.Target should be set")
	}
	got, err := f.lastCallBody.Target.AsCallOperationBodyTarget1()
	if err != nil {
		t.Fatalf("AsCallOperationBodyTarget1: %v", err)
	}
	if got["fqdn"] != "vra.lab" || got["name"] != "vcfa" {
		t.Fatalf("fqdn/name not threaded into dict target: %+v", got)
	}
}

// TestCallRefreshesOn401AndRetries — the 401 dance mirrors
// api.AuthedClient.GetHealth: first call returns 401, Refresh runs
// once, second call returns 200.
func TestCallRefreshesOn401AndRetries(t *testing.T) {
	cr, _ := json.Marshal(CallResult{Status: "ok"})
	f := withFake(t, &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			{HTTPResponse: makeHTTPResp(200), Body: cr},
		},
	})
	c := New("c")
	if _, err := c.Call(context.Background(), "https://bp", "op", "t", nil); err != nil {
		t.Fatalf("Call after refresh: %v", err)
	}
	if f.refreshCount != 1 {
		t.Fatalf("expected exactly one Refresh; got %d", f.refreshCount)
	}
}

// TestCallClassifies403AsAPIResponseError — non-401 4xx wraps as
// *APIResponseError; renderRequestError later maps it to unexpected.
func TestCallClassifies403AsAPIResponseError(t *testing.T) {
	withFake(t, &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(403), Body: []byte(`{"detail":"forbidden"}`)},
		},
	})
	c := New("c")
	_, err := c.Call(context.Background(), "https://bp", "op", "t", nil)
	if err == nil {
		t.Fatalf("expected non-2xx error; got nil")
	}
	var apiErr *APIResponseError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 403 {
		t.Fatalf("expected *APIResponseError{StatusCode:403}; got %+v", err)
	}
	if apiErr.Body != `{"detail":"forbidden"}` {
		t.Fatalf("APIResponseError.Body should preserve the response body; got %q", apiErr.Body)
	}
}

// TestCallTransportErrorPropagates — pure transport failure (DNS /
// connection-refused etc.) returns directly so renderRequestError can
// classify as unreachable.
func TestCallTransportErrorPropagates(t *testing.T) {
	transportErr := errors.New("dial tcp: lookup meho.test on 8.8.8.8: no such host")
	withFake(t, &fakeOperationsClient{callErrors: []error{transportErr}})
	c := New("c")
	_, err := c.Call(context.Background(), "https://bp", "op", "t", nil)
	if !errors.Is(err, transportErr) {
		t.Fatalf("expected transport error to propagate verbatim; got %v", err)
	}
	var apiErr *APIResponseError
	if errors.As(err, &apiErr) {
		t.Fatalf("transport error should not wrap as *APIResponseError")
	}
}

// TestCallRefreshFailurePropagates — Refresh returning a no-refresh-
// token error propagates so the verb's renderer can map it to
// auth_expired.
func TestCallRefreshFailurePropagates(t *testing.T) {
	refreshErr := errors.New("meho: stored token has no refresh_token")
	withFake(t, &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
		},
		refreshErr: refreshErr,
	})
	c := New("c")
	_, err := c.Call(context.Background(), "https://bp", "op", "t", nil)
	if !errors.Is(err, refreshErr) {
		t.Fatalf("expected refreshErr to propagate; got %v", err)
	}
}

// ---- Search ----

// TestSearchPassesTypedParams — all four params (ConnectorId, Query,
// Group, Limit) land on the typed struct; Group + Limit are pointer-
// typed so the test asserts they're set (not nil).
func TestSearchPassesTypedParams(t *testing.T) {
	body, _ := json.Marshal(SearchResponse{
		Hits:            []SearchHit{{OpID: "vault.kv.read", FusedScore: 0.9}},
		QueryDurationMs: 12.0,
	})
	f := withFake(t, &fakeOperationsClient{
		searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	})
	c := New("vault-1.x")
	if _, err := c.Search(context.Background(), "https://bp", "secret", "kv", 7); err != nil {
		t.Fatalf("Search: %v", err)
	}
	p := f.lastSearchParams
	if p == nil || p.ConnectorId != "vault-1.x" || p.Query != "secret" {
		t.Fatalf("Search should pass typed ConnectorId+Query; got %+v", p)
	}
	if p.Group == nil || *p.Group != "kv" {
		t.Fatalf("Search should pass typed Group=%q; got %+v", "kv", p.Group)
	}
	if p.Limit == nil || *p.Limit != 7 {
		t.Fatalf("Search should pass typed Limit=%d; got %+v", 7, p.Limit)
	}
}

// TestSearchOmitsOptionalParamsWhenEmpty — Group + Limit are nil-
// pointer when the caller didn't supply them, so the generator's
// omitempty form keeps them out of the URL.
func TestSearchOmitsOptionalParamsWhenEmpty(t *testing.T) {
	body, _ := json.Marshal(SearchResponse{Hits: nil, QueryDurationMs: 0})
	f := withFake(t, &fakeOperationsClient{
		searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	})
	c := New("vault-1.x")
	if _, err := c.Search(context.Background(), "https://bp", "secret", "", 0); err != nil {
		t.Fatalf("Search: %v", err)
	}
	p := f.lastSearchParams
	if p == nil {
		t.Fatalf("Search should populate params struct")
	}
	if p.Group != nil {
		t.Fatalf("empty groupKey should leave Group=nil; got %v", *p.Group)
	}
	if p.Limit != nil {
		t.Fatalf("zero limit should leave Limit=nil; got %v", *p.Limit)
	}
}

// TestSearchRefreshesOn401AndRetries — 401 dance for Search.
func TestSearchRefreshesOn401AndRetries(t *testing.T) {
	body, _ := json.Marshal(SearchResponse{})
	f := withFake(t, &fakeOperationsClient{
		searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	})
	c := New("c")
	if _, err := c.Search(context.Background(), "https://bp", "q", "", 0); err != nil {
		t.Fatalf("Search after refresh: %v", err)
	}
	if f.refreshCount != 1 {
		t.Fatalf("expected exactly one Refresh; got %d", f.refreshCount)
	}
}

// TestSearchClassifies5xxAsAPIResponseError — 5xx wraps as
// *APIResponseError, body preserved.
func TestSearchClassifies5xxAsAPIResponseError(t *testing.T) {
	withFake(t, &fakeOperationsClient{
		searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
			{HTTPResponse: makeHTTPResp(500), Body: []byte(`{"detail":"backplane unavailable"}`)},
		},
	})
	c := New("c")
	_, err := c.Search(context.Background(), "https://bp", "q", "", 0)
	var apiErr *APIResponseError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 500 {
		t.Fatalf("expected *APIResponseError{StatusCode:500}; got %+v", err)
	}
}

// TestClassifyNon2xxTrimsBodyWhitespace — Server-Sent envelopes
// often carry trailing newlines; the trim keeps the rendered error
// compact.
func TestClassifyNon2xxTrimsBodyWhitespace(t *testing.T) {
	httpResp := &http.Response{StatusCode: 422}
	apiErr := classifyNon2xx(httpResp, []byte("  {\"detail\":\"bad\"}\n\n  "))
	if apiErr.Body != `{"detail":"bad"}` {
		t.Fatalf("classifyNon2xx should trim whitespace; got %q", apiErr.Body)
	}
}

// TestClassifyNon2xxCapsBodyAtOneMiB — payloads larger than 1 MiB
// are truncated so the rendered error doesn't bloat operator output.
func TestClassifyNon2xxCapsBodyAtOneMiB(t *testing.T) {
	const sizeOverCap = (1 << 20) + 100
	body := bytes.Repeat([]byte("x"), sizeOverCap)
	httpResp := &http.Response{StatusCode: 500}
	apiErr := classifyNon2xx(httpResp, body)
	if len(apiErr.Body) > (1 << 20) {
		t.Fatalf("classifyNon2xx should cap at 1 MiB; got %d bytes", len(apiErr.Body))
	}
}

// ---- Render / PrintGeneric / PrettyJSON ----

func TestRenderStatusMapping(t *testing.T) {
	c := New("c")
	cases := []struct {
		status  string
		wantErr error
	}{
		{"ok", nil},
		{"error", ErrOpError},
		{"denied", ErrOpError},
	}
	for _, tc := range cases {
		cmd := &cobra.Command{}
		cmd.SetOut(&bytes.Buffer{})
		cmd.SetErr(&bytes.Buffer{})
		r := &CallResult{Status: tc.status, OpID: "op"}
		err := c.Render(cmd, "op", r, false, nil)
		if !errors.Is(err, tc.wantErr) {
			t.Fatalf("status %q: err = %v, want %v", tc.status, err, tc.wantErr)
		}
	}
}

func TestRenderInvalidStatusReturnsRenderError(t *testing.T) {
	c := New("c")
	cmd := &cobra.Command{}
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	err := c.Render(cmd, "op", &CallResult{Status: "weird"}, false, nil)
	if err == nil || errors.Is(err, ErrOpError) {
		t.Fatalf("invalid status should yield a non-ErrOpError render error, got %v", err)
	}
}

func TestPrintGenericSuccessRendersResult(t *testing.T) {
	c := New("vault-1.x")
	var buf bytes.Buffer
	c.PrintGeneric(&buf, "vault.kv.read", &CallResult{
		Status: "ok", Result: json.RawMessage(`{"k":"v"}`), DurationMs: 12,
	})
	out := buf.String()
	if !strings.Contains(out, "vault-1.x vault.kv.read") || !strings.Contains(out, `"k": "v"`) {
		t.Fatalf("unexpected generic render:\n%s", out)
	}
}

func TestPrettyJSON(t *testing.T) {
	got, err := PrettyJSON(json.RawMessage(`{"b":1,"a":2}`))
	if err != nil {
		t.Fatalf("PrettyJSON: %v", err)
	}
	if !strings.Contains(got, "\n  ") {
		t.Fatalf("expected 2-space indent, got %q", got)
	}
	if _, err := PrettyJSON(json.RawMessage(`{bad`)); err == nil {
		t.Fatalf("expected error on malformed JSON")
	}
}
