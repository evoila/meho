// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agentprincipal

import (
	"bytes"
	"context"
	"encoding/json"
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

// ---------- shared test substrate ----------

// inMemoryStore satisfies auth.TokenStore without touching the OS
// keyring or the filesystem. Same shape as the helper in
// cli/internal/cmd/approvals/approvals_test.go — duplicated in-package
// because that helper is unexported and cmd/agent-principal cannot
// import cmd/approvals without an import cycle.
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
// pre-loaded with a bearer for the supplied test server. Mirrors the
// substrate every other in-package verb test uses (approvals, agent).
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

// mockHandler is the HandlerFunc alias mockBackplane keys its routing
// table on. Same shape as connector_test.go's helper.
type mockHandler = http.HandlerFunc

// mockBackplane stands up an httptest.Server that routes by
// `<METHOD> <path>`. Empty key is the catch-all so a test can
// validate URL escaping or other path-derived behaviour without
// over-specifying. Mirrors connector_test.go's helper so the
// in-package test surface stays uniform across verb trees.
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

// stubID / stubTenantID are the fixed UUIDs every fixture uses so a
// test failure reads "11111111-..." in the request URL or response
// body — easier to chase than a uuid.New() that rotates per run.
const (
	stubID       = "11111111-1111-1111-1111-111111111111"
	stubTenantID = "22222222-2222-2222-2222-222222222222"
)

func mustUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	id, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("mustUUID(%q): %v", s, err)
	}
	return id
}

func newPrincipalRead(t *testing.T, name string, revoked bool) api.AgentPrincipalRead {
	t.Helper()
	createdAt := time.Date(2026, 5, 28, 12, 0, 0, 0, time.UTC)
	return api.AgentPrincipalRead{
		Id:                 mustUUID(t, stubID),
		TenantId:           mustUUID(t, stubTenantID),
		Name:               name,
		KeycloakClientId:   "agent:" + name,
		KeycloakInternalId: "00000000-internal",
		OwnerSub:           "owner-sub",
		Revoked:            revoked,
		CreatedBySub:       "creator-sub",
		CreatedAt:          createdAt,
		UpdatedAt:          createdAt,
	}
}

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

// runVerbWithMock primes the on-disk file-backed token store + config
// then invokes the supplied verb runner. Returns the captured stderr
// buffer + the RunE error so callers can assert against the rendered
// error envelope. The default `auth.NewTokenStore` + `backplane.Resolve`
// path expects a real config file + token; we short-circuit by setting
// XDG_CONFIG_HOME to a tempdir and writing both. Mirrors T1's
// runListWithMock helper.
func runVerbWithMock(
	t *testing.T,
	srv *httptest.Server,
	runner func(cmd *cobra.Command) error,
) (*bytes.Buffer, *bytes.Buffer, error) {
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
	cmd, stdout, stderr := newCapturingCmd(t)
	return stdout, stderr, runner(cmd)
}

// assertRenderedErrorCode parses the JSON error envelope from stderr
// and asserts the StructuredError code + a substring of the detail
// field. Also asserts exitErr is non-nil so the cobra dispatcher
// would have exited non-zero (operator's shell-script `$?` sees a
// matching exit code). Mirrors T1's helper of the same name.
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

// readJSONBody decodes the request body into a fresh value of T's
// type. Used by handlers that assert on the wire shape they received
// (typically against a generated `api.*RequestBody` type, not a
// consumer-side duplicate).
func readJSONBody[T any](t *testing.T, r *http.Request) T {
	t.Helper()
	var v T
	if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	return v
}

// ---------- helper-function tests ----------

// TestDecodeDetailStringPullsDetailField pins the small renderer
// helper: FastAPI HTTPException bodies arrive as `{"detail": "..."}`
// and we surface just the string so the error envelope stays clean.
func TestDecodeDetailStringPullsDetailField(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{`{"detail":"agent_principal_not_found"}`, "agent_principal_not_found"},
		{`not-json`, "not-json"},                   // fallback: raw body
		{`{"unrelated":"x"}`, `{"unrelated":"x"}`}, // no detail field → raw body
		{``, ``},                           // empty input
		{`{"detail":""}`, `{"detail":""}`}, // empty detail value → raw body
	}
	for _, tc := range cases {
		if got := decodeDetailString(tc.in); got != tc.want {
			t.Errorf("decodeDetailString(%q) = %q; want %q", tc.in, got, tc.want)
		}
	}
}

