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
	"slices"
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

	clients        map[string]*clientRep          // keyed by UUID
	mappers        map[string][]protocolMapperRep // keyed by client UUID
	defaultScopes  map[string][]string            // client UUID -> scope ID list
	optionalScopes map[string][]string            // client UUID -> scope ID list
	realmScopes    []scopeRep                     // realm-level client-scopes
	createdScopes  []clientScopeRep               // bodies of POST /client-scopes
	groups         map[string]*groupRep           // keyed by UUID
	users          map[string]*userRep            // keyed by UUID
	userGroups     map[string][]string            // user UUID -> group UUIDs
	passwords      map[string]string              // user UUID -> last reset password
	flows          map[string]*fakeFlow           // login flows, keyed by alias

	// Counts requests per (method, path-template) so tests can assert
	// idempotency: a re-run must not increment "POST /clients".
	calls map[string]int
}

func newFakeKeycloak() *fakeKeycloak {
	return &fakeKeycloak{
		clients:        map[string]*clientRep{},
		mappers:        map[string][]protocolMapperRep{},
		defaultScopes:  map[string][]string{},
		optionalScopes: map[string][]string{},
		realmScopes: []scopeRep{
			{ID: "scope-basic", Name: "basic"},
			{ID: "scope-roles", Name: "roles"},
			{ID: "scope-web-origins", Name: "web-origins"},
			{ID: "scope-acr", Name: "acr"},
			{ID: "scope-profile", Name: "profile"},
			// Realm-built-in: Keycloak provisions `offline_access` at
			// realm-create time. The MCP bootstrap depends on its
			// presence to attach as an optional scope (RDC Wall W7 /
			// #912).
			{ID: "scope-offline-access", Name: "offline_access"},
		},
		groups:     map[string]*groupRep{},
		users:      map[string]*userRep{},
		userGroups: map[string][]string{},
		passwords:  map[string]string{},
		flows:      map[string]*fakeFlow{},
		calls:      map[string]int{},
	}
}

// fakeFlow is one login flow of the fake realm with its steps.
type fakeFlow struct {
	ID       string
	TopLevel bool
	Steps    []*fakeFlowStep
}

type fakeFlowStep struct {
	ID          string
	Provider    string
	Subflow     string
	Requirement string
}

// flowSteps lists a flow's steps the way Keycloak does: the steps of a
// sub-flow follow the sub-flow.
func (f *fakeKeycloak) flowSteps(alias string, level int) []map[string]any {
	var out []map[string]any
	for _, s := range f.flows[alias].Steps {
		if s.Subflow != "" {
			out = append(out, map[string]any{
				"id": s.ID, "displayName": s.Subflow, "authenticationFlow": true,
				"requirement": s.Requirement, "level": level,
			})
			out = append(out, f.flowSteps(s.Subflow, level+1)...)
			continue
		}
		out = append(out, map[string]any{
			"id": s.ID, "displayName": "display of " + s.Provider, "providerId": s.Provider,
			"authenticationFlow": false, "requirement": s.Requirement, "level": level,
		})
	}
	return out
}

