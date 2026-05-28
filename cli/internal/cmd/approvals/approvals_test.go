// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// ---------- helpers ----------

// inMemoryStore satisfies auth.TokenStore without touching the OS
// keyring or the filesystem. Mirrors the shape of the helper in
// cli/internal/api/client_test.go (which is unexported, so cannot
// be imported across packages).
type inMemoryStore struct {
	entries map[string]auth.StoredToken
}

func (s *inMemoryStore) key(service, user string) string {
	return service + "\x00" + user
}

func (s *inMemoryStore) Save(service, user string, tok auth.StoredToken) error {
	if s.entries == nil {
		s.entries = map[string]auth.StoredToken{}
	}
	s.entries[s.key(service, user)] = tok
	return nil
}

func (s *inMemoryStore) Load(service, user string) (auth.StoredToken, error) {
	tok, ok := s.entries[s.key(service, user)]
	if !ok {
		return auth.StoredToken{}, auth.ErrTokenNotFound
	}
	return tok, nil
}

func (s *inMemoryStore) Delete(service, user string) error {
	delete(s.entries, s.key(service, user))
	return nil
}

func (inMemoryStore) Describe() string { return "in-memory test store" }

// newTestClient builds an AuthedClient backed by an in-memory store
// pre-loaded with a bearer for the supplied test server. Bypasses
// the production keychain / config-file path. Mirrors the test
// substrate used by client_test.go.
func newTestClient(t *testing.T, srv *httptest.Server) *api.AuthedClient {
	t.Helper()
	store := &inMemoryStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "test-bearer",
		Expiry:       time.Now().Add(time.Hour),
	})
	client, err := api.NewAuthedClient(context.Background(), srv.URL,
		api.AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	return client
}

// stubID is a fixed UUID used in every approval-API fixture. Reading
// the literal "11111111-..." in a request URL or response body in a
// test failure makes the assertion easier to chase than a random
// uuid.New() that changes per test run.
const stubID = "11111111-1111-1111-1111-111111111111"

func newApprovalView(t *testing.T, status, reason string) api.ApprovalRequestView {
	t.Helper()
	id, err := uuid.Parse(stubID)
	if err != nil {
		t.Fatalf("uuid.Parse(stubID): %v", err)
	}
	tenant, err := uuid.Parse("22222222-2222-2222-2222-222222222222")
	if err != nil {
		t.Fatalf("uuid.Parse(tenant): %v", err)
	}
	reviewed := "alice@example.com"
	var reviewedBy *string
	if reason != "" {
		reviewedBy = &reviewed
	}
	return api.ApprovalRequestView{
		Id:           id,
		TenantId:     tenant,
		Status:       api.ApprovalRequestStatus(status),
		ConnectorId:  "vmware-rest-9.0",
		OpId:         "GET:/api/vcenter/cluster",
		ParamsHash:   "deadbeef",
		PrincipalSub: "user-abc",
		CreatedAt:    "2026-05-28T12:00:00Z",
		ReviewedBy:   reviewedBy,
	}
}

// mockHandler is the HandlerFunc alias mockBackplane keys its
// routing table on. Same shape as connector_test.go's helper.
type mockHandler = http.HandlerFunc

// mockBackplane stands up an httptest.Server that routes by
// `<METHOD> <path>`. The empty key acts as a catch-all so tests can
// validate URL escaping or other path-derived behaviour without
// over-specifying. Mirrors connector_test.go's helper to keep the
// in-package test surface uniform across verb trees.
func mockBackplane(t *testing.T, routes map[string]mockHandler) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		if h, ok := routes[key]; ok {
			h(w, r)
			return
		}
		if h, ok := routes[""]; ok {
			h(w, r)
			return
		}
		t.Errorf("mockBackplane: unhandled route %s", key)
		w.WriteHeader(http.StatusNotFound)
	}))
}

func writeJSON(t *testing.T, w http.ResponseWriter, status int, body any) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Errorf("writeJSON marshal: %v", err)
		w.WriteHeader(http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write(raw); err != nil {
		t.Errorf("writeJSON write: %v", err)
	}
}

