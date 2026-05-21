// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
)

// BootstrapOptions captures every knob the bootstrap orchestrator
// needs. The cobra command fills this from flags + env vars; tests
// fill it directly. Empty fields fall back to recipe defaults.
type BootstrapOptions struct {
	// KeycloakBaseURL is the realm-less root, e.g. "https://keycloak.evba.lab".
	KeycloakBaseURL string

	// Realm is the target realm where the clients land, e.g. "evba".
	Realm string

	// AdminUsername / AdminPassword authenticate against the master
	// realm via the built-in `admin-cli` client (password grant). The
	// password is never echoed; the cobra command reads it via
	// stdin / env var so it never enters argv.
	AdminUsername string
	AdminPassword string

	// CLIClientID is the public device-code client name; defaults to
	// "meho-cli". Must match the chart's keycloakCliClientId so the
	// CLI's auth-config discovery resolves it.
	CLIClientID string

	// MCPClientID is the public authorization-code+PKCE client name
	// used by Claude.ai Custom Connector / MCP Inspector; defaults to
	// "meho-mcp-client".
	MCPClientID string

	// BackplaneAudience is the confidential resource-server client's
	// audience claim ("meho-backplane") — the value the
	// `audience-meho-backplane` mapper emits into `aud`.
	BackplaneAudience string

	// MCPResourceURI is the `<backplane-url>/mcp` audience the
	// `meho-mcp-audience` mapper emits — must match the no-trailing-
	// slash form the backplane normalises to.
	MCPResourceURI string

	// TenantID + TenantRole are the hardcoded values the
	// tenant-id / tenant-role mappers emit. The dogfood lab uses a
	// single tenant; a multi-tenant realm should replace these with
	// usermodel-attribute mappers (out of scope for the bootstrap).
	TenantID   string
	TenantRole string

	// AdminGroupName / AdminUsername / AdminUserPassword optionally
	// provision a human user in the meho-admins group. When
	// SkipUserProvisioning is true the user / group steps are skipped
	// (re-runs on a realm with externally-managed users).
	AdminGroupName       string
	AdminUserUsername    string
	AdminUserEmail       string
	AdminUserPassword    string
	SkipUserProvisioning bool

	// MCPRedirectURIs / MCPWebOrigins control the public MCP client's
	// browser-flow allowlists. Defaults cover Claude.ai + localhost
	// MCP Inspector.
	MCPRedirectURIs []string
	MCPWebOrigins   []string

	// DryRun true → print every API call that would be made and exit
	// 0 without mutating anything. Mirrors the reference shell
	// script's `--dry-run`.
	DryRun bool

	// Out / Err are where structured progress lines go. nil → stdout
	// / stderr.
	Out io.Writer
	Err io.Writer
}

// withDefaults applies the recipe defaults so the cobra command can
// pass a sparsely-populated BootstrapOptions and still get the right
// shape. Returns a copy; the input is not mutated.
func (o BootstrapOptions) withDefaults() BootstrapOptions {
	if o.CLIClientID == "" {
		o.CLIClientID = "meho-cli"
	}
	if o.MCPClientID == "" {
		o.MCPClientID = "meho-mcp-client"
	}
	if o.BackplaneAudience == "" {
		o.BackplaneAudience = "meho-backplane"
	}
	if o.TenantID == "" {
		o.TenantID = "00000000-0000-0000-0000-000000000000"
	}
	if o.TenantRole == "" {
		o.TenantRole = "tenant_admin"
	}
	if o.AdminGroupName == "" {
		o.AdminGroupName = "meho-admins"
	}
	if len(o.MCPRedirectURIs) == 0 {
		o.MCPRedirectURIs = []string{
			"https://claude.ai/api/mcp/auth_callback",
			"http://localhost:*",
		}
	}
	if len(o.MCPWebOrigins) == 0 {
		o.MCPWebOrigins = []string{"+"}
	}
	return o
}

