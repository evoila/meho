// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// fakeKeycloak is an in-memory mock of the subset of the Keycloak
// admin REST API the bootstrap verb exercises. It is intentionally
// loose: it stores resources in maps, never validates the body
// against the real ClientRepresentation / UserRepresentation schemas,
// and returns the canonical 201/204/200 status codes the production
// code branches on. Replaying the same request twice mirrors the
// production behaviour (clients survive across requests; second
// create on the same clientId returns 409).
//
// The mock's goal is to verify the orchestrator's *interaction shape*
// (which endpoints get called, in what order, with what body shape)
// not to provide a real Keycloak. Real-realm verification belongs in
// the CI integration suite (testcontainers Keycloak), tracked
// separately.
type fakeKeycloak struct {
	mu sync.Mutex

	// Generates Keycloak-style UUIDs (incrementing for test
	// determinism).
	nextID int

	clients       map[string]*clientRep          // keyed by UUID
	mappers       map[string][]protocolMapperRep // keyed by client UUID
	defaultScopes map[string][]string            // client UUID -> scope ID list
	realmScopes   []scopeRep                     // realm-level client-scopes
	groups        map[string]*groupRep           // keyed by UUID
	users         map[string]*userRep            // keyed by UUID
	userGroups    map[string][]string            // user UUID -> group UUIDs
	passwords     map[string]string              // user UUID -> last reset password

	// Counts requests per (method, path-template) so tests can assert
	// idempotency: a re-run must not increment "POST /clients".
	calls map[string]int
}

func newFakeKeycloak() *fakeKeycloak {
	return &fakeKeycloak{
		clients:       map[string]*clientRep{},
		mappers:       map[string][]protocolMapperRep{},
		defaultScopes: map[string][]string{},
		realmScopes: []scopeRep{
			{ID: "scope-basic", Name: "basic"},
			{ID: "scope-roles", Name: "roles"},
			{ID: "scope-web-origins", Name: "web-origins"},
			{ID: "scope-acr", Name: "acr"},
			{ID: "scope-profile", Name: "profile"},
		},
		groups:     map[string]*groupRep{},
		users:      map[string]*userRep{},
		userGroups: map[string][]string{},
		passwords:  map[string]string{},
		calls:      map[string]int{},
	}
}

func (f *fakeKeycloak) mintID() string {
	f.nextID++
	return fmt.Sprintf("uuid-%04d", f.nextID)
}