// readJSONBody decodes the request body into a fresh value of v's
// type. Used by handlers that want to assert on the wire shape they
// received (typically against a generated `api.*RequestBody` type,
// not a consumer-side duplicate).
func readJSONBody[T any](t *testing.T, r *http.Request) T {
	t.Helper()
	var v T
	if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	return v
}

// ---------- helper-function tests ----------

// TestTrimmedBodyDropsTrailingWhitespace pins the small renderer
// helper. Backplane responses often arrive with a trailing newline;
// dropping it keeps the error envelope's `HTTP 500: foo` shape
// stable in the operator-visible output.
func TestTrimmedBodyDropsTrailingWhitespace(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"plain", "plain"},
		{"trail\n", "trail"},
		{"trail \r\n", "trail"},
		{"trail\t  ", "trail"},
		{"", "(empty body)"},
		{"   \n", "(empty body)"},
		{"   leading kept", "   leading kept"},
	}
	for _, tc := range cases {
		if got := trimmedBody([]byte(tc.in)); got != tc.want {
			t.Errorf("trimmedBody(%q) = %q; want %q", tc.in, got, tc.want)
		}
	}
}

// TestParseRequestIDRejectsGarbage pins the UUID-at-the-edge
// behaviour the show / approve / reject verbs share. Garbage input
// surfaces a clean output.Unexpected (no panic, no `fmt.Errorf`
// blob halfway through a request).
func TestParseRequestIDRejectsGarbage(t *testing.T) {
	cases := []string{
		"not-a-uuid",
		"",
		"11111111-1111-1111-1111-1111111111", // one char short
	}
	for _, in := range cases {
		_, err := parseRequestID(in)
		if err == nil {
			t.Errorf("parseRequestID(%q): expected rejection, got nil err", in)
			continue
		}
		if !strings.Contains(err.Error(), "approval-id is not a valid UUID") {
			t.Errorf("parseRequestID(%q): unexpected error %q", in, err)
		}
	}
}

// TestParseRequestIDAcceptsValid pins the happy path so a regression
// to strict v4-only parsing (uuid.MustParse rejects v1/v3/v5) would
// surface here.
func TestParseRequestIDAcceptsValid(t *testing.T) {
	got, err := parseRequestID(stubID)
	if err != nil {
		t.Fatalf("parseRequestID(%q): %v", stubID, err)
	}
	if got.String() != stubID {
		t.Errorf("round-trip: got %q want %q", got.String(), stubID)
	}
}

// ---------- list verb ----------

// TestListPassesTypedStatusParam pins the load-bearing G0.12-T1
// migration claim: list passes the typed `status` param through the
// generated client, no string-concat. The mock asserts on the
// decoded query param shape.
func TestListPassesTypedStatusParam(t *testing.T) {
	var seenStatus string
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, r *http.Request) {
			seenStatus = r.URL.Query().Get("status")
			writeJSON(t, w, http.StatusOK, []api.ApprovalRequestView{
				newApprovalView(t, "pending", ""),
			})
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	items, err := fetchList(context.Background(), client, listOpts{StatusFilter: "pending"})
	if err != nil {
		t.Fatalf("fetchList: %v", err)
	}
	if len(items) != 1 {
		t.Fatalf("expected 1 item, got %d", len(items))
	}
	if seenStatus != "pending" {
		t.Errorf("expected status=pending on the wire, got %q", seenStatus)
	}
	if items[0].Id.String() != stubID {
		t.Errorf("decoded ApprovalRequestView.Id: got %q want %q", items[0].Id.String(), stubID)
	}
}