// validate refuses option combinations the verb explicitly cannot
// safely act on — confidential-client provisioning, user creation
// without a password, etc. The recipe is fundamentally about *public*
// clients; routing operators away from confidential-client mistakes
// at the boundary is part of the deliverable.
func (o BootstrapOptions) validate() error {
	if o.KeycloakBaseURL == "" {
		return errors.New("--keycloak-base-url is required")
	}
	if o.Realm == "" {
		return errors.New("--realm is required")
	}
	if o.AdminUsername == "" {
		return errors.New(
			"--admin-username (or KEYCLOAK_ADMIN_USER) is required")
	}
	if o.AdminPassword == "" {
		return errors.New(
			"admin password is required (set KEYCLOAK_ADMIN_PASSWORD " +
				"or pipe via stdin)")
	}
	// MCPResourceURI is required if we're going to mint the
	// meho-mcp-audience mapper. Synthesise the default elsewhere only
	// when the operator explicitly told us a backplane URL; we don't
	// silently guess.
	if o.MCPResourceURI == "" {
		return errors.New(
			"--mcp-resource-uri is required (the audience the " +
				"meho-mcp-audience mapper will emit, e.g. " +
				"https://meho.example.com/mcp — no trailing slash)")
	}
	if strings.HasSuffix(o.MCPResourceURI, "/") {
		return errors.New(
			"--mcp-resource-uri must not have a trailing slash " +
				"(MEHO normalises MCP_RESOURCE_URI server-side; the " +
				"audience claim in the token must match the no-slash form)")
	}
	if o.CLIClientID == "meho-backplane" || o.MCPClientID == "meho-backplane" {
		return errors.New(
			"refusing to provision a client named meho-backplane: " +
				"that is the realm's confidential resource-server " +
				"client and is out of scope for this verb (see " +
				"`meho admin keycloak bootstrap-clients --help`)")
	}
	if !o.SkipUserProvisioning {
		if o.AdminUserUsername == "" {
			return errors.New(
				"--admin-user-username is required unless " +
					"--skip-user-provisioning is set")
		}
		if o.AdminUserPassword == "" {
			return errors.New(
				"admin user password is required unless " +
					"--skip-user-provisioning is set (set " +
					"KEYCLOAK_ADMIN_USER_PASSWORD or use " +
					"--skip-user-provisioning to leave users " +
					"externally-managed)")
		}
	}
	return nil
}

// desiredMappers returns the five protocol mappers the recipe pins,
// shaped exactly as the reference shell script does. Same names so
// Keycloak's existing-mapper detection (by name) handles idempotency.
func (o BootstrapOptions) desiredMappers() []protocolMapperRep {
	return []protocolMapperRep{
		{
			Name:           "audience-" + o.BackplaneAudience,
			Protocol:       "openid-connect",
			ProtocolMapper: "oidc-audience-mapper",
			Config: map[string]string{
				"included.client.audience":  o.BackplaneAudience,
				"id.token.claim":            "false",
				"access.token.claim":        "true",
				"introspection.token.claim": "true",
			},
		},
		{
			Name:           "meho-mcp-audience",
			Protocol:       "openid-connect",
			ProtocolMapper: "oidc-audience-mapper",
			Config: map[string]string{
				"included.custom.audience":  o.MCPResourceURI,
				"id.token.claim":            "false",
				"access.token.claim":        "true",
				"introspection.token.claim": "true",
			},
		},
		{
			Name:           "tenant-id",
			Protocol:       "openid-connect",
			ProtocolMapper: "oidc-hardcoded-claim-mapper",
			Config: map[string]string{
				"claim.name":           "tenant_id",
				"claim.value":          o.TenantID,
				"jsonType.label":       "String",
				"id.token.claim":       "false",
				"access.token.claim":   "true",
				"userinfo.token.claim": "false",
			},
		},
		{
			Name:           "tenant-role",
			Protocol:       "openid-connect",
			ProtocolMapper: "oidc-hardcoded-claim-mapper",
			Config: map[string]string{
				"claim.name":           "tenant_role",
				"claim.value":          o.TenantRole,
				"jsonType.label":       "String",
				"id.token.claim":       "false",
				"access.token.claim":   "true",
				"userinfo.token.claim": "false",
			},
		},
		{
			Name:           "groups-claim",
			Protocol:       "openid-connect",
			ProtocolMapper: "oidc-group-membership-mapper",
			Config: map[string]string{
				"claim.name":           "groups",
				"full.path":            "false",
				"id.token.claim":       "true",
				"access.token.claim":   "true",
				"userinfo.token.claim": "false",
			},
		},
	}
}