// handler returns an http.HandlerFunc compatible with httptest.
// We branch on (method, path) — the surface is small enough that a
// hand-rolled mux is clearer than a chi router for a unit test.
func (f *fakeKeycloak) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()

		// Mint admin token (the only non-/admin/realms path).
		if r.URL.Path == "/realms/master/protocol/openid-connect/token" {
			f.calls["POST /realms/master/.../token"]++
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), "grant_type=password") {
				http.Error(w, "missing grant_type", http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(
				`{"access_token":"fake-admin-token","expires_in":60}`))
			return
		}

		// Strip the /admin/realms/{realm}/ prefix.
		const realmPrefix = "/admin/realms/"
		if !strings.HasPrefix(r.URL.Path, realmPrefix) {
			http.NotFound(w, r)
			return
		}
		rest := strings.TrimPrefix(r.URL.Path, realmPrefix)
		// rest is "<realm>/<sub>" — drop the realm segment.
		slash := strings.IndexByte(rest, '/')
		if slash < 0 {
			http.NotFound(w, r)
			return
		}
		sub := rest[slash+1:]

		switch {
		case sub == "clients" && r.Method == http.MethodGet:
			f.calls["GET /clients"]++
			clientID := r.URL.Query().Get("clientId")
			var matches []clientRep
			for _, c := range f.clients {
				if c.ClientID == clientID {
					matches = append(matches, *c)
				}
			}
			writeJSON(w, http.StatusOK, matches)
		case sub == "clients" && r.Method == http.MethodPost:
			f.calls["POST /clients"]++
			var c clientRep
			if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			// Idempotency: real Keycloak returns 409 on duplicate
			// clientId. Tests rely on this branch not firing on the
			// first run.
			for _, existing := range f.clients {
				if existing.ClientID == c.ClientID {
					w.WriteHeader(http.StatusConflict)
					return
				}
			}
			id := f.mintID()
			c.ID = id
			f.clients[id] = &c
			w.Header().Set("Location",
				fmt.Sprintf("https://fake/admin/realms/r/clients/%s", id))
			w.WriteHeader(http.StatusCreated)
		case strings.HasPrefix(sub, "clients/") &&
			r.Method == http.MethodPut &&
			!strings.Contains(sub, "/"):
			// Will be caught by the more-specific branches below.
			http.NotFound(w, r)
		case strings.HasPrefix(sub, "clients/") &&
			r.Method == http.MethodPut &&
			strings.Count(sub, "/") == 1:
			// PUT /clients/{uuid}
			f.calls["PUT /clients/{uuid}"]++
			uuid := strings.TrimPrefix(sub, "clients/")
			if _, ok := f.clients[uuid]; !ok {
				http.Error(w, "no such client", http.StatusNotFound)
				return
			}
			var c clientRep
			if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			c.ID = uuid
			f.clients[uuid] = &c
			w.WriteHeader(http.StatusNoContent)
		case strings.HasSuffix(sub, "/protocol-mappers/models") &&
			r.Method == http.MethodGet:
			f.calls["GET /clients/{uuid}/protocol-mappers/models"]++
			uuid := strings.TrimSuffix(
				strings.TrimPrefix(sub, "clients/"),
				"/protocol-mappers/models")
			writeJSON(w, http.StatusOK, f.mappers[uuid])
		case strings.HasSuffix(sub, "/protocol-mappers/models") &&
			r.Method == http.MethodPost:
			f.calls["POST /clients/{uuid}/protocol-mappers/models"]++
			uuid := strings.TrimSuffix(
				strings.TrimPrefix(sub, "clients/"),
				"/protocol-mappers/models")
			var m protocolMapperRep
			if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			m.ID = f.mintID()
			f.mappers[uuid] = append(f.mappers[uuid], m)
			w.WriteHeader(http.StatusCreated)
		case strings.Contains(sub, "/protocol-mappers/models/") &&
			r.Method == http.MethodPut:
			f.calls["PUT /clients/{uuid}/protocol-mappers/models/{id}"]++
			// .../clients/{uuid}/protocol-mappers/models/{mapperId}
			parts := strings.Split(sub, "/")
			uuid := parts[1]
			mapperID := parts[len(parts)-1]
			var m protocolMapperRep
			if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			updated := false
			for i := range f.mappers[uuid] {
				if f.mappers[uuid][i].ID == mapperID {
					m.ID = mapperID
					f.mappers[uuid][i] = m
					updated = true
					break
				}
			}
			if !updated {
				http.Error(w, "no such mapper", http.StatusNotFound)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		case sub == "client-scopes" && r.Method == http.MethodGet:
			f.calls["GET /client-scopes"]++
			writeJSON(w, http.StatusOK, f.realmScopes)
		case strings.HasSuffix(sub, "/default-client-scopes") &&
			r.Method == http.MethodGet:
			f.calls["GET /clients/{uuid}/default-client-scopes"]++
			uuid := strings.TrimSuffix(
				strings.TrimPrefix(sub, "clients/"),
				"/default-client-scopes")
			var attached []scopeRep
			for _, sid := range f.defaultScopes[uuid] {
				for _, rs := range f.realmScopes {
					if rs.ID == sid {
						attached = append(attached, rs)
					}
				}
			}
			writeJSON(w, http.StatusOK, attached)
		case strings.Contains(sub, "/default-client-scopes/") &&
			r.Method == http.MethodPut:
			f.calls["PUT /clients/{uuid}/default-client-scopes/{sid}"]++
			parts := strings.Split(sub, "/")
			uuid := parts[1]
			sid := parts[len(parts)-1]
			for _, already := range f.defaultScopes[uuid] {
				if already == sid {
					w.WriteHeader(http.StatusNoContent)
					return
				}
			}
			f.defaultScopes[uuid] = append(f.defaultScopes[uuid], sid)
			w.WriteHeader(http.StatusNoContent)
		case sub == "groups" && r.Method == http.MethodGet:
			f.calls["GET /groups"]++
			searchName := r.URL.Query().Get("search")
			var hits []groupRep
			for _, g := range f.groups {
				if searchName == "" || strings.Contains(g.Name, searchName) {
					hits = append(hits, *g)
				}
			}
			writeJSON(w, http.StatusOK, hits)
		case sub == "groups" && r.Method == http.MethodPost:
			f.calls["POST /groups"]++
			var g groupRep
			if err := json.NewDecoder(r.Body).Decode(&g); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			id := f.mintID()
			g.ID = id
			f.groups[id] = &g
			w.Header().Set("Location",
				fmt.Sprintf("https://fake/admin/realms/r/groups/%s", id))
			w.WriteHeader(http.StatusCreated)
		case sub == "users" && r.Method == http.MethodGet:
			f.calls["GET /users"]++
			username := r.URL.Query().Get("username")
			var hits []userRep
			for _, u := range f.users {
				if username == "" || u.Username == username {
					hits = append(hits, *u)
				}
			}
			writeJSON(w, http.StatusOK, hits)
		case sub == "users" && r.Method == http.MethodPost:
			f.calls["POST /users"]++
			var u userRep
			if err := json.NewDecoder(r.Body).Decode(&u); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			id := f.mintID()
			u.ID = id
			f.users[id] = &u
			w.Header().Set("Location",
				fmt.Sprintf("https://fake/admin/realms/r/users/%s", id))
			w.WriteHeader(http.StatusCreated)
		case strings.HasSuffix(sub, "/reset-password") &&
			r.Method == http.MethodPut:
			f.calls["PUT /users/{uuid}/reset-password"]++
			uuid := strings.TrimSuffix(
				strings.TrimPrefix(sub, "users/"),
				"/reset-password")
			var cred credentialRep
			if err := json.NewDecoder(r.Body).Decode(&cred); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			f.passwords[uuid] = cred.Value
			w.WriteHeader(http.StatusNoContent)
		case strings.HasPrefix(sub, "users/") &&
			strings.HasSuffix(sub, "/groups") &&
			r.Method == http.MethodGet:
			f.calls["GET /users/{uuid}/groups"]++
			uuid := strings.TrimSuffix(
				strings.TrimPrefix(sub, "users/"),
				"/groups")
			var memberships []groupRep
			for _, gid := range f.userGroups[uuid] {
				if g, ok := f.groups[gid]; ok {
					memberships = append(memberships, *g)
				}
			}
			writeJSON(w, http.StatusOK, memberships)
		case strings.Contains(sub, "/groups/") &&
			strings.HasPrefix(sub, "users/") &&
			r.Method == http.MethodPut:
			f.calls["PUT /users/{uuid}/groups/{gid}"]++
			parts := strings.Split(sub, "/")
			userUUID := parts[1]
			gid := parts[len(parts)-1]
			// idempotent
			for _, already := range f.userGroups[userUUID] {
				if already == gid {
					w.WriteHeader(http.StatusNoContent)
					return
				}
			}
			f.userGroups[userUUID] = append(f.userGroups[userUUID], gid)
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w,
				fmt.Sprintf("fakeKeycloak: unhandled %s %s",
					r.Method, r.URL.Path),
				http.StatusNotImplemented)
		}
	}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