// ---------- list verb ----------

// TestListOmitsIncludeRevokedWhenFalse confirms the default flag
// state sends no `include_revoked` query param. The backplane's own
// default (excluding revoked rows) then applies; sending an explicit
// `include_revoked=false` would be load-bearing-equivalent but adds
// wire noise the typed-client transition shouldn't introduce.
func TestListOmitsIncludeRevokedWhenFalse(t *testing.T) {
	var seenQuery string
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/agent-principals": func(w http.ResponseWriter, r *http.Request) {
			seenQuery = r.URL.RawQuery
			writeJSON(t, w, http.StatusOK, api.AgentPrincipalListResponse{
				Principals: []api.AgentPrincipalRead{newPrincipalRead(t, "alice-bot", false)},
			})
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	resp, err := retryOn401(context.Background(), client,
		func(ctx context.Context) (*api.ListAgentPrincipalsApiV1AgentPrincipalsGetResponse, error) {
			return client.ListAgentPrincipalsApiV1AgentPrincipalsGetWithResponse(
				ctx, listQueryParams(listOptions{IncludeRevoked: false}),
			)
		},
		func(r *api.ListAgentPrincipalsApiV1AgentPrincipalsGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		t.Fatalf("ListAgentPrincipals*WithResponse: %v", err)
	}
	if resp.StatusCode() != http.StatusOK {
		t.Fatalf("status: got %d want 200", resp.StatusCode())
	}
	if resp.JSON200 == nil || len(resp.JSON200.Principals) != 1 {
		t.Fatalf("expected one decoded principal; got %+v", resp.JSON200)
	}
	if seenQuery != "" {
		t.Errorf("unset --include-revoked should not appear on wire; got query=%q", seenQuery)
	}
	if resp.JSON200.Principals[0].Name != "alice-bot" {
		t.Errorf("decoded Name: got %q want %q", resp.JSON200.Principals[0].Name, "alice-bot")
	}
}

// TestListPassesIncludeRevokedWhenTrue confirms --include-revoked
// surfaces on the wire as `include_revoked=true` via the generated
// query-param shape (no string-concat).
func TestListPassesIncludeRevokedWhenTrue(t *testing.T) {
	var seenIncludeRevoked string
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/agent-principals": func(w http.ResponseWriter, r *http.Request) {
			seenIncludeRevoked = r.URL.Query().Get("include_revoked")
			writeJSON(t, w, http.StatusOK, api.AgentPrincipalListResponse{
				Principals: []api.AgentPrincipalRead{newPrincipalRead(t, "alice-bot", true)},
			})
		},
	})
	defer srv.Close()
	client := newTestClient(t, srv)

	_, err := retryOn401(context.Background(), client,
		func(ctx context.Context) (*api.ListAgentPrincipalsApiV1AgentPrincipalsGetResponse, error) {
			return client.ListAgentPrincipalsApiV1AgentPrincipalsGetWithResponse(
				ctx, listQueryParams(listOptions{IncludeRevoked: true}),
			)
		},
		func(r *api.ListAgentPrincipalsApiV1AgentPrincipalsGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		t.Fatalf("ListAgentPrincipals*WithResponse: %v", err)
	}
	if seenIncludeRevoked != "true" {
		t.Errorf("--include-revoked=true should send include_revoked=true; got %q", seenIncludeRevoked)
	}
}

// TestListMaps401ToAuthExpired covers the 401 path end to end: the
// mocked backplane always rejects the stored bearer, refresh fails
// (no refresh_token in the in-memory store), and the renderer maps
// the sentinel onto auth_expired with the `meho login` hint.
func TestListMaps401ToAuthExpired(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusUnauthorized)
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeAuthExpired, "no refresh_token")
}

// TestListMaps403ToInsufficientRole confirms 403 → insufficient_role
// survives the migration. The pre-G0.12 path read the same status
// through the local sentinel; this test drives the typed-client
// transport end to end through the verb's RunE.
func TestListMaps403ToInsufficientRole(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"detail":"operator role required"}`))
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeInsufficientRole, "operator role required")
}

// TestListMaps503ToKeycloakAdminNotConfigured pins the explicit 503
// branch on `renderHTTPStatus`. The backplane raises 503 when the
// Keycloak admin client knobs are unset; we render a friendly hint
// rather than the raw body so the operator knows who to chase.
func TestListMaps503ToKeycloakAdminNotConfigured(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"detail":"keycloak_admin_not_configured"}`))
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "keycloak_admin_not_configured")
}