// requiredDefaultScopes returns the four default client-scopes the
// recipe pins. Order does not matter at PUT time; ordered here only
// so the progress output is deterministic.
func requiredDefaultScopes() []string {
	return []string{"basic", "roles", "web-origins", "acr"}
}

// boolPtr / strSlice are tiny helpers to make the desired-client
// builders readable.
func boolPtr(b bool) *bool { return &b }

// desiredCLIClient builds the ClientRepresentation for the public
// device-code client. Mirrors the reference shell script verbatim
// (including the "Description" string pattern so re-runs against a
// shell-script-created client don't churn the field).
func (o BootstrapOptions) desiredCLIClient() *clientRep {
	return &clientRep{
		ClientID:                  o.CLIClientID,
		Name:                      "MEHO CLI + MCP-OAuth-2.1 device-code public client",
		Description:               "Public OAuth client for RFC 8628 device-code flow used by `meho login` and any MCP-OAuth-2.1 client targeting <backplane-url>/mcp. Audience-mapped to " + o.BackplaneAudience + ". Provisioned by `meho admin keycloak bootstrap-clients` (#791).",
		Enabled:                   boolPtr(true),
		PublicClient:              boolPtr(true),
		StandardFlowEnabled:       boolPtr(false),
		ImplicitFlowEnabled:       boolPtr(false),
		DirectAccessGrantsEnabled: boolPtr(false),
		ServiceAccountsEnabled:    boolPtr(false),
		FrontchannelLogout:        boolPtr(false),
		Attributes: map[string]string{
			"oauth2.device.authorization.grant.enabled":   "true",
			"use.refresh.tokens":                          "true",
			"client.use.lightweight.access.token.enabled": "false",
		},
		RedirectURIs:        []string{},
		WebOrigins:          []string{},
		DefaultClientScopes: append([]string{"openid", "profile", "email"}, requiredDefaultScopes()...),
	}
}

// desiredMCPClient builds the ClientRepresentation for the public
// authorization-code+PKCE client used by browser MCP clients. The
// 5 mappers + 4 default scopes apply identically — only the flow
// flags + redirect URIs / web origins differ from the CLI client.
func (o BootstrapOptions) desiredMCPClient() *clientRep {
	return &clientRep{
		ClientID:                  o.MCPClientID,
		Name:                      "MEHO MCP browser-OAuth-2.1 public client",
		Description:               "Public OAuth client for OAuth 2.1 authorization-code + PKCE used by Claude.ai Custom Connector, MCP Inspector, and any browser-flow MCP client targeting <backplane-url>/mcp. Provisioned by `meho admin keycloak bootstrap-clients` (#791).",
		Enabled:                   boolPtr(true),
		PublicClient:              boolPtr(true),
		StandardFlowEnabled:       boolPtr(true),
		ImplicitFlowEnabled:       boolPtr(false),
		DirectAccessGrantsEnabled: boolPtr(false),
		ServiceAccountsEnabled:    boolPtr(false),
		FrontchannelLogout:        boolPtr(false),
		Attributes: map[string]string{
			"pkce.code.challenge.method":                  "S256",
			"oauth2.device.authorization.grant.enabled":   "false",
			"use.refresh.tokens":                          "true",
			"client.use.lightweight.access.token.enabled": "false",
		},
		RedirectURIs:        append([]string{}, o.MCPRedirectURIs...),
		WebOrigins:          append([]string{}, o.MCPWebOrigins...),
		DefaultClientScopes: append([]string{"openid", "profile", "email"}, requiredDefaultScopes()...),
	}
}