// runBootstrapOnce is a test helper that points the package at the
// fake Keycloak and runs Bootstrap. Returns the captured stdout +
// the result struct.
func runBootstrapOnce(t *testing.T, fake *fakeKeycloak, opts BootstrapOptions) (*Result, string, error) {
	t.Helper()
	srv := httptest.NewServer(fake.handler())
	t.Cleanup(srv.Close)

	// Point Bootstrap at the fake. Use the package-internal override
	// so the test doesn't have to plumb a *http.Client through the
	// public API.
	prev := httpClientOverride
	httpClientOverride = srv.Client()
	t.Cleanup(func() { httpClientOverride = prev })

	var stdout bytes.Buffer
	opts.KeycloakBaseURL = srv.URL
	opts.Out = &stdout
	opts.Err = io.Discard

	res, err := Bootstrap(context.Background(), opts)
	return res, stdout.String(), err
}

// fullOpts is a minimally-populated BootstrapOptions for tests that
// don't care about every knob. Required fields only; recipe
// defaults fill in the rest via withDefaults().
func fullOpts() BootstrapOptions {
	return BootstrapOptions{
		Realm:             "evba",
		AdminUsername:     "admin",
		AdminPassword:     "hunter2",
		MCPResourceURI:    "https://meho.evba.lab/mcp",
		TenantID:          "438fdfa5-95ae-4303-90d6-8742123234cc",
		AdminUserUsername: "deployer@example.com",
		AdminUserPassword: "deploypw",
	}
}