// handleFlows serves the login flow endpoints; false when sub is not one.
func (f *fakeKeycloak) handleFlows(w http.ResponseWriter, r *http.Request, sub string) bool {
	const prefix = "authentication/flows"
	if !strings.HasPrefix(sub, prefix) {
		return false
	}
	f.calls[r.Method+" /authentication/flows..."]++
	rest := strings.TrimPrefix(strings.TrimPrefix(sub, prefix), "/")
	switch {
	case rest == "" && r.Method == http.MethodGet:
		var list []flowRep
		for alias, fl := range f.flows {
			if fl.TopLevel {
				list = append(list, flowRep{ID: fl.ID, Alias: alias})
			}
		}
		writeJSON(w, http.StatusOK, list)
	case rest == "" && r.Method == http.MethodPost:
		var rep flowRep
		_ = json.NewDecoder(r.Body).Decode(&rep)
		if _, ok := f.flows[rep.Alias]; ok || !rep.TopLevel || rep.BuiltIn {
			w.WriteHeader(http.StatusConflict)
			return true
		}
		f.flows[rep.Alias] = &fakeFlow{ID: f.mintID(), TopLevel: true}
		w.WriteHeader(http.StatusCreated)
	default:
		alias, tail, _ := strings.Cut(rest, "/")
		parent, ok := f.flows[alias]
		if !ok {
			http.Error(w, "Parent flow doesn't exist", http.StatusBadRequest)
			return true
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		switch {
		case tail == "executions/execution" && r.Method == http.MethodPost:
			provider, _ := body["provider"].(string)
			parent.Steps = append(parent.Steps, &fakeFlowStep{
				ID: f.mintID(), Provider: provider, Requirement: "DISABLED"})
			w.WriteHeader(http.StatusCreated)
		case tail == "executions/flow" && r.Method == http.MethodPost:
			name, _ := body["alias"].(string)
			f.flows[name] = &fakeFlow{ID: f.mintID()}
			parent.Steps = append(parent.Steps, &fakeFlowStep{
				ID: f.mintID(), Subflow: name, Requirement: "DISABLED"})
			w.WriteHeader(http.StatusCreated)
		case tail == "executions" && r.Method == http.MethodGet:
			writeJSON(w, http.StatusOK, f.flowSteps(alias, 0))
		case tail == "executions" && r.Method == http.MethodPut:
			for _, fl := range f.flows {
				for _, s := range fl.Steps {
					if s.ID == body["id"] {
						s.Requirement, _ = body["requirement"].(string)
						w.WriteHeader(http.StatusNoContent)
						return true
					}
				}
			}
			http.Error(w, "Illegal execution", http.StatusNotFound)
		default:
			return false
		}
	}
	return true
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
		if f.handleFlows(w, r, sub) {
			return
		}

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
		case sub == "client-scopes" && r.Method == http.MethodPost:
			f.calls["POST /client-scopes"]++
			var rep clientScopeRep
			if err := json.NewDecoder(r.Body).Decode(&rep); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			for _, rs := range f.realmScopes {
				if rs.Name == rep.Name {
					http.Error(w, "conflict", http.StatusConflict)
					return
				}
			}
			f.realmScopes = append(f.realmScopes,
				scopeRep{ID: "scope-" + rep.Name, Name: rep.Name})
			f.createdScopes = append(f.createdScopes, rep)
			w.WriteHeader(http.StatusCreated)
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
		case strings.HasSuffix(sub, "/optional-client-scopes") &&
			r.Method == http.MethodGet:
			f.calls["GET /clients/{uuid}/optional-client-scopes"]++
			uuid := strings.TrimSuffix(
				strings.TrimPrefix(sub, "clients/"),
				"/optional-client-scopes")
			var attached []scopeRep
			for _, sid := range f.optionalScopes[uuid] {
				for _, rs := range f.realmScopes {
					if rs.ID == sid {
						attached = append(attached, rs)
					}
				}
			}
			writeJSON(w, http.StatusOK, attached)
		case strings.Contains(sub, "/optional-client-scopes/") &&
			r.Method == http.MethodPut:
			f.calls["PUT /clients/{uuid}/optional-client-scopes/{sid}"]++
			parts := strings.Split(sub, "/")
			uuid := parts[1]
			sid := parts[len(parts)-1]
			for _, already := range f.optionalScopes[uuid] {
				if already == sid {
					w.WriteHeader(http.StatusNoContent)
					return
				}
			}
			f.optionalScopes[uuid] = append(f.optionalScopes[uuid], sid)
			w.WriteHeader(http.StatusNoContent)
		case sub == "groups" && r.Method == http.MethodGet:
			f.calls["GET /groups"]++
			// Like Keycloak: the search is a case-insensitive substring
			// match, and the answer is the short form (no attributes).
			searchName := strings.ToLower(r.URL.Query().Get("search"))
			var hits []groupRep
			for _, g := range f.groups {
				if searchName == "" ||
					strings.Contains(strings.ToLower(g.Name), searchName) {
					hits = append(hits, groupRep{ID: g.ID, Name: g.Name})
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
		case strings.HasPrefix(sub, "groups/") &&
			strings.Count(sub, "/") == 1 &&
			r.Method == http.MethodGet:
			f.calls["GET /groups/{uuid}"]++
			g, ok := f.groups[strings.TrimPrefix(sub, "groups/")]
			if !ok {
				http.Error(w, "no such group", http.StatusNotFound)
				return
			}
			writeJSON(w, http.StatusOK, g)
		case strings.HasPrefix(sub, "groups/") &&
			strings.Count(sub, "/") == 1 &&
			r.Method == http.MethodPut:
			f.calls["PUT /groups/{uuid}"]++
			id := strings.TrimPrefix(sub, "groups/")
			g, ok := f.groups[id]
			if !ok {
				http.Error(w, "no such group", http.StatusNotFound)
				return
			}
			var in groupRep
			if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			// Like Keycloak: a sent attribute map replaces the old one.
			g.Name = in.Name
			g.Description = in.Description
			if in.Attributes != nil {
				g.Attributes = in.Attributes
			}
			w.WriteHeader(http.StatusNoContent)
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

	// Three clients created (CLI, approval sign-in, MCP).
	if len(fake.clients) != 3 {
		t.Errorf("expected 3 clients, got %d", len(fake.clients))
	}
	if res.ApproveClientID != "meho-cli-approve" {
		t.Errorf("ApproveClientID = %q, want meho-cli-approve", res.ApproveClientID)
	}
	if res.CLIClientID != "meho-cli" {
		t.Errorf("CLIClientID = %q, want meho-cli", res.CLIClientID)
	}
	if res.MCPClientID != "meho-mcp" {
		t.Errorf("MCPClientID = %q, want meho-mcp", res.MCPClientID)
	}

	// The CLI and MCP clients have 8 mappers each; the approval client
	// has the same without meho-mcp-audience.
	for uuid, mappers := range fake.mappers {
		names := make(map[string]bool)
		for _, m := range mappers {
			names[m.Name] = true
		}
		if fake.clients[uuid].ClientID == "meho-cli-approve" {
			if len(mappers) != 7 || names["meho-mcp-audience"] {
				t.Errorf("approval client has mappers %v, want 7 without meho-mcp-audience", names)
			}
			continue
		}
		if len(mappers) != 8 {
			t.Errorf("client %s has %d mappers, want 8",
				uuid, len(mappers))
		}
		for _, want := range []string{
			"audience-meho-backplane",
			"meho-mcp-audience",
			"tenant-id",
			"tenant-role",
			"approver",
			"platform-admin",
			"meho-tenants",
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

	// Five role groups plus the platform group, one user, and the user
	// is in MEHO-Admins and MEHO-Platform-Admins.
	if len(fake.groups) != 6 {
		t.Errorf("expected 6 groups, got %d", len(fake.groups))
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
	if got := fake.userGroupNames(userID); !slices.Equal(got, []string{"MEHO-Admins", "MEHO-Platform-Admins"}) {
		t.Errorf("user groups = %v, want [MEHO-Admins MEHO-Platform-Admins]", got)
	}
	if res.AdminGroupUUID != fake.groupByName("MEHO-Admins").ID {
		t.Errorf("AdminGroupUUID = %q, want the MEHO-Admins UUID", res.AdminGroupUUID)
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

	firstUpdates := fake.calls["PUT /clients/{uuid}/protocol-mappers/models/{id}"] +
		fake.calls["PUT /groups/{uuid}"] +
		fake.calls["PUT /users/{uuid}/groups/{gid}"]

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	secondCreatePosts := fake.calls["POST /clients"] +
		fake.calls["POST /clients/{uuid}/protocol-mappers/models"] +
		fake.calls["POST /groups"] +
		fake.calls["POST /users"]
	// Mappers, groups and the group join must not be touched again.
	secondUpdates := fake.calls["PUT /clients/{uuid}/protocol-mappers/models/{id}"] +
		fake.calls["PUT /groups/{uuid}"] +
		fake.calls["PUT /users/{uuid}/groups/{gid}"]
	if secondUpdates != firstUpdates {
		t.Errorf("re-run updated mappers, groups or memberships: first=%d second=%d",
			firstUpdates, secondUpdates)
	}
	for _, line := range res.MapperEventsLog {
		if strings.Contains(line, "mapper") || strings.Contains(line, "group") {
			if !strings.HasPrefix(line, "[skip]") {
				t.Errorf("re-run event %q, want [skip]", line)
			}
		}
	}
	if res.AdminUserCreated || res.AdminUserJoined {
		t.Errorf("re-run created or re-joined the admin user: %+v", res)
	}

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

// Skipping the user step still creates the five groups: they carry the
// tenant and the role for people who come from a directory.
func TestBootstrap_SkipUserProvisioningKeepsGroupsOmitsUser(t *testing.T) {
	opts := fullOpts()
	opts.SkipUserProvisioning = true
	opts.AdminUserUsername = ""
	opts.AdminUserPassword = ""
	fake := newFakeKeycloak()
	_, _, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap with skip-user: %v", err)
	}
	if len(fake.groups) != 6 {
		t.Errorf("expected the 5 role groups and the platform group "+
			"when the user step is skipped, got %d", len(fake.groups))
	}
	if len(fake.users) != 0 {
		t.Errorf("expected 0 users when skipped, got %d",
			len(fake.users))
	}
	// All three clients still landed.
	if len(fake.clients) != 3 {
		t.Errorf("expected 3 clients, got %d", len(fake.clients))
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
	// The CLI client has the full set (the approval client has no
	// meho-mcp-audience).
	byName := fake.clientMappers(t, "meho-cli")

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

	// tenant-id / tenant-role / approver — stock User Attribute mappers
	// that read the group attributes. No fixed value anywhere.
	for _, tc := range []struct {
		mapper, attr, jsonType string
	}{
		{"tenant-id", "tenant_id", "String"},
		{"tenant-role", "tenant_role", "String"},
		{"approver", "approver", "boolean"},
		{"platform-admin", "platform_admin", "boolean"},
	} {
		m := byName[tc.mapper]
		if m.ProtocolMapper != "oidc-usermodel-attribute-mapper" {
			t.Errorf("%s mapper type = %q, want oidc-usermodel-attribute-mapper",
				tc.mapper, m.ProtocolMapper)
		}
		want := map[string]string{
			"user.attribute":     tc.attr,
			"claim.name":         tc.attr,
			"jsonType.label":     tc.jsonType,
			"multivalued":        "false",
			"aggregate.attrs":    "false",
			"access.token.claim": "true",
			"id.token.claim":     "false",
		}
		for k, v := range want {
			if m.Config[k] != v {
				t.Errorf("%s config %s = %q, want %q", tc.mapper, k, m.Config[k], v)
			}
		}
		if _, ok := m.Config["claim.value"]; ok {
			t.Errorf("%s carries a fixed claim.value", tc.mapper)
		}
	}

	// meho-tenants — collects meho_tenant from every group of the person
	// into the meho_tenants list.
	mt := byName["meho-tenants"]
	if mt.ProtocolMapper != "oidc-usermodel-attribute-mapper" {
		t.Errorf("meho-tenants mapper type = %q", mt.ProtocolMapper)
	}
	for k, v := range map[string]string{
		"user.attribute":       "meho_tenant",
		"claim.name":           "meho_tenants",
		"jsonType.label":       "String",
		"multivalued":          "true",
		"aggregate.attrs":      "true",
		"access.token.claim":   "true",
		"userinfo.token.claim": "true",
		"id.token.claim":       "false",
	} {
		if mt.Config[k] != v {
			t.Errorf("meho-tenants config %s = %q, want %q", k, mt.Config[k], v)
		}
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

// TestBootstrap_CLIOfflineAccessOptInAssignsScopeAndIdle is the opt-in
// contract for #2902: when a deployment sets
// CLIOfflineSessionIdleSeconds (the `--cli-offline-access` flag), the
// device-code CLI client gains `offline_access` as an OPTIONAL scope
// (never default) plus a bounded `client.offline.session.idle.timeout`
// attribute — the same opt-in shape #912 gave the MCP client, but off
// by default for the CLI.
func TestBootstrap_CLIOfflineAccessOptInAssignsScopeAndIdle(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	opts.CLIOfflineSessionIdleSeconds = 172800
	if _, _, err := runBootstrapOnce(t, fake, opts); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	var cliUUID string
	var cliClient *clientRep
	for uuid, c := range fake.clients {
		if c.ClientID == "meho-cli" {
			cliUUID = uuid
			cliClient = c
		}
	}
	if cliClient == nil {
		t.Fatalf("meho-cli client not found")
	}

	// offline_access is attached as an OPTIONAL scope on the CLI client.
	foundOptional := false
	for _, sid := range fake.optionalScopes[cliUUID] {
		if sid == "scope-offline-access" {
			foundOptional = true
			break
		}
	}
	if !foundOptional {
		t.Errorf("meho-cli missing optional scope offline_access when "+
			"opted in; got optionalScopes=%v", fake.optionalScopes[cliUUID])
	}

	// ...and NOT as a default scope (else every device login would mint
	// a refresh token even without --offline).
	for _, sid := range fake.defaultScopes[cliUUID] {
		if sid == "scope-offline-access" {
			t.Errorf("meho-cli has offline_access as DEFAULT — must be " +
				"optional only")
		}
	}

	// The per-client offline-session idle timeout is bounded to the
	// requested value (Keycloak attr client.offline.session.idle.timeout).
	if got := cliClient.Attributes["client.offline.session.idle.timeout"]; got != "172800" {
		t.Errorf("client.offline.session.idle.timeout = %q, want 172800", got)
	}
}

// TestBootstrap_DefaultRunLeavesCLIOfflineAccessOff is the
// off-by-default contract for #2902: a bootstrap run that does NOT opt
// in must leave the CLI client exactly as before — no offline_access
// scope and no offline-session-idle attribute — so existing
// deployments are unaffected.
func TestBootstrap_DefaultRunLeavesCLIOfflineAccessOff(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var cliUUID string
	var cliClient *clientRep
	for uuid, c := range fake.clients {
		if c.ClientID == "meho-cli" {
			cliUUID = uuid
			cliClient = c
		}
	}
	if cliClient == nil {
		t.Fatalf("meho-cli client not found")
	}
	for _, sid := range fake.optionalScopes[cliUUID] {
		if sid == "scope-offline-access" {
			t.Errorf("default run assigned offline_access to meho-cli; " +
				"must be opt-in via --cli-offline-access")
		}
	}
	if _, ok := cliClient.Attributes["client.offline.session.idle.timeout"]; ok {
		t.Errorf("default run set client.offline.session.idle.timeout on " +
			"meho-cli; must be opt-in only")
	}
}

// TestBootstrap_RejectsNegativeCLIOfflineIdle pins that validate()
// rejects a negative --cli-offline-access value (0 = off is fine; a
// negative idle timeout is nonsensical and would otherwise be sent to
// Keycloak verbatim).
func TestBootstrap_RejectsNegativeCLIOfflineIdle(t *testing.T) {
	opts := fullOpts()
	opts.KeycloakBaseURL = "https://keycloak.evba.lab"
	opts.CLIOfflineSessionIdleSeconds = -1
	err := opts.withDefaults().validate()
	if err == nil {
		t.Fatal("expected validate() to reject a negative " +
			"--cli-offline-access value")
	}
	if !strings.Contains(err.Error(), "--cli-offline-access") {
		t.Errorf("error = %v, want mention of --cli-offline-access", err)
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
	// The browser flow is on for the fresh sign-in of an approval
	// (#3979): PKCE S256 and loopback redirects only.
	if cliClient.StandardFlowEnabled == nil || !*cliClient.StandardFlowEnabled {
		t.Errorf("CLI client standardFlow should be on")
	}
	if got := cliClient.Attributes["pkce.code.challenge.method"]; got != "S256" {
		t.Errorf("CLI client PKCE method = %q, want S256", got)
	}
	if !slices.Equal(cliClient.RedirectURIs, []string{"http://127.0.0.1:*"}) {
		t.Errorf("CLI client redirect URIs = %v, want the 127.0.0.1 loopback only", cliClient.RedirectURIs)
	}
	if len(cliClient.WebOrigins) != 0 {
		t.Errorf("CLI client web origins = %v, want none", cliClient.WebOrigins)
	}
}

func TestBootstrap_MCPClientHasStandardFlowAndPKCE(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var mcpClient *clientRep
	for _, c := range fake.clients {
		if c.ClientID == "meho-mcp" {
			mcpClient = c
		}
	}
	if mcpClient == nil {
		t.Fatalf("meho-mcp not found")
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

// TestBootstrap_MCPClientDefaultRedirectURIsLoopbackOnly is the
// regression contract for #2793: when no --mcp-redirect-uri is given,
// withDefaults() must apply the loopback-only set covering both host
// forms (localhost + 127.0.0.1). Keycloak matches redirect hosts
// literally, so `http://localhost:*` alone does NOT cover the
// `127.0.0.1` host that mcp-remote's `/oauth/callback` uses (#2666).
// The dead claude.ai cloud callback — structurally unreachable for
// internal-only MEHO (#2744) — must not be defaulted.
func TestBootstrap_MCPClientDefaultRedirectURIsLoopbackOnly(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var mcpClient *clientRep
	for _, c := range fake.clients {
		if c.ClientID == "meho-mcp" {
			mcpClient = c
		}
	}
	if mcpClient == nil {
		t.Fatalf("meho-mcp not found")
	}

	want := []string{"http://localhost:*", "http://127.0.0.1:*"}
	if !slices.Equal(mcpClient.RedirectURIs, want) {
		t.Errorf("default MCP redirect URIs = %v, want %v",
			mcpClient.RedirectURIs, want)
	}
	for _, uri := range mcpClient.RedirectURIs {
		if strings.Contains(uri, "claude.ai") {
			t.Errorf("default MCP redirect URIs must not include a "+
				"claude.ai cloud callback (internal-only MEHO can never "+
				"exercise it); got %q", uri)
		}
	}
}

// TestBootstrap_MCPClientHasOfflineAccessOptionalScope is the
// regression contract for RDC Wall W7 (#912): Claude Code's MCP
// client always requests `offline_access` for a refresh token, so the
// MCP browser-flow client must carry that scope as an optional client
// scope. The CLI device-code client is deliberately NOT given it —
// device-code clients re-run the device dance instead of holding a
// long-lived refresh token. The test pins both halves of that
// asymmetry plus the idempotency invariant.
func TestBootstrap_MCPClientHasOfflineAccessOptionalScope(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	// Locate the two clients by clientId.
	var cliUUID, mcpUUID string
	for uuid, c := range fake.clients {
		switch c.ClientID {
		case "meho-cli":
			cliUUID = uuid
		case "meho-mcp":
			mcpUUID = uuid
		}
	}
	if cliUUID == "" {
		t.Fatalf("meho-cli client not found")
	}
	if mcpUUID == "" {
		t.Fatalf("meho-mcp not found")
	}

	// MCP client: offline_access is attached as an OPTIONAL scope.
	mcpOpt := fake.optionalScopes[mcpUUID]
	foundOnMCP := false
	for _, sid := range mcpOpt {
		if sid == "scope-offline-access" {
			foundOnMCP = true
			break
		}
	}
	if !foundOnMCP {
		t.Errorf("meho-mcp missing optional scope offline_access; "+
			"got optionalScopes=%v", mcpOpt)
	}

	// MCP client: offline_access is NOT also a default scope (that
	// would mint a refresh token into every flow regardless of
	// whether the client requested it).
	for _, sid := range fake.defaultScopes[mcpUUID] {
		if sid == "scope-offline-access" {
			t.Errorf("meho-mcp has offline_access as DEFAULT — " +
				"must be optional only (else every browser-flow login " +
				"mints a refresh token even when offline_access isn't " +
				"requested)")
		}
	}

	// CLI device-code client: offline_access is NOT attached at all
	// (neither default nor optional). Device-code clients re-run the
	// device dance; a refresh token is more attack surface than
	// re-prompting the operator.
	for _, sid := range fake.optionalScopes[cliUUID] {
		if sid == "scope-offline-access" {
			t.Errorf("meho-cli has offline_access as optional — must " +
				"not have it (RFC 8628 device-code flow doesn't need a " +
				"refresh token; a stolen refresh token is worse than " +
				"re-running the device dance)")
		}
	}
	for _, sid := range fake.defaultScopes[cliUUID] {
		if sid == "scope-offline-access" {
			t.Errorf("meho-cli has offline_access as default — must not")
		}
	}

	// Idempotency contract: re-running the bootstrap against the same
	// realm must not re-PUT the optional scope (the production code
	// pre-checks via listClientOptionalScopes; PUTting again would be
	// harmless against real Keycloak but indicates a regression in
	// the skip-if-already-present path).
	putsAfterFirst := fake.calls["PUT /clients/{uuid}/optional-client-scopes/{sid}"]
	if want := len(requiredMCPOptionalScopes()); putsAfterFirst != want {
		t.Errorf("first run PUT'd optional scopes %d times; want %d",
			putsAfterFirst, want)
	}
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	putsAfterSecond := fake.calls["PUT /clients/{uuid}/optional-client-scopes/{sid}"]
	if putsAfterSecond != putsAfterFirst {
		t.Errorf("re-run PUT'd optional scope again: first=%d second=%d "+
			"(reconcileOptionalScopes idempotency regressed)",
			putsAfterFirst, putsAfterSecond)
	}
}

// TestReadPassword_NonTTYFallbackReadsPipedSecret asserts that when the
// reader is not an *os.File on a TTY (a piped secret, as every unit test
// and shell here-string supplies), readPassword still takes the plain
// buffered line read and returns the secret unchanged — the TTY branch's
// term.ReadPassword only engages for a real terminal.
func TestReadPassword_NonTTYFallbackReadsPipedSecret(t *testing.T) {
	const want = "s3cr3t-from-pipe"
	var errOut bytes.Buffer
	got, err := readPassword(strings.NewReader(want+"\n"), &errOut, "Password: ")
	if err != nil {
		t.Fatalf("readPassword returned error: %v", err)
	}
	if got != want {
		t.Fatalf("readPassword = %q, want %q", got, want)
	}
	if errOut.String() != "Password: " {
		t.Fatalf("prompt written to errOut = %q, want %q", errOut.String(), "Password: ")
	}
}

// TestBootstrap_MCPClientOffersMCPScopes is the regression contract for
// #4002: the Claude Code plugin and the Claude Desktop bundle ask for
// `mcp:read mcp:execute` (plus `mcp:admin` when elevated), and Keycloak
// refuses a login that asks for a scope the client does not offer. So
// bootstrap must create the three scopes in the realm and offer them on
// meho-mcp as optional scopes, and a re-run must not create them again.
func TestBootstrap_MCPClientOffersMCPScopes(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	want := []string{"mcp:read", "mcp:execute", "mcp:admin"}

	if len(fake.createdScopes) != len(want) {
		t.Fatalf("created %d client scopes, want %d: %+v",
			len(fake.createdScopes), len(want), fake.createdScopes)
	}
	for i, rep := range fake.createdScopes {
		if rep.Name != want[i] {
			t.Errorf("created scope %d = %q, want %q", i, rep.Name, want[i])
		}
		if rep.Protocol != "openid-connect" {
			t.Errorf("scope %s protocol = %q, want openid-connect",
				rep.Name, rep.Protocol)
		}
		if rep.Attributes["include.in.token.scope"] != "true" {
			t.Errorf("scope %s must set include.in.token.scope=true "+
				"(the backplane reads mcp:admin from the scope claim)",
				rep.Name)
		}
	}

	var cliUUID, mcpUUID string
	for uuid, c := range fake.clients {
		switch c.ClientID {
		case "meho-cli":
			cliUUID = uuid
		case "meho-mcp":
			mcpUUID = uuid
		}
	}
	has := func(ids []string, id string) bool {
		for _, got := range ids {
			if got == id {
				return true
			}
		}
		return false
	}
	for _, name := range want {
		id := "scope-" + name
		if !has(fake.optionalScopes[mcpUUID], id) {
			t.Errorf("meho-mcp missing optional scope %s; got %v",
				name, fake.optionalScopes[mcpUUID])
		}
		if has(fake.defaultScopes[mcpUUID], id) {
			t.Errorf("meho-mcp has %s as DEFAULT — must be optional", name)
		}
		if has(fake.optionalScopes[cliUUID], id) ||
			has(fake.defaultScopes[cliUUID], id) {
			t.Errorf("meho-cli must not get %s", name)
		}
	}

	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if got := fake.calls["POST /client-scopes"]; got != len(want) {
		t.Errorf("POST /client-scopes called %d times across 2 runs; "+
			"want %d (re-run must skip existing scopes)", got, len(want))
	}
}

// groupByName returns the stored group with this exact name, or nil.
func (f *fakeKeycloak) groupByName(name string) *groupRep {
	for _, g := range f.groups {
		if g.Name == name {
			return g
		}
	}
	return nil
}

// userGroupNames returns the sorted names of the groups the user is in.
func (f *fakeKeycloak) userGroupNames(userUUID string) []string {
	var names []string
	for _, gid := range f.userGroups[userUUID] {
		if g, ok := f.groups[gid]; ok {
			names = append(names, g.Name)
		}
	}
	slices.Sort(names)
	return names
}

// clientMappers returns the mappers stored on the client with this
// clientId, keyed by mapper name.
func (f *fakeKeycloak) clientMappers(t *testing.T, clientID string) map[string]protocolMapperRep {
	t.Helper()
	for uuid, c := range f.clients {
		if c.ClientID == clientID {
			byName := map[string]protocolMapperRep{}
			for _, m := range f.mappers[uuid] {
				byName[m.Name] = m
			}
			return byName
		}
	}
	t.Fatalf("client %s not found", clientID)
	return nil
}

// The five groups carry the decided attributes: the tenant from
// --tenant-id, the role per group, and approver=true only on
// MEHO-Approvers.
func TestBootstrap_CreatesFiveRoleGroupsWithAttributes(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	if _, _, err := runBootstrapOnce(t, fake, opts); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	tenant := []string{opts.TenantID}
	mt := func(role string) []string { return []string{opts.TenantID + ":main:" + role} }
	want := map[string]map[string][]string{
		"MEHO-Readers": {
			"tenant_id": tenant, "tenant_role": {"read_only"}, "meho_tenant": mt("Readers"),
		},
		"MEHO-Operators": {
			"tenant_id": tenant, "tenant_role": {"operator"}, "meho_tenant": mt("Operators"),
		},
		"MEHO-SA-Operators": {
			"tenant_id": tenant, "tenant_role": {"operator"}, "meho_tenant": mt("SA-Operators"),
		},
		"MEHO-Admins": {
			"tenant_id": tenant, "tenant_role": {"tenant_admin"}, "meho_tenant": mt("Admins"),
		},
		"MEHO-Approvers": {
			"tenant_id": tenant, "tenant_role": {"read_only"}, "approver": {"true"},
			"meho_tenant": mt("Approvers"),
		},
		"MEHO-Platform-Admins": {"platform_admin": {"true"}},
	}
	for name, attrs := range want {
		g := fake.groupByName(name)
		if g == nil {
			t.Errorf("group %s not created", name)
			continue
		}
		if len(g.Attributes) != len(attrs) {
			t.Errorf("group %s attributes = %v, want %v", name, g.Attributes, attrs)
			continue
		}
		for k, v := range attrs {
			if !slices.Equal(g.Attributes[k], v) {
				t.Errorf("group %s %s = %v, want %v", name, k, g.Attributes[k], v)
			}
		}
	}
}

// A realm made by the older version of the command has fixed-value
// tenant-id / tenant-role mappers on both clients and a `meho-admins`
// group with the admin user in it. A run replaces the two fixed mappers
// with User Attribute mappers, adds the approver mapper, creates the
// five groups and puts the admin user in MEHO-Admins. Old groups are
// left alone. A second run then changes nothing.
func TestBootstrap_UpgradeReplacesFixedValueMappers(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts().withDefaults()
	oldFixed := func(name, claim, value string) protocolMapperRep {
		return protocolMapperRep{
			ID:             "mapper-old-" + name,
			Name:           name,
			Protocol:       "openid-connect",
			ProtocolMapper: "oidc-hardcoded-claim-mapper",
			Config: map[string]string{
				"claim.name":           claim,
				"claim.value":          value,
				"jsonType.label":       "String",
				"id.token.claim":       "false",
				"access.token.claim":   "true",
				"userinfo.token.claim": "false",
			},
		}
	}
	for _, c := range []*clientRep{opts.desiredCLIClient(), opts.desiredMCPClient()} {
		c.ID = "uuid-old-" + c.ClientID
		fake.clients[c.ID] = c
		fake.mappers[c.ID] = []protocolMapperRep{
			oldFixed("tenant-id", "tenant_id", opts.TenantID),
			oldFixed("tenant-role", "tenant_role", "tenant_admin"),
		}
	}
	fake.groups["uuid-old-group"] = &groupRep{ID: "uuid-old-group", Name: "meho-admins"}
	fake.users["uuid-old-user"] = &userRep{ID: "uuid-old-user", Username: opts.AdminUserUsername}
	fake.userGroups["uuid-old-user"] = []string{"uuid-old-group"}

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	for _, clientID := range []string{"meho-cli", "meho-mcp"} {
		byName := fake.clientMappers(t, clientID)
		if len(byName) != 8 {
			t.Errorf("%s has %d mappers, want 8", clientID, len(byName))
		}
		for name, m := range byName {
			if m.ProtocolMapper == "oidc-hardcoded-claim-mapper" {
				t.Errorf("%s still has fixed-value mapper %s", clientID, name)
			}
		}
		for _, name := range []string{"tenant-id", "tenant-role", "approver"} {
			if got := byName[name].ProtocolMapper; got != "oidc-usermodel-attribute-mapper" {
				t.Errorf("%s mapper %s type = %q", clientID, name, got)
			}
		}
	}
	replaced := 0
	for _, line := range res.MapperEventsLog {
		if strings.Contains(line, "replaced oidc-hardcoded-claim-mapper with oidc-usermodel-attribute-mapper") {
			replaced++
		}
	}
	if replaced != 4 {
		t.Errorf("got %d mapper-replaced events, want 4 (2 mappers x 2 clients): %v",
			replaced, res.MapperEventsLog)
	}

	if fake.groupByName("meho-admins") == nil {
		t.Errorf("old meho-admins group was removed; old groups must stay")
	}
	if got := fake.userGroupNames("uuid-old-user"); !slices.Equal(got, []string{"MEHO-Admins", "MEHO-Platform-Admins", "meho-admins"}) {
		t.Errorf("admin user groups = %v, want [MEHO-Admins MEHO-Platform-Admins meho-admins]", got)
	}
	if res.AdminUserCreated || !res.AdminUserJoined {
		t.Errorf("want an existing user that joined MEHO-Admins, got %+v", res)
	}
	if n := fake.calls["PUT /users/{uuid}/reset-password"]; n != 0 {
		t.Errorf("existing user's password reset %d times, want 0", n)
	}

	putsBefore := fake.calls["PUT /clients/{uuid}/protocol-mappers/models/{id}"] +
		fake.calls["PUT /groups/{uuid}"] + fake.calls["POST /groups"] +
		fake.calls["POST /clients/{uuid}/protocol-mappers/models"]
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	putsAfter := fake.calls["PUT /clients/{uuid}/protocol-mappers/models/{id}"] +
		fake.calls["PUT /groups/{uuid}"] + fake.calls["POST /groups"] +
		fake.calls["POST /clients/{uuid}/protocol-mappers/models"]
	if putsAfter != putsBefore {
		t.Errorf("second run after the upgrade changed mappers or groups: %d -> %d",
			putsBefore, putsAfter)
	}
}

// A role group whose attributes were changed by hand is set back. Only
// the three keys the command owns change: other attributes stay, and
// `approver` is removed from groups other than MEHO-Approvers.
func TestBootstrap_FixesChangedGroupAttributes(t *testing.T) {
	fake := newFakeKeycloak()
	fake.groups["uuid-ops"] = &groupRep{
		ID: "uuid-ops", Name: "MEHO-Operators",
		Attributes: map[string][]string{
			"tenant_id":   {"11111111-1111-1111-1111-111111111111"},
			"tenant_role": {"tenant_admin"},
			"note":        {"kept"},
		},
	}
	fake.groups["uuid-readers"] = &groupRep{
		ID: "uuid-readers", Name: "MEHO-Readers",
		Attributes: map[string][]string{
			"tenant_id":   {fullOpts().TenantID},
			"tenant_role": {"read_only"},
			"approver":    {"true"},
		},
	}
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	ops := fake.groups["uuid-ops"].Attributes
	if !slices.Equal(ops["tenant_role"], []string{"operator"}) ||
		!slices.Equal(ops["tenant_id"], []string{fullOpts().TenantID}) {
		t.Errorf("MEHO-Operators attributes not fixed: %v", ops)
	}
	if !slices.Equal(ops["note"], []string{"kept"}) {
		t.Errorf("MEHO-Operators lost an attribute the command does not own: %v", ops)
	}
	if _, ok := fake.groups["uuid-readers"].Attributes["approver"]; ok {
		t.Errorf("MEHO-Readers still has approver: %v", fake.groups["uuid-readers"].Attributes)
	}
	if n := fake.calls["PUT /groups/{uuid}"]; n != 2 {
		t.Errorf("PUT /groups/{uuid} called %d times, want 2", n)
	}
	if n := fake.calls["POST /groups"]; n != 4 {
		t.Errorf("POST /groups called %d times, want 4 (the missing groups)", n)
	}
	for _, name := range []string{"MEHO-Operators", "MEHO-Readers"} {
		found := false
		for _, line := range res.MapperEventsLog {
			if strings.HasPrefix(line, "[updated] group "+name+" ") {
				found = true
			}
		}
		if !found {
			t.Errorf("no [updated] event for %s: %v", name, res.MapperEventsLog)
		}
	}
}

// The dry run lists the five groups with their attributes.
func TestBootstrap_DryRunListsRoleGroups(t *testing.T) {
	opts := fullOpts()
	opts.DryRun = true
	_, stdout, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err != nil {
		t.Fatalf("Bootstrap dry-run: %v", err)
	}
	want := "MEHO-Approvers: tenant_id=" + opts.TenantID +
		" tenant_role=read_only approver=true meho_tenant=" + opts.TenantID + ":main:Approvers"
	if !strings.Contains(stdout, want) {
		t.Errorf("dry-run stdout missing %q; got:\n%s", want, stdout)
	}
}

// --tenant-role and --admin-group-name are gone: the role comes from the
// group, and the first admin always joins MEHO-Admins. A script that
// still passes one stops before any Keycloak call.
func TestBootstrapClientsCmd_RemovedFlagsStop(t *testing.T) {
	for _, flag := range []string{"--tenant-role", "--admin-group-name"} {
		t.Run(flag, func(t *testing.T) {
			cmd := newBootstrapClientsCmd()
			cmd.SetArgs([]string{
				"--keycloak-base-url", "https://keycloak.example.com",
				"--realm", "meho", flag, "x",
			})
			cmd.SetOut(io.Discard)
			cmd.SetErr(io.Discard)
			err := cmd.Execute()
			if err == nil || !strings.Contains(err.Error(), "unknown flag: "+flag) {
				t.Errorf("err = %v, want unknown flag: %s", err, flag)
			}
		})
	}
}

// TestBootstrap_ClientDescriptionsFitKeycloak pins the 255-character
// limit of Keycloak's client description column: a longer description
// makes the client create fail with HTTP 500.
func TestBootstrap_ClientDescriptionsFitKeycloak(t *testing.T) {
	opts := fullOpts().withDefaults()
	for _, c := range []*clientRep{opts.desiredCLIClient(), opts.desiredCLIApproveClient(), opts.desiredMCPClient()} {
		if n := len(c.Description); n > 255 {
			t.Errorf("%s description is %d characters, want at most 255", c.ClientID, n)
		}
	}
}

// TestBootstrap_ApproveClientShape checks the client of the approval
// sign-in (#5515): browser sign-in with PKCE on a 127.0.0.1 loopback
// only, no device flow, no offline_access, the four default scopes.
func TestBootstrap_ApproveClientShape(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var c *clientRep
	for _, rep := range fake.clients {
		if rep.ClientID == "meho-cli-approve" {
			c = rep
		}
	}
	if c == nil {
		t.Fatal("meho-cli-approve client not created")
	}
	if c.PublicClient == nil || !*c.PublicClient || c.StandardFlowEnabled == nil || !*c.StandardFlowEnabled {
		t.Errorf("want a public client with the browser flow: %+v", c)
	}
	if c.DirectAccessGrantsEnabled == nil || *c.DirectAccessGrantsEnabled {
		t.Errorf("direct access grants must be off")
	}
	if got := c.Attributes["oauth2.device.authorization.grant.enabled"]; got != "false" {
		t.Errorf("device flow = %q, want false", got)
	}
	if got := c.Attributes["pkce.code.challenge.method"]; got != "S256" {
		t.Errorf("PKCE = %q, want S256", got)
	}
	if got := c.Attributes["use.refresh.tokens"]; got != "false" {
		t.Errorf("refresh tokens = %q, want false", got)
	}
	if got := c.Attributes["access.token.lifespan"]; got != "900" {
		t.Errorf("access token lifespan = %q, want 900 (the approval window)", got)
	}
	if !slices.Equal(c.RedirectURIs, []string{"http://127.0.0.1:*"}) {
		t.Errorf("redirect URIs = %v", c.RedirectURIs)
	}
	if len(c.OptionalClientScopes) != 0 {
		t.Errorf("optional scopes = %v, want none (no offline_access)", c.OptionalClientScopes)
	}
	if got := fake.defaultScopes[c.ID]; len(got) != 4 {
		t.Errorf("default scopes = %v, want the four", got)
	}
	flow := fake.flows[approvalFlowAlias]
	if flow == nil {
		t.Fatalf("login flow %q not created", approvalFlowAlias)
	}
	if got := c.AuthenticationFlowBindingOverrides["browser"]; got != flow.ID {
		t.Errorf("browser flow of the approval client = %q, want %q", got, flow.ID)
	}
}

// The approval client's login flow has the password form and the
// one-time code step, and nothing else.
func TestBootstrap_ApprovalFlowSteps(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var got []string
	for _, s := range fake.flowSteps(approvalFlowAlias, 0) {
		name, _ := s["providerId"].(string)
		if s["authenticationFlow"] == true {
			name = s["displayName"].(string)
		}
		got = append(got, fmt.Sprintf("%d %s %s", s["level"], name, s["requirement"]))
	}
	want := []string{
		"0 auth-username-password-form REQUIRED",
		"0 " + approvalCodeFlowAlias + " CONDITIONAL",
		"1 conditional-user-configured REQUIRED",
		"1 auth-otp-form REQUIRED",
	}
	if !slices.Equal(got, want) {
		t.Errorf("steps = %v, want %v", got, want)
	}
	// Only the approval client is bound to a flow of its own.
	for _, c := range fake.clients {
		if c.ClientID != "meho-cli-approve" && len(c.AuthenticationFlowBindingOverrides) > 0 {
			t.Errorf("client %s has a login flow binding", c.ClientID)
		}
	}
}

// An existing flow is kept; an existing approval client without the
// binding gets it, and gets the token settings of the approval client.
func TestBootstrap_ReRunKeepsFlowAndBindsExistingClient(t *testing.T) {
	fake := newFakeKeycloak()
	fake.flows[approvalFlowAlias] = &fakeFlow{ID: "flow-existing", TopLevel: true}
	old := fullOpts().withDefaults().desiredCLIApproveClient()
	old.ID = "uuid-existing-approve"
	old.Attributes["use.refresh.tokens"] = "true"
	delete(old.Attributes, "access.token.lifespan")
	fake.clients[old.ID] = old

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := fake.clients[old.ID].AuthenticationFlowBindingOverrides["browser"]; got != "flow-existing" {
		t.Errorf("browser flow = %q, want flow-existing", got)
	}
	if got := fake.clients[old.ID].Attributes; got["use.refresh.tokens"] != "false" || got["access.token.lifespan"] != "900" {
		t.Errorf("token settings of the existing approval client = %v, want no refresh token and 900", got)
	}
	if n := fake.calls["POST /authentication/flows..."]; n != 0 {
		t.Errorf("POST to the flow API %d times, want 0", n)
	}
	if !slices.Contains(res.MapperEventsLog, `[skip] login flow "meho approval sign-in" exists`) {
		t.Errorf("events miss the kept flow line: %v", res.MapperEventsLog)
	}
}