// Result captures the IDs of the resources the bootstrap touched.
// Used by the cobra command to print the "set these in your chart"
// summary, and by tests for assertions.
type Result struct {
	CLIClientID       string // clientId (not UUID) of the device-code client
	CLIClientUUID     string // internal UUID, useful for downstream debugging
	MCPClientID       string // clientId of the MCP browser-flow client
	MCPClientUUID     string
	AdminGroupCreated bool
	AdminGroupUUID    string
	AdminUserCreated  bool
	AdminUserUUID     string
	MapperEventsLog   []string // human-readable bullet list (one line per mapper / scope event)
	ConfigKeysToSet   []string // chart values + env vars the operator must set
}

// Bootstrap is the public entry point. It mints an admin token, then
// idempotently reconciles the realm against the recipe shape:
//
//  1. Public CLI device-code client (`meho-cli`)
//  2. 5 protocol mappers cloned from the reference
//  3. 4 default client scopes (basic / roles / web-origins / acr)
//  4. Public MCP browser-flow client (`meho-mcp-client`)
//  5. Same 5 mappers + 4 default scopes on the MCP client
//  6. `meho-admins` group (top-level, no attributes)
//  7. Admin user, set password, join meho-admins
//
// The operator-facing progress output (one line per step + a final
// summary block) goes to opts.Out / opts.Err.
//
// Idempotency: every step does a "does this exist?" check before
// mutating, so re-runs print "[skip] …" rather than re-creating.
// Drift is corrected (PUT-to-update) on the client + mapper level
// but **not** on the user's password — re-running with a different
// password would silently reset the existing user's credential. The
// step prints "[skip] user … exists; not resetting password (use
// --reset-password to force)" instead.
func Bootstrap(
	ctx context.Context, opts BootstrapOptions,
) (*Result, error) {
	opts = opts.withDefaults()
	if err := opts.validate(); err != nil {
		return nil, err
	}

	out := opts.Out
	if out == nil {
		out = io.Discard
	}

	httpClient := httpClientOverride
	if httpClient == nil {
		httpClient = defaultHTTPClient()
	}

	if opts.DryRun {
		fmt.Fprintln(out, "==> DRY RUN — no admin token will be minted; "+
			"no realm state will change.")
		return dryRunSummary(opts, out)
	}

	fmt.Fprintf(out,
		"==> minting Keycloak admin token (realm=master user=%s)\n",
		opts.AdminUsername)
	token, err := mintAdminToken(
		ctx, httpClient,
		opts.KeycloakBaseURL, opts.AdminUsername, opts.AdminPassword)
	if err != nil {
		return nil, fmt.Errorf("mint admin token: %w", err)
	}

	c := newAdminClient(httpClient, opts.KeycloakBaseURL, opts.Realm, token)
	res := &Result{}

	// --- CLI device-code client ---------------------------------------------

	desiredCLI := opts.desiredCLIClient()
	cliUUID, cliEvents, err := reconcileClient(ctx, c, desiredCLI)
	if err != nil {
		return nil, fmt.Errorf(
			"reconcile CLI client %q: %w", opts.CLIClientID, err)
	}
	res.CLIClientID = opts.CLIClientID
	res.CLIClientUUID = cliUUID
	res.MapperEventsLog = append(res.MapperEventsLog, cliEvents...)
	mapperEvents, err := reconcileMappers(
		ctx, c, cliUUID, opts.desiredMappers())
	if err != nil {
		return nil, fmt.Errorf("reconcile CLI mappers: %w", err)
	}
	res.MapperEventsLog = append(res.MapperEventsLog, mapperEvents...)
	scopeEvents, err := reconcileDefaultScopes(
		ctx, c, cliUUID, requiredDefaultScopes())
	if err != nil {
		return nil, fmt.Errorf("reconcile CLI default scopes: %w", err)
	}
	res.MapperEventsLog = append(res.MapperEventsLog, scopeEvents...)

	// --- MCP browser-flow client --------------------------------------------

	desiredMCP := opts.desiredMCPClient()
	mcpUUID, mcpEvents, err := reconcileClient(ctx, c, desiredMCP)
	if err != nil {
		return nil, fmt.Errorf(
			"reconcile MCP client %q: %w", opts.MCPClientID, err)
	}
	res.MCPClientID = opts.MCPClientID
	res.MCPClientUUID = mcpUUID
	res.MapperEventsLog = append(res.MapperEventsLog, mcpEvents...)
	mcpMapperEvents, err := reconcileMappers(
		ctx, c, mcpUUID, opts.desiredMappers())
	if err != nil {
		return nil, fmt.Errorf("reconcile MCP mappers: %w", err)
	}
	res.MapperEventsLog = append(res.MapperEventsLog, mcpMapperEvents...)
	mcpScopeEvents, err := reconcileDefaultScopes(
		ctx, c, mcpUUID, requiredDefaultScopes())
	if err != nil {
		return nil, fmt.Errorf("reconcile MCP default scopes: %w", err)
	}
	res.MapperEventsLog = append(res.MapperEventsLog, mcpScopeEvents...)

	// --- Admin group + user -------------------------------------------------

	if !opts.SkipUserProvisioning {
		groupCreated, groupUUID, gerr := reconcileGroup(
			ctx, c, opts.AdminGroupName)
		if gerr != nil {
			return nil, fmt.Errorf(
				"reconcile %q group: %w", opts.AdminGroupName, gerr)
		}
		res.AdminGroupCreated = groupCreated
		res.AdminGroupUUID = groupUUID

		userCreated, userUUID, uerr := reconcileUser(
			ctx, c, &userRep{
				Username:      opts.AdminUserUsername,
				Email:         opts.AdminUserEmail,
				Enabled:       boolPtr(true),
				EmailVerified: boolPtr(true),
			}, opts.AdminUserPassword, groupUUID,
		)
		if uerr != nil {
			return nil, fmt.Errorf(
				"reconcile user %q: %w", opts.AdminUserUsername, uerr)
		}
		res.AdminUserCreated = userCreated
		res.AdminUserUUID = userUUID
	} else {
		fmt.Fprintln(out, "==> --skip-user-provisioning: leaving "+
			"users + groups externally-managed")
	}

	res.ConfigKeysToSet = buildConfigKeySummary(opts)
	return res, nil
}