// TestListRendersTable pins the human-table happy path: the renderer
// emits the header + one row per decoded principal + "no principals"
// when empty.
func TestListRendersTable(t *testing.T) {
	var buf bytes.Buffer
	printListTable(&buf, &api.AgentPrincipalListResponse{
		Principals: []api.AgentPrincipalRead{
			newPrincipalRead(t, "alice-bot", false),
			newPrincipalRead(t, "bob-bot", true),
		},
	})
	out := buf.String()
	for _, want := range []string{
		"NAME", "REVOKED", "OWNER", "KEYCLOAK CLIENT ID",
		"alice-bot", "agent:alice-bot",
		"bob-bot", "agent:bob-bot",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printListTable missing %q in:\n%s", want, out)
		}
	}
}

// TestListRendersEmptyTable confirms an empty principal list renders
// the empty-tenant line — the operator sees the explicit "nothing
// here" rather than a header-only frame.
func TestListRendersEmptyTable(t *testing.T) {
	var buf bytes.Buffer
	printListTable(&buf, &api.AgentPrincipalListResponse{Principals: nil})
	if !strings.Contains(buf.String(), "no agent principals registered in this tenant") {
		t.Errorf("empty list: missing empty-line; got:\n%s", buf.String())
	}
}

// ---------- register verb ----------

// TestRegisterSendsTypedBody confirms the verb POSTs the generated
// AgentPrincipalCreate body (decoded by the mock against the same
// generated type) — no consumer-side struct duplicate sits between
// the operator's --name / --owner-sub flags and the backend's
// pydantic model.
func TestRegisterSendsTypedBody(t *testing.T) {
	var seen api.AgentPrincipalCreate
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, r *http.Request) {
			seen = readJSONBody[api.AgentPrincipalCreate](t, r)
			writeJSON(t, w, http.StatusCreated, newPrincipalRead(t, seen.Name, false))
		},
	})
	defer srv.Close()

	_, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "alice-bot", OwnerSub: "alice-sub",
			JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	if err != nil {
		t.Fatalf("runRegister: %v", err)
	}
	if seen.Name != "alice-bot" {
		t.Errorf("body Name: got %q want %q", seen.Name, "alice-bot")
	}
	if seen.OwnerSub == nil || *seen.OwnerSub != "alice-sub" {
		t.Errorf("body OwnerSub: got %+v want pointer to %q", seen.OwnerSub, "alice-sub")
	}
}

// TestRegisterOmitsOwnerSubWhenUnset confirms an unset --owner-sub
// marshals to a body with `owner_sub` as a null pointer (the
// generated AgentPrincipalCreate field is *string). The backend
// treats null/missing as "default to the caller's sub"; sending an
// explicit empty string would land in the DB as a literal empty
// owner_sub.
func TestRegisterOmitsOwnerSubWhenUnset(t *testing.T) {
	var rawBody bytes.Buffer
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, r *http.Request) {
			if _, err := rawBody.ReadFrom(r.Body); err != nil {
				t.Fatalf("read body: %v", err)
			}
			writeJSON(t, w, http.StatusCreated, newPrincipalRead(t, "alice-bot", false))
		},
	})
	defer srv.Close()

	_, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "alice-bot", JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	if err != nil {
		t.Fatalf("runRegister: %v", err)
	}
	// AgentPrincipalCreate.OwnerSub has no `omitempty` tag (the
	// backend pydantic model is `owner_sub: str | None = None` →
	// generated as `OwnerSub *string \`json:"owner_sub"\``), so an
	// unset --owner-sub serialises as `"owner_sub":null`, not as a
	// missing key. The load-bearing property is that it's not an
	// explicit empty string — that distinction is what tells the
	// backend "use the caller's sub" vs "literal empty".
	body := rawBody.String()
	if !strings.Contains(body, `"owner_sub":null`) {
		t.Errorf("unset --owner-sub should marshal as null, got body=%s", body)
	}
	if strings.Contains(body, `"owner_sub":""`) {
		t.Errorf("unset --owner-sub must not marshal as empty string, got body=%s", body)
	}
}