func TestBootstrap_FreshRealmCreatesEverything(t *testing.T) {
	fake := newFakeKeycloak()
	res, stdout, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	// Two clients created (CLI + MCP).
	if len(fake.clients) != 2 {
		t.Errorf("expected 2 clients, got %d", len(fake.clients))
	}
	if res.CLIClientID != "meho-cli" {
		t.Errorf("CLIClientID = %q, want meho-cli", res.CLIClientID)
	}
	if res.MCPClientID != "meho-mcp-client" {
		t.Errorf("MCPClientID = %q, want meho-mcp-client", res.MCPClientID)
	}

	// Each client has 5 mappers.
	for uuid, mappers := range fake.mappers {
		if len(mappers) != 5 {
			t.Errorf("client %s has %d mappers, want 5",
				uuid, len(mappers))
		}
		names := make(map[string]bool)
		for _, m := range mappers {
			names[m.Name] = true
		}
		for _, want := range []string{
			"audience-meho-backplane",
			"meho-mcp-audience",
			"tenant-id",
			"tenant-role",
			"groups-claim",
		} {
			if !names[want] {
				t.Errorf("client %s missing mapper %q", uuid, want)
			}
		}
	}

	// Each client got 4 default scopes.
	for uuid, scopes := range fake.defaultScopes {
		if len(scopes) != 4 {
			t.Errorf("client %s has %d default scopes, want 4",
				uuid, len(scopes))
		}
	}

	// Group + user created and joined.
	if len(fake.groups) != 1 {
		t.Errorf("expected 1 group, got %d", len(fake.groups))
	}
	if len(fake.users) != 1 {
		t.Errorf("expected 1 user, got %d", len(fake.users))
	}
	var userID string
	for id := range fake.users {
		userID = id
	}
	if got := fake.passwords[userID]; got != "deploypw" {
		t.Errorf("user password = %q, want deploypw", got)
	}
	if !res.AdminGroupCreated {
		t.Errorf("AdminGroupCreated = false, want true")
	}
	if !res.AdminUserCreated {
		t.Errorf("AdminUserCreated = false, want true")
	}

	// Result carries the chart-key summary lines the cobra command
	// prints. (printSummary is invoked by the cobra RunE, not
	// Bootstrap itself; the test exercises Bootstrap directly.)
	foundChartKey := false
	for _, line := range res.ConfigKeysToSet {
		if strings.Contains(line, "config.keycloakCliClientId: meho-cli") {
			foundChartKey = true
			break
		}
	}
	if !foundChartKey {
		t.Errorf("ConfigKeysToSet missing chart key; got: %#v",
			res.ConfigKeysToSet)
	}
	// And Bootstrap's stdout records every mapper / scope event.
	if !strings.Contains(stdout, "minting Keycloak admin token") {
		t.Errorf("stdout missing token-mint progress line; got:\n%s", stdout)
	}
}

func TestBootstrap_IdempotentReRunIsNoOp(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("first Bootstrap: %v", err)
	}
	firstCreatePosts := fake.calls["POST /clients"] +
		fake.calls["POST /clients/{uuid}/protocol-mappers/models"] +
		fake.calls["POST /groups"] +
		fake.calls["POST /users"]

	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	secondCreatePosts := fake.calls["POST /clients"] +
		fake.calls["POST /clients/{uuid}/protocol-mappers/models"] +
		fake.calls["POST /groups"] +
		fake.calls["POST /users"]

	if firstCreatePosts == 0 {
		t.Fatalf("first run did not POST anything — test setup wrong")
	}
	if secondCreatePosts != firstCreatePosts {
		t.Errorf("re-run created more resources: first=%d second=%d",
			firstCreatePosts, secondCreatePosts)
	}
	// Re-run still flips the password? No — second run must not
	// reset the password (silent rotation would be a bug).
	resetCalls := fake.calls["PUT /users/{uuid}/reset-password"]
	if resetCalls != 1 {
		t.Errorf("password reset called %d times across 2 runs; want 1",
			resetCalls)
	}
}