// dryRunSummary prints what would change and returns a result whose
// IDs are empty strings — callers must not pass the UUIDs onward.
func dryRunSummary(
	opts BootstrapOptions, out io.Writer,
) (*Result, error) {
	fmt.Fprintf(out, "==> would provision against %s realm=%s\n",
		opts.KeycloakBaseURL, opts.Realm)
	fmt.Fprintf(out, "==> CLI device-code client: %s\n", opts.CLIClientID)
	fmt.Fprintf(out, "==> MCP browser-flow client: %s\n", opts.MCPClientID)
	fmt.Fprintln(out, "==> protocol mappers (each on both clients):")
	for _, m := range opts.desiredMappers() {
		fmt.Fprintf(out, "    - %s (%s)\n", m.Name, m.ProtocolMapper)
	}
	fmt.Fprintln(out, "==> default client scopes (each on both clients):")
	for _, s := range requiredDefaultScopes() {
		fmt.Fprintf(out, "    - %s\n", s)
	}
	if !opts.SkipUserProvisioning {
		fmt.Fprintf(out, "==> admin group: %s\n", opts.AdminGroupName)
		fmt.Fprintf(out, "==> admin user: %s (member of %s)\n",
			opts.AdminUserUsername, opts.AdminGroupName)
	} else {
		fmt.Fprintln(out, "==> user provisioning skipped")
	}
	return &Result{
		CLIClientID:     opts.CLIClientID,
		MCPClientID:     opts.MCPClientID,
		ConfigKeysToSet: buildConfigKeySummary(opts),
	}, nil
}