// TestRegisterMaps409ToAlreadyExists pins the 409 path: the backend
// returns `agent_principal_already_exists` when a same-name principal
// already exists in the tenant; the renderer surfaces the detail
// under unexpected_response.
func TestRegisterMaps409ToAlreadyExists(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"detail":"agent_principal_already_exists"}`))
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "alice-bot", JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "agent_principal_already_exists")
}

// TestRegisterMaps403ToInsufficientRole covers the 403 path on the
// write verb (the operator lacks tenant_admin).
func TestRegisterMaps403ToInsufficientRole(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"detail":"tenant_admin role required"}`))
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "alice-bot", JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeInsufficientRole, "tenant_admin role required")
}

// TestRegisterRejectsEmptyName pins the CLI-side guard: an empty
// --name reaches the renderer before any HTTP traffic so the
// operator gets immediate feedback (the backend's 422 would land
// on the same code, but the round-trip is wasteful).
func TestRegisterRejectsEmptyName(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		// No routes — the verb should reject before any HTTP traffic.
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "non-empty <name>")
}

// ---------- revoke verb ----------

// TestRevokeSendsTypedPathParam pins the URL shape: the generated
// client interpolates the name into `/api/v1/agent-principals/{name}/revoke`
// with proper escaping. The mock asserts on the path it received.
func TestRevokeSendsTypedPathParam(t *testing.T) {
	var seenPath string
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/agent-principals/alice-bot/revoke": func(w http.ResponseWriter, r *http.Request) {
			seenPath = r.URL.Path
			writeJSON(t, w, http.StatusOK, newPrincipalRead(t, "alice-bot", true))
		},
	})
	defer srv.Close()

	_, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRevoke(cmd, revokeOptions{
			Name: "alice-bot", JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	if err != nil {
		t.Fatalf("runRevoke: %v", err)
	}
	if seenPath != "/api/v1/agent-principals/alice-bot/revoke" {
		t.Errorf("path: got %q want %q", seenPath, "/api/v1/agent-principals/alice-bot/revoke")
	}
}

// TestRevokeMaps404ToNotFound pins the 404 path: the backend returns
// `agent_principal_not_found` for both genuine absence and cross-
// tenant probes (the conflation prevents enumerating other tenants
// via status-code differential), and the renderer surfaces the
// backend's detail under unexpected_response.
func TestRevokeMaps404ToNotFound(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/agent-principals/ghost/revoke": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"agent_principal_not_found"}`))
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRevoke(cmd, revokeOptions{
			Name: "ghost", JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "agent_principal_not_found")
}

// TestRevokeMaps403ToInsufficientRole — the write verb's 403 lands
// on insufficient_role, same as register.
func TestRevokeMaps403ToInsufficientRole(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"DELETE /api/v1/agent-principals/alice-bot/revoke": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"detail":"tenant_admin role required"}`))
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRevoke(cmd, revokeOptions{
			Name: "alice-bot", JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeInsufficientRole, "tenant_admin role required")
}

// TestRevokeRejectsEmptyName mirrors register's empty-name guard:
// the verb refuses to hit the wire on a blank argument.
func TestRevokeRejectsEmptyName(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRevoke(cmd, revokeOptions{JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, output.ErrCodeUnexpected, "non-empty <name>")
}

// ---------- printEntrySummary renderer ----------

// TestPrintEntrySummaryRendersFields confirms the renderer hits the
// AgentPrincipalRead fields the operator-facing summary advertises.
// The format is byte-identical to the pre-migration renderer except
// CreatedAt is now formatted from the typed time.Time (was a string
// in the consumer-side Entry).
func TestPrintEntrySummaryRendersFields(t *testing.T) {
	var buf bytes.Buffer
	entry := newPrincipalRead(t, "alice-bot", true)
	printEntrySummary(&buf, &entry)
	out := buf.String()
	for _, want := range []string{
		"id:                  " + stubID,
		"keycloak_client_id:  agent:alice-bot",
		"owner_sub:           owner-sub",
		"revoked:             true",
		"created_at:          2026-05-28T12:00:00Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printEntrySummary missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintEntrySummaryNilSafe — a nil entry is a no-op rather than
// a panic. Defensive: the caller already checks the response shape,
// but a panic here would surface as a confusing operator crash.
func TestPrintEntrySummaryNilSafe(t *testing.T) {
	var buf bytes.Buffer
	printEntrySummary(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("nil entry should write nothing; got %q", buf.String())
	}
}