// TestListOmitsStatusWhenUnset confirms an empty StatusFilter sends
// no `status` query param (the backend then applies its own default
// of `pending`).
func TestListOmitsStatusWhenUnset(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, r *http.Request) {
			if got := r.URL.Query().Get("status"); got != "" {
				t.Errorf("unset StatusFilter should omit query param; got status=%q", got)
			}
			// Confirm we don't accidentally send limit/offset either —
			// the generated client's params struct doesn't expose them,
			// so they should never reach the wire even though the CLI
			// flags accept them.
			if got := r.URL.Query().Get("limit"); got != "" {
				t.Errorf("limit should not reach wire (generator omits it); got limit=%q", got)
			}
			if got := r.URL.Query().Get("offset"); got != "" {
				t.Errorf("offset should not reach wire (generator omits it); got offset=%q", got)
			}
			writeJSON(t, w, http.StatusOK, []api.ApprovalRequestView{})
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	// Even when --limit and --offset are set, they're a no-op at the
	// typed-client layer (the backend doesn't accept them and the
	// generator agrees).
	if _, err := fetchList(context.Background(), client, listOpts{Limit: 10, Offset: 5}); err != nil {
		t.Fatalf("fetchList: %v", err)
	}
}

// TestListMaps403ToInsufficientRole confirms the 403 → insufficient_role
// mapping survives the migration off the pre-G0.12 local httpError
// sentinel. Same shape as the pre-migration test would have looked,
// just driving the typed client through the verb's RunE.
func TestListMaps403ToInsufficientRole(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte("operator role required"))
		},
	})
	defer srv.Close()

	stderr, exitErr := runListWithMock(t, srv, listOpts{StatusFilter: "pending", JSONOut: true})
	assertRenderedErrorCode(t, stderr, exitErr, output.ErrCodeInsufficientRole, "operator role required")
}