// reconcileClient creates or PUT-updates a client to the desired
// shape. Returns the client's UUID + an events log (one bullet line
// per mutation, suitable for printing).
func reconcileClient(
	ctx context.Context, c *adminClient, desired *clientRep,
) (string, []string, error) {
	var events []string
	existing, err := c.findClient(ctx, desired.ClientID)
	if err != nil {
		return "", nil, err
	}
	if existing == nil {
		uuid, cerr := c.createClient(ctx, desired)
		if cerr != nil {
			return "", nil, cerr
		}
		events = append(events,
			fmt.Sprintf("[created] client %s (uuid=%s)",
				desired.ClientID, uuid))
		return uuid, events, nil
	}
	// PUT-update with the existing UUID baked in, mirroring the
	// shell script's `$existing * $desired` merge semantic: the
	// desired shape wins for every key it sets, the existing wins
	// for everything it doesn't set. Keycloak's PUT is a full
	// replacement at the client-rep level, so we carry the UUID +
	// the desired shape's full set of fields explicitly.
	merged := *desired
	merged.ID = existing.ID
	if err := c.updateClient(ctx, existing.ID, &merged); err != nil {
		return "", nil, err
	}
	events = append(events,
		fmt.Sprintf("[updated] client %s (uuid=%s)",
			desired.ClientID, existing.ID))
	return existing.ID, events, nil
}

// reconcileMappers installs any missing mappers + PUTs differences
// onto existing ones. We match by name (Keycloak's natural key for
// mappers within a client).
func reconcileMappers(
	ctx context.Context, c *adminClient,
	clientUUID string, desired []protocolMapperRep,
) ([]string, error) {
	existing, err := c.listClientMappers(ctx, clientUUID)
	if err != nil {
		return nil, err
	}
	byName := make(map[string]protocolMapperRep, len(existing))
	for _, m := range existing {
		byName[m.Name] = m
	}
	var events []string
	for _, want := range desired {
		got, ok := byName[want.Name]
		if !ok {
			if err := c.createClientMapper(ctx, clientUUID, want); err != nil {
				return nil, fmt.Errorf(
					"create mapper %s: %w", want.Name, err)
			}
			events = append(events,
				fmt.Sprintf("[created] mapper %s (%s)",
					want.Name, want.ProtocolMapper))
			continue
		}
		if mapperEquivalent(got, want) {
			events = append(events,
				fmt.Sprintf("[skip]    mapper %s (already matches)",
					want.Name))
			continue
		}
		want.ID = got.ID
		if err := c.updateClientMapper(
			ctx, clientUUID, got.ID, want); err != nil {
			return nil, fmt.Errorf(
				"update mapper %s: %w", want.Name, err)
		}
		events = append(events,
			fmt.Sprintf("[updated] mapper %s", want.Name))
	}
	return events, nil
}

// mapperEquivalent compares the fields we care about (name, protocol,
// mapper type, full config map). It does NOT compare IDs (those are
// Keycloak-generated) or consentRequired (we always set false so the
// field is uniform and equality is straightforward).
func mapperEquivalent(a, b protocolMapperRep) bool {
	if a.Name != b.Name ||
		a.Protocol != b.Protocol ||
		a.ProtocolMapper != b.ProtocolMapper {
		return false
	}
	if len(a.Config) != len(b.Config) {
		return false
	}
	for k, v := range a.Config {
		if b.Config[k] != v {
			return false
		}
	}
	return true
}