func TestBootstrap_RefusesConfidentialClientName(t *testing.T) {
	opts := fullOpts()
	opts.CLIClientID = "meho-backplane"
	_, _, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err == nil {
		t.Fatalf("expected refusal for meho-backplane client name")
	}
	if !strings.Contains(err.Error(), "meho-backplane") {
		t.Errorf("error should mention meho-backplane; got: %v", err)
	}
}

func TestBootstrap_RefusesTrailingSlashOnMCPURI(t *testing.T) {
	opts := fullOpts()
	opts.MCPResourceURI = "https://meho.evba.lab/mcp/"
	_, _, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err == nil {
		t.Fatalf("expected refusal for trailing-slash MCP URI")
	}
	if !strings.Contains(err.Error(), "trailing slash") {
		t.Errorf("error should mention trailing slash; got: %v", err)
	}
}

func TestBootstrap_DryRunDoesNotCallKeycloak(t *testing.T) {
	opts := fullOpts()
	opts.DryRun = true
	fake := newFakeKeycloak()
	_, stdout, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap dry-run: %v", err)
	}
	if len(fake.calls) != 0 {
		t.Errorf("dry-run made %d Keycloak calls, want 0: %v",
			len(fake.calls), fake.calls)
	}
	if !strings.Contains(stdout, "DRY RUN") {
		t.Errorf("dry-run stdout missing banner; got:\n%s", stdout)
	}
}

func TestBootstrap_SkipUserProvisioningOmitsGroupAndUser(t *testing.T) {
	opts := fullOpts()
	opts.SkipUserProvisioning = true
	opts.AdminUserUsername = ""
	opts.AdminUserPassword = ""
	fake := newFakeKeycloak()
	_, _, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap with skip-user: %v", err)
	}
	if len(fake.groups) != 0 {
		t.Errorf("expected 0 groups when skipped, got %d",
			len(fake.groups))
	}
	if len(fake.users) != 0 {
		t.Errorf("expected 0 users when skipped, got %d",
			len(fake.users))
	}
	// Both clients still landed.
	if len(fake.clients) != 2 {
		t.Errorf("expected 2 clients, got %d", len(fake.clients))
	}
}

func TestBootstrap_RequiresMandatoryFlags(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(*BootstrapOptions)
		wantErr string
	}{
		{
			name:    "no keycloak url",
			mutate:  func(o *BootstrapOptions) { o.KeycloakBaseURL = "" },
			wantErr: "--keycloak-base-url",
		},
		{
			name:    "no realm",
			mutate:  func(o *BootstrapOptions) { o.Realm = "" },
			wantErr: "--realm",
		},
		{
			name:    "no admin user",
			mutate:  func(o *BootstrapOptions) { o.AdminUsername = "" },
			wantErr: "admin-username",
		},
		{
			name:    "no admin password",
			mutate:  func(o *BootstrapOptions) { o.AdminPassword = "" },
			wantErr: "admin password",
		},
		{
			name:    "no mcp resource uri",
			mutate:  func(o *BootstrapOptions) { o.MCPResourceURI = "" },
			wantErr: "--mcp-resource-uri",
		},
		{
			name:    "no admin user username",
			mutate:  func(o *BootstrapOptions) { o.AdminUserUsername = "" },
			wantErr: "--admin-user-username",
		},
		{
			name:    "no admin user password",
			mutate:  func(o *BootstrapOptions) { o.AdminUserPassword = "" },
			wantErr: "admin user password",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			opts := fullOpts()
			// KeycloakBaseURL is set inside runBootstrapOnce, so for
			// the "no keycloak url" case we have to short-circuit
			// before the fake-server hookup.
			tc.mutate(&opts)
			if tc.name == "no keycloak url" {
				_, err := Bootstrap(context.Background(), opts)
				if err == nil ||
					!strings.Contains(err.Error(), tc.wantErr) {
					t.Errorf("err = %v, want substring %q",
						err, tc.wantErr)
				}
				return
			}
			_, _, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
			if err == nil ||
				!strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("err = %v, want substring %q",
					err, tc.wantErr)
			}
		})
	}
}