// TestListMaps422ToUnexpected pins 422 (the FastAPI validation error
// route) → unexpected_response. Same gate the pre-migration
// renderHTTPError used.
func TestListMaps422ToUnexpected(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"detail":"validation"}`))
		},
	})
	defer srv.Close()

	stderr, exitErr := runListWithMock(t, srv, listOpts{StatusFilter: "pending", JSONOut: true})
	assertRenderedErrorCode(t, stderr, exitErr, output.ErrCodeUnexpected, "validation")
}

// TestListMaps404ToApprovalRequestNotFound — the list route never
// returns 404 in practice (it's a tenant-scoped list), but the
// renderer's switch covers it for the show / decide siblings and
// the gate must survive uniformly across verbs. Pinned here so a
// rewrite of `renderHTTPStatus` can't lose the case silently.
func TestListMaps404ToApprovalRequestNotFound(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte("missing"))
		},
	})
	defer srv.Close()

	stderr, exitErr := runListWithMock(t, srv, listOpts{StatusFilter: "pending", JSONOut: true})
	assertRenderedErrorCode(t, stderr, exitErr, output.ErrCodeUnexpected, "approval_request_not_found")
}

// ---------- show verb ----------

// TestShowPathParamCarriesUUID pins the typed UUID path-param
// behaviour: the operator's string `<id>` round-trips through the
// generated client into the URL path as the canonical UUID form,
// not concatenated, not escaped twice.
func TestShowPathParamCarriesUUID(t *testing.T) {
	var seenPath string
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, r *http.Request) {
			seenPath = r.URL.Path
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "pending", ""))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	id, err := parseRequestID(stubID)
	if err != nil {
		t.Fatalf("parseRequestID: %v", err)
	}
	detail, err := fetchDetail(context.Background(), client, id)
	if err != nil {
		t.Fatalf("fetchDetail: %v", err)
	}
	if seenPath != "/api/v1/approvals/"+stubID {
		t.Errorf("path: got %q want %q", seenPath, "/api/v1/approvals/"+stubID)
	}
	if detail == nil || detail.Id.String() != stubID {
		t.Fatalf("decoded ApprovalRequestView.Id: got %+v want id=%q", detail, stubID)
	}
}

// TestShowRejectsGarbageIDCleanly drives the verb's RunE with a
// non-UUID arg and asserts the renderer fires output.Unexpected
// with the operator-visible hint — no panic, no fmt-into-the-URL.
func TestShowRejectsGarbageID(t *testing.T) {
	cmd, _, stderr := newCapturingCmd(t)
	err := runShow(cmd, "not-a-uuid", true, "https://meho.example")
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "approval-id is not a valid UUID")
}

// ---------- approve / reject verbs ----------

// TestApproveDispatchesDecideAndShow pins the two-call shape:
// /decide POST with decision=approved + the supplied reason, then
// GET on the same id to re-render the view. Mirrors the same shape
// reject uses; documented here once on the approve verb.
func TestApproveDispatchesDecideAndShow(t *testing.T) {
	type call struct {
		method string
		path   string
		body   *api.DecideRequestBody
	}
	var calls []call

	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, r *http.Request) {
			b := readJSONBody[api.DecideRequestBody](t, r)
			calls = append(calls, call{r.Method, r.URL.Path, &b})
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{
				ApprovalRequestId: mustUUID(t, stubID),
				Decision:          "approved",
				Reason:            "T-test",
			})
		},
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, r *http.Request) {
			calls = append(calls, call{r.Method, r.URL.Path, nil})
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "approved", "T-test"))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	id := mustUUID(t, stubID)
	detail, err := postDecision(context.Background(), client, id, "approve", "T-test")
	if err != nil {
		t.Fatalf("postDecision: %v", err)
	}
	if len(calls) != 2 {
		t.Fatalf("expected 2 calls (decide + show); got %d: %+v", len(calls), calls)
	}
	if calls[0].method != "POST" || calls[0].path != "/api/v1/approvals/"+stubID+"/decide" {
		t.Errorf("first call: got %s %s", calls[0].method, calls[0].path)
	}
	if calls[0].body == nil || calls[0].body.Decision != "approved" {
		t.Errorf("decide body: got %+v want decision=approved", calls[0].body)
	}
	if calls[0].body.Reason == nil || *calls[0].body.Reason != "T-test" {
		t.Errorf("decide body reason: got %+v want pointer to %q", calls[0].body.Reason, "T-test")
	}
	if calls[1].method != "GET" || calls[1].path != "/api/v1/approvals/"+stubID {
		t.Errorf("second call: got %s %s", calls[1].method, calls[1].path)
	}
	if detail == nil || string(detail.Status) != "approved" {
		t.Fatalf("decoded view: got %+v want status=approved", detail)
	}
}

// TestRejectDispatchesDecideWithRejectedDecision confirms the verb
// shape mirrors approve but with `decision=rejected`. Asserts the
// past-tense translation (verb "reject" → decision "rejected").
func TestRejectDispatchesDecideWithRejectedDecision(t *testing.T) {
	var seenDecision string
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, r *http.Request) {
			b := readJSONBody[api.DecideRequestBody](t, r)
			seenDecision = b.Decision
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{
				ApprovalRequestId: mustUUID(t, stubID),
				Decision:          "rejected",
				Reason:            "",
			})
		},
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "rejected", ""))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	id := mustUUID(t, stubID)
	if _, err := postDecision(context.Background(), client, id, "reject", ""); err != nil {
		t.Fatalf("postDecision: %v", err)
	}
	if seenDecision != "rejected" {
		t.Errorf("verb=reject should send decision=rejected; got %q", seenDecision)
	}
}

// TestApproveOmitsReasonWhenEmpty pins the wire-shape semantics: an
// unset --reason marshals to a body with no `reason` key (the
// generated DecideRequestBody.Reason is *string with `omitempty`),
// not an explicit `"reason": ""` that would land in the decision
// audit row as a blank string.
func TestApproveOmitsReasonWhenEmpty(t *testing.T) {
	var rawBody []byte
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, r *http.Request) {
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Fatalf("read body: %v", err)
			}
			rawBody = body
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{
				ApprovalRequestId: mustUUID(t, stubID),
				Decision:          "approved",
			})
		},
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "approved", ""))
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	id := mustUUID(t, stubID)
	if _, err := postDecision(context.Background(), client, id, "approve", ""); err != nil {
		t.Fatalf("postDecision: %v", err)
	}
	if strings.Contains(string(rawBody), `"reason"`) {
		t.Errorf("empty reason should not marshal a `reason` key; got body=%s", rawBody)
	}
}

// TestApproveSurfacesDecide409 confirms a 409 on /decide (e.g.
// "approval_request_already_approved") propagates as
// `unexpected_response` with the backplane's body — the operator
// sees the real reason.
func TestApproveSurfacesDecide409(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte("approval_request_already_approved"))
		},
		// The GET shouldn't fire — postDecision short-circuits on
		// the non-2xx decide response. mockBackplane's t.Errorf
		// catch-all would flag any stray call.
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	id := mustUUID(t, stubID)
	_, err := postDecision(context.Background(), client, id, "approve", "")
	if err == nil {
		t.Fatalf("expected error from 409; got nil")
	}
	var he *httpResponseError
	if !errors.As(err, &he) || he.statusCode != http.StatusConflict {
		t.Fatalf("expected *httpResponseError 409; got %T %v", err, err)
	}
}

// ---------- 401 refresh retry ----------

// TestFetchList401RefreshFailureSurfacesAuthExpired pins the
// 401-retry shape end-to-end through the typed client + AuthedClient
// (RefreshDiscovery never configured, no refresh_token persisted →
// IsNoRefreshToken sentinel surfaces). Mirrors AuthedClient.GetHealth's
// 401 path the typed-client migration has to preserve. The renderer
// maps the sentinel onto auth_expired.
func TestFetchList401RefreshFailureSurfacesAuthExpired(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/approvals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusUnauthorized)
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv) // bearer has no refresh_token

	_, err := fetchList(context.Background(), client, listOpts{StatusFilter: "pending"})
	if err == nil {
		t.Fatal("expected error from 401 + no-refresh-token; got nil")
	}
	if !api.IsNoRefreshToken(err) {
		t.Fatalf("expected IsNoRefreshToken sentinel; got %T %v", err, err)
	}

	// Confirm the verb's renderer maps the sentinel onto
	// `auth_expired` with the `meho login` hint.
	cmd, _, stderr := newCapturingCmd(t)
	rerr := routeRequestError(cmd, srv.URL, err, true)
	assertRenderedErrorCode(t, stderr, rerr, output.ErrCodeAuthExpired, "no refresh_token")
}

// ---------- printDetail / printListTable renderers ----------

// TestPrintDetailRendersOptionalFields confirms the renderer omits
// optional fields when nil (target_id, run_id, reviewed_by,
// decided_at, expires_at, proposed_effect) and includes them when
// present. Asserts against the generated api.ApprovalRequestView
// directly — there is no consumer-side duplicate by design.
func TestPrintDetailRendersOptionalFields(t *testing.T) {
	d := newApprovalView(t, "approved", "T-test")
	target := mustUUID(t, "33333333-3333-3333-3333-333333333333")
	decidedAt := "2026-05-28T12:05:00Z"
	d.TargetId = &target
	d.DecidedAt = &decidedAt
	d.ProposedEffect = map[string]interface{}{"action": "create"}

	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	out := stdout.String()
	for _, want := range []string{
		"ID:           " + stubID,
		"Status:       approved",
		"Connector:    vmware-rest-9.0",
		"Operation:    GET:/api/vcenter/cluster",
		"Target:       33333333-3333-3333-3333-333333333333",
		"Principal:    user-abc",
		"Params hash:  deadbeef",
		"Created:      2026-05-28T12:00:00Z",
		"Reviewed by:  alice@example.com",
		"Decided at:   2026-05-28T12:05:00Z",
		"Effect:",
		"\"action\": \"create\"",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printDetail render missing %q in:\n%s", want, out)
		}
	}
	for _, banned := range []string{"Acting as:", "Agent run:", "Expires:"} {
		if strings.Contains(out, banned) {
			t.Errorf("printDetail render included %q for nil field in:\n%s", banned, out)
		}
	}
}

// TestPrintListTableEmpty — zero items renders the empty-tenant line.
func TestPrintListTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printListTable(&buf, nil)
	if !strings.Contains(buf.String(), "no approval requests in this tenant") {
		t.Errorf("empty list: missing empty-line; got:\n%s", buf.String())
	}
}

// TestPrintListTableHappyPath — happy-path render emits the header,
// the row's coordinates, and the "Showing N." footer.
func TestPrintListTableHappyPath(t *testing.T) {
	items := []api.ApprovalRequestView{newApprovalView(t, "pending", "")}
	var buf bytes.Buffer
	printListTable(&buf, items)
	out := buf.String()
	for _, want := range []string{
		"ID", "STATUS", "CONNECTOR", "OP", "PRINCIPAL", "CREATED",
		stubID, "pending", "vmware-rest-9.0", "user-abc",
		"Showing 1.",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printListTable render missing %q in:\n%s", want, out)
		}
	}
}

// TestTruncatePassthroughAndCut covers the rune-aware truncate
// helper. Same shape as the connector sibling's test — duplicated
// in-package because cmd/approvals can't import cmd/connector
// without an import cycle.
func TestTruncatePassthroughAndCut(t *testing.T) {
	cases := []struct {
		in     string
		maxLen int
		want   string
	}{
		{"abc", 5, "abc"},
		{"abcdef", 4, "abc…"},
		{"café world", 5, "café…"},
		{"x", 0, ""},
	}
	for _, tc := range cases {
		if got := truncate(tc.in, tc.maxLen); got != tc.want {
			t.Errorf("truncate(%q, %d) = %q; want %q", tc.in, tc.maxLen, got, tc.want)
		}
	}
}

// ---------- small helpers ----------

// mustUUID parses s as a UUID, failing the test on error. Strict
// helper used by handlers that need a canonical UUID for fixture
// data.
func mustUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	id, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("mustUUID(%q): %v", s, err)
	}
	return id
}

// newCapturingCmd builds a cobra.Command with in-memory stdout and
// stderr buffers wired up. Returns the command, the stdout buffer
// (for renderers that write to cmd.OutOrStdout()), and the stderr
// buffer (for error-envelope assertions that route through
// cmd.ErrOrStderr()).
func newCapturingCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{Use: "x"}
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	cmd.SetOut(stdout)
	cmd.SetErr(stderr)
	cmd.SetContext(context.Background())
	return cmd, stdout, stderr
}

// runListWithMock runs runList against a mocked backplane, with a
// pre-populated in-memory token store so newAuthedClient succeeds.
// Returns the captured stderr buffer + the RunE error. The
// runList path resolves the backplane URL via `backplane.Resolve`
// which reads $XDG_CONFIG_HOME; we point it at a tempdir holding a
// config.json that names the test server.
func runListWithMock(t *testing.T, srv *httptest.Server, opts listOpts) (*bytes.Buffer, error) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	cfgDir := filepath.Join(dir, "meho")
	if err := os.MkdirAll(cfgDir, 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	cfgBlob, _ := json.Marshal(map[string]string{"backplane_url": srv.URL})
	if err := os.WriteFile(filepath.Join(cfgDir, "config.json"), cfgBlob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	// Prime the on-disk file-backed token store (matches
	// auth.fileTokenStore shape).
	service, user := auth.KeyForBackplane(srv.URL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: srv.URL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}

	cmd := &cobra.Command{Use: "list"}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetContext(context.Background())

	// Drive runList with the override pointing at the test server.
	opts.BackplaneOverride = srv.URL
	err = runList(cmd, opts)
	return &stderr, err
}

// assertRenderedErrorCode parses the JSON error envelope from stderr
// and asserts the StructuredError code + a substring in the detail.
// Also confirms `exitErr` is non-nil (RunE returned an error so the
// cobra dispatcher exits non-zero) and carries the expected exit
// code so the operator's shell-script `$?` sees the right value.
func assertRenderedErrorCode(t *testing.T, stderr *bytes.Buffer, exitErr error, wantCode, wantDetailSubstr string) {
	t.Helper()
	if exitErr == nil {
		t.Fatalf("expected RunE to return non-nil error; got nil. stderr=%q", stderr.String())
	}
	var envelope map[string]interface{}
	if err := json.NewDecoder(stderr).Decode(&envelope); err != nil {
		t.Fatalf("decode error envelope %q: %v", stderr.String(), err)
	}
	gotCode, _ := envelope["error"].(string)
	if gotCode != wantCode {
		t.Errorf("error: got %q want %q (envelope=%+v)", gotCode, wantCode, envelope)
	}
	detail, _ := envelope["detail"].(string)
	if wantDetailSubstr != "" && !strings.Contains(detail, wantDetailSubstr) {
		t.Errorf("detail %q missing substring %q (envelope=%+v)", detail, wantDetailSubstr, envelope)
	}
}