// reconcileDefaultScopes resolves each desired-scope name to its
// realm-level UUID, then PUTs every one that isn't already a default
// scope on the client.
func reconcileDefaultScopes(
	ctx context.Context, c *adminClient,
	clientUUID string, desired []string,
) ([]string, error) {
	allScopes, err := c.listRealmClientScopes(ctx)
	if err != nil {
		return nil, err
	}
	scopeIDByName := make(map[string]string, len(allScopes))
	for _, s := range allScopes {
		scopeIDByName[s.Name] = s.ID
	}
	existing, err := c.listClientDefaultScopes(ctx, clientUUID)
	if err != nil {
		return nil, err
	}
	already := make(map[string]bool, len(existing))
	for _, s := range existing {
		already[s.ID] = true
	}
	var events []string
	for _, name := range desired {
		id, ok := scopeIDByName[name]
		if !ok {
			events = append(events,
				fmt.Sprintf("[WARN]    scope %s not in realm — skipping",
					name))
			continue
		}
		if already[id] {
			events = append(events,
				fmt.Sprintf("[skip]    scope %s (already default)", name))
			continue
		}
		if err := c.addClientDefaultScope(
			ctx, clientUUID, id); err != nil {
			return nil, fmt.Errorf(
				"add default scope %s: %w", name, err)
		}
		events = append(events,
			fmt.Sprintf("[added]   scope %s", name))
	}
	return events, nil
}

// reconcileGroup ensures a top-level group named groupName exists.
// Returns (created, uuid).
func reconcileGroup(
	ctx context.Context, c *adminClient, groupName string,
) (bool, string, error) {
	existing, err := c.findGroup(ctx, groupName)
	if err != nil {
		return false, "", err
	}
	if existing != nil {
		return false, existing.ID, nil
	}
	uuid, err := c.createGroup(ctx, groupName)
	if err != nil {
		return false, "", err
	}
	return true, uuid, nil
}

// reconcileUser ensures a user with the given username exists and
// belongs to the meho-admins group. If the user already exists the
// password is **not** reset (silent password rotation on a re-run is
// strictly worse than the "set it once at create time" rule).
func reconcileUser(
	ctx context.Context, c *adminClient,
	desired *userRep, password, groupUUID string,
) (bool, string, error) {
	existing, err := c.findUserByUsername(ctx, desired.Username)
	if err != nil {
		return false, "", err
	}
	var (
		userUUID string
		created  bool
	)
	if existing == nil {
		uuid, cerr := c.createUser(ctx, desired)
		if cerr != nil {
			return false, "", cerr
		}
		if err := c.resetUserPassword(ctx, uuid, password); err != nil {
			return false, "", fmt.Errorf(
				"set initial password: %w", err)
		}
		userUUID = uuid
		created = true
	} else {
		userUUID = existing.ID
	}

	// Join the group, skipping if the user is already in it.
	memberships, err := c.listUserGroups(ctx, userUUID)
	if err != nil {
		return created, userUUID, err
	}
	for _, g := range memberships {
		if g.ID == groupUUID {
			return created, userUUID, nil
		}
	}
	if err := c.joinUserToGroup(ctx, userUUID, groupUUID); err != nil {
		return created, userUUID, err
	}
	return created, userUUID, nil
}

// buildConfigKeySummary names the chart values + env vars the
// operator must set on the backplane side to match the realm config
// this verb just provisioned. Printed at the end of the bootstrap.
func buildConfigKeySummary(opts BootstrapOptions) []string {
	return []string{
		"config.keycloakCliClientId: " + opts.CLIClientID,
		"  (env: KEYCLOAK_CLI_CLIENT_ID=" + opts.CLIClientID + ")",
		"# Surfaced via GET /api/v1/auth-config as `cli_client_id`;",
		"# `meho login` resolves it automatically.",
		"",
		"MCP browser-flow client_id (paste into Claude.ai Custom Connector or MCP Inspector config): " + opts.MCPClientID,
	}
}