func TestBootstrap_MapperShapeMatchesReferenceScript(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	// Pull any client's mappers (both have the same set).
	var mappers []protocolMapperRep
	for _, m := range fake.mappers {
		mappers = m
		break
	}
	byName := map[string]protocolMapperRep{}
	for _, m := range mappers {
		byName[m.Name] = m
	}

	// audience-meho-backplane — emits into `aud`.
	bp := byName["audience-meho-backplane"]
	if bp.ProtocolMapper != "oidc-audience-mapper" {
		t.Errorf("backplane mapper type = %q", bp.ProtocolMapper)
	}
	if bp.Config["included.client.audience"] != "meho-backplane" {
		t.Errorf("backplane mapper audience = %q",
			bp.Config["included.client.audience"])
	}
	if bp.Config["access.token.claim"] != "true" {
		t.Errorf("backplane mapper access.token.claim = %q",
			bp.Config["access.token.claim"])
	}

	// meho-mcp-audience — custom-audience form.
	mcp := byName["meho-mcp-audience"]
	if mcp.Config["included.custom.audience"] != "https://meho.evba.lab/mcp" {
		t.Errorf("mcp mapper audience = %q",
			mcp.Config["included.custom.audience"])
	}

	// tenant-id — hardcoded-claim.
	tid := byName["tenant-id"]
	if tid.ProtocolMapper != "oidc-hardcoded-claim-mapper" {
		t.Errorf("tenant-id mapper type = %q", tid.ProtocolMapper)
	}
	if tid.Config["claim.name"] != "tenant_id" {
		t.Errorf("tenant-id claim.name = %q", tid.Config["claim.name"])
	}

	// groups-claim — group-membership.
	gc := byName["groups-claim"]
	if gc.ProtocolMapper != "oidc-group-membership-mapper" {
		t.Errorf("groups-claim mapper type = %q", gc.ProtocolMapper)
	}
	if gc.Config["claim.name"] != "groups" {
		t.Errorf("groups claim name = %q", gc.Config["claim.name"])
	}
}

func TestBootstrap_AllFourDefaultScopesApplied(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	want := map[string]bool{
		"scope-basic": false, "scope-roles": false,
		"scope-web-origins": false, "scope-acr": false,
	}
	for _, scopes := range fake.defaultScopes {
		for _, sid := range scopes {
			if _, ok := want[sid]; ok {
				want[sid] = true
			}
		}
		// Reset for the next client; we only need to prove at least
		// one client got all four (both get the same set).
		break
	}
	for sid, seen := range want {
		if !seen {
			t.Errorf("default scope %s not applied", sid)
		}
	}
}

func TestBootstrap_CLIClientHasDeviceGrantAttribute(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var cliClient *clientRep
	for _, c := range fake.clients {
		if c.ClientID == "meho-cli" {
			cliClient = c
		}
	}
	if cliClient == nil {
		t.Fatalf("meho-cli client not found")
	}
	if got := cliClient.Attributes["oauth2.device.authorization.grant.enabled"]; got != "true" {
		t.Errorf("device-grant flag = %q, want true", got)
	}
	if cliClient.PublicClient == nil || !*cliClient.PublicClient {
		t.Errorf("CLI client publicClient = false, want true")
	}
	if cliClient.StandardFlowEnabled == nil || *cliClient.StandardFlowEnabled {
		t.Errorf("CLI client standardFlow should be off")
	}
}

func TestBootstrap_MCPClientHasStandardFlowAndPKCE(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var mcpClient *clientRep
	for _, c := range fake.clients {
		if c.ClientID == "meho-mcp-client" {
			mcpClient = c
		}
	}
	if mcpClient == nil {
		t.Fatalf("meho-mcp-client not found")
	}
	if mcpClient.StandardFlowEnabled == nil || !*mcpClient.StandardFlowEnabled {
		t.Errorf("MCP client standardFlow should be on")
	}
	if got := mcpClient.Attributes["pkce.code.challenge.method"]; got != "S256" {
		t.Errorf("MCP pkce method = %q, want S256", got)
	}
	if len(mcpClient.RedirectURIs) == 0 {
		t.Errorf("MCP client must have at least one redirect URI")
	}
}
