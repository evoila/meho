// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"errors"
	"fmt"
	"io"
	"slices"
	"strconv"
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
	// used by browser-flow MCP clients — the mcp-remote stdio shim
	// (Claude Desktop), MCP Inspector, and Claude Code / Cursor
	// (loopback PKCE); defaults to "meho-mcp".
	MCPClientID string

	// BackplaneAudience is the confidential resource-server client's
	// audience claim ("meho-backplane") — the value the
	// `audience-meho-backplane` mapper emits into `aud`.
	BackplaneAudience string

	// MCPResourceURI is the `<backplane-url>/mcp` audience the
	// `meho-mcp-audience` mapper emits — must match the no-trailing-
	// slash form the backplane normalises to.
	MCPResourceURI string

	// TenantID is the tenant UUID written as the `tenant_id` attribute
	// on the five MEHO role groups (see roleGroups). One site has one
	// tenant for now.
	TenantID string

	// AdminUserUsername / AdminUserEmail / AdminUserPassword provision
	// the first admin user and put it in MEHO-Admins. When
	// SkipUserProvisioning is true the user step is skipped (people come
	// from a directory or another login system). The five groups are
	// created either way: they carry the tenant and the role.
	AdminUserUsername    string
	AdminUserEmail       string
	AdminUserPassword    string
	SkipUserProvisioning bool

	// MCPRedirectURIs / MCPWebOrigins control the public MCP client's
	// browser-flow allowlists. Defaults cover the loopback callbacks
	// (localhost + 127.0.0.1, any port/path) that mcp-remote and Claude
	// Code listen on — Keycloak matches redirect hosts literally, so
	// both loopback forms are listed.
	MCPRedirectURIs []string
	MCPWebOrigins   []string

	// CLIOfflineSessionIdleSeconds opts the device-code CLI client into
	// the long-lived offline-token path (#2902). When > 0, bootstrap
	// assigns the realm-built-in `offline_access` scope to the CLI
	// client as an *optional* scope (so `meho login --offline` can mint
	// a refresh token that survives the SSO idle timeout) and sets the
	// per-client `client.offline.session.idle.timeout` attribute to
	// this many seconds, bounding how long an unused offline token
	// stays valid. Zero (the default) preserves the historical posture:
	// no offline_access on the CLI client at all — a stolen device-code
	// refresh token has worse blast-radius than re-running the device
	// dance, so the offline path is strictly opt-in and off by default.
	// Mirrors the opt-in shape #912 gave the MCP client, bounded for
	// CLI use.
	CLIOfflineSessionIdleSeconds int

	// MCPOfflineSessionIdleSeconds sets the per-client
	// `client.offline.session.idle.timeout` attribute on the MCP
	// browser-flow client: how long an unused MCP login (an offline
	// refresh token) stays valid. Zero means the default, 48 hours.
	// Without this attribute the realm's Offline Session Idle applies,
	// which is 30 days on a default Keycloak realm.
	MCPOfflineSessionIdleSeconds int

	// DryRun true → print every API call that would be made and exit
	// 0 without mutating anything. Mirrors the reference shell
	// script's `--dry-run`.
	DryRun bool

	// Out / Err are where structured progress lines go. nil → stdout
	// / stderr.
	Out io.Writer
	Err io.Writer
}

// defaultMCPOfflineSessionIdleSeconds is the offline-session idle
// timeout (48h) set on the MCP browser-flow client unless the operator
// picks another value with --mcp-offline-session-idle.
const defaultMCPOfflineSessionIdleSeconds = 172800

// withDefaults applies the recipe defaults so the cobra command can
// pass a sparsely-populated BootstrapOptions and still get the right
// shape. Returns a copy; the input is not mutated.
func (o BootstrapOptions) withDefaults() BootstrapOptions {
	if o.CLIClientID == "" {
		o.CLIClientID = "meho-cli"
	}
	if o.MCPClientID == "" {
		o.MCPClientID = "meho-mcp"
	}
	if o.BackplaneAudience == "" {
		o.BackplaneAudience = "meho-backplane"
	}
	if o.TenantID == "" {
		o.TenantID = "00000000-0000-0000-0000-000000000000"
	}
	if o.MCPOfflineSessionIdleSeconds == 0 {
		o.MCPOfflineSessionIdleSeconds = defaultMCPOfflineSessionIdleSeconds
	}
	if len(o.MCPRedirectURIs) == 0 {
		o.MCPRedirectURIs = []string{
			"http://localhost:*",
			"http://127.0.0.1:*",
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
	if o.CLIOfflineSessionIdleSeconds < 0 {
		return errors.New(
			"--cli-offline-access seconds must be a positive number of " +
				"seconds (omit the flag to leave the CLI client " +
				"offline-token-free)")
	}
	if o.MCPOfflineSessionIdleSeconds < 0 {
		return errors.New(
			"--mcp-offline-session-idle must be a positive number of " +
				"seconds (omit the flag to use the 48h default)")
	}
	return nil
}

// roleGroup is one of the five MEHO role groups. A person is in exactly
// one of them. The group's attributes give the person their tenant and
// role: the User Attribute mappers below read them into the token.
type roleGroup struct {
	Name       string
	TenantRole string
	Approver   bool
}

// adminGroupName is the role group the first admin user joins.
const adminGroupName = "MEHO-Admins"

// roleGroups are the five MEHO role groups, see
// docs/decisions/login-role-groups.md.
var roleGroups = []roleGroup{
	{Name: "MEHO-Readers", TenantRole: "read_only"},
	{Name: "MEHO-Operators", TenantRole: "operator"},
	{Name: "MEHO-SA-Operators", TenantRole: "operator"},
	{Name: adminGroupName, TenantRole: "tenant_admin"},
	{Name: "MEHO-Approvers", TenantRole: "read_only", Approver: true},
}

// roleGroupAttributeKeys are the group attributes this command owns.
// It sets them on every run and leaves every other attribute alone.
var roleGroupAttributeKeys = []string{"tenant_id", "tenant_role", "approver"}

// desiredAttributes returns the attributes the command owns on g.
// `approver` is set only on MEHO-Approvers; on the other groups it must
// be absent.
func (g roleGroup) desiredAttributes(tenantID string) map[string][]string {
	attrs := map[string][]string{
		"tenant_id":   {tenantID},
		"tenant_role": {g.TenantRole},
	}
	if g.Approver {
		attrs["approver"] = []string{"true"}
	}
	return attrs
}

// userAttributeMapper builds a stock "User Attribute" mapper that copies
// the attribute `name` into a claim of the same name. When the person
// has no value of their own, Keycloak takes the value from the person's
// groups (KeycloakModelUtils.resolveAttribute). aggregate.attrs=false:
// the person's own value wins, else the first group that has one.
func userAttributeMapper(mapperName, name, jsonType string) protocolMapperRep {
	return protocolMapperRep{
		Name:           mapperName,
		Protocol:       "openid-connect",
		ProtocolMapper: "oidc-usermodel-attribute-mapper",
		Config: map[string]string{
			"user.attribute":            name,
			"claim.name":                name,
			"jsonType.label":            jsonType,
			"multivalued":               "false",
			"aggregate.attrs":           "false",
			"id.token.claim":            "false",
			"access.token.claim":        "true",
			"userinfo.token.claim":      "true",
			"introspection.token.claim": "true",
		},
	}
}

// desiredMappers returns the six protocol mappers the recipe pins on
// both public clients. Mappers are matched by name, so a re-run updates
// them in place. The names `tenant-id` and `tenant-role` are kept from
// the older fixed-value mappers: a run on an older realm turns those
// two into User Attribute mappers, so no fixed value is left behind.
//
// Every mapper spells out all four token toggles. Keycloak's GET shows
// an unset userinfo / introspection toggle with its default filled in,
// so a mapper that leaves one out never matches and a re-run would
// update it every time.
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
				"userinfo.token.claim":      "false",
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
				"userinfo.token.claim":      "false",
			},
		},
		userAttributeMapper("tenant-id", "tenant_id", "String"),
		userAttributeMapper("tenant-role", "tenant_role", "String"),
		userAttributeMapper("approver", "approver", "boolean"),
		{
			Name:           "groups-claim",
			Protocol:       "openid-connect",
			ProtocolMapper: "oidc-group-membership-mapper",
			Config: map[string]string{
				"claim.name":                "groups",
				"full.path":                 "false",
				"id.token.claim":            "true",
				"access.token.claim":        "true",
				"userinfo.token.claim":      "false",
				"introspection.token.claim": "true",
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

// mcpClientScopes returns the realm-level client scopes MEHO's own MCP
// clients ask for: `mcp:read` and `mcp:execute` (the default request of
// the Claude Code plugin and the Claude Desktop bundle) and `mcp:admin`
// (the explicit opt-in to the operator tools). Keycloak does not ship
// these, and it refuses a login that asks for a scope the client does
// not offer (`invalid_scope`), so the bootstrap creates them and offers
// them on the MCP client as optional scopes (#4002).
//
// `include.in.token.scope` is set so the scope name lands in the
// token's `scope` claim; the backplane reads `mcp:admin` from there.
// The scopes carry no mappers: the MCP client's own mappers already
// produce every claim the backplane checks.
func mcpClientScopes() []clientScopeRep {
	names := []struct{ name, desc string }{
		{"mcp:read", "MEHO MCP working surface: list and read."},
		{"mcp:execute", "MEHO MCP working surface: run operations."},
		{"mcp:admin", "MEHO MCP operator tools. Only granted when the client asks for it."},
	}
	scopes := make([]clientScopeRep, 0, len(names))
	for _, n := range names {
		scopes = append(scopes, clientScopeRep{
			Name:        n.name,
			Description: n.desc,
			Protocol:    "openid-connect",
			Attributes: map[string]string{
				"include.in.token.scope":    "true",
				"display.on.consent.screen": "false",
			},
		})
	}
	return scopes
}

// requiredMCPOptionalScopes returns the optional client-scopes the
// recipe pins on the MCP browser-flow client: the realm-built-in
// `offline_access` scope plus the three `mcp:*` scopes from
// mcpClientScopes. For `offline_access`, OIDC Core §11 makes
// `offline_access` the request signal for a refresh token, and Claude
// Code's MCP client always requests it during its auth-code + PKCE
// flow. Without the scope assigned (default OR optional), Keycloak
// rejects the authorization request with `invalid_scope` — RDC Wall
// W7.
//
// Optional, not default: only flows that explicitly include
// `offline_access` in their scope parameter should mint a refresh
// token. Listing it as a default would mint refresh tokens into every
// browser-flow login regardless of whether the client asked for one.
// The CLI device-code client (`meho-cli`) gets `offline_access` only
// when a deployment opts in via
// BootstrapOptions.CLIOfflineSessionIdleSeconds (the
// `--cli-offline-access` flag) — off by default, and paired with a
// tight per-client offline-session idle timeout when on (#2902). A
// stolen device-code refresh token has worse blast-radius than a fresh
// dance prompt, so the offline path stays opt-in and bounded rather
// than the always-on default; see requiredCLIOptionalScopes and
// desiredCLIClient.
func requiredMCPOptionalScopes() []string {
	scopes := []string{"offline_access"}
	for _, s := range mcpClientScopes() {
		scopes = append(scopes, s.Name)
	}
	return scopes
}

// requiredCLIOptionalScopes returns the optional client-scopes pinned
// on the device-code CLI client. Empty unless the deployment opted
// into the offline-token path (CLIOfflineSessionIdleSeconds > 0), in
// which case it is the realm-built-in `offline_access` scope — the
// same opt-in shape #912 gave the MCP client, but off by default for
// the CLI and paired with a bounded offline-session idle timeout (see
// desiredCLIClient). Optional, never default: a refresh token is
// minted only when `meho login --offline` explicitly requests it.
func (o BootstrapOptions) requiredCLIOptionalScopes() []string {
	if o.CLIOfflineSessionIdleSeconds > 0 {
		return []string{"offline_access"}
	}
	return nil
}

// boolPtr / strSlice are tiny helpers to make the desired-client
// builders readable.
func boolPtr(b bool) *bool { return &b }

// desiredCLIClient builds the ClientRepresentation for the public
// device-code client. Mirrors the reference shell script verbatim
// (including the "Description" string pattern so re-runs against a
// shell-script-created client don't churn the field).
func (o BootstrapOptions) desiredCLIClient() *clientRep {
	c := &clientRep{
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
	// Opt-in offline-token path (#2902): assign offline_access as an
	// OPTIONAL scope on the CLI client and bound the per-client
	// offline-session idle timeout. Off by default — see
	// requiredCLIOptionalScopes. The actual scope assignment is done by
	// reconcileOptionalScopes in Bootstrap; setting the field here keeps
	// the representation consistent with a subsequent GET/PUT.
	if scopes := o.requiredCLIOptionalScopes(); len(scopes) > 0 {
		c.OptionalClientScopes = scopes
		// Keycloak attribute key OIDCConfigAttributes.
		// CLIENT_OFFLINE_SESSION_IDLE_TIMEOUT. Overrides the realm
		// offlineSessionIdleTimeout downward for this client only; the
		// realm value stays the upper bound.
		c.Attributes["client.offline.session.idle.timeout"] =
			strconv.Itoa(o.CLIOfflineSessionIdleSeconds)
	}
	return c
}

// desiredMCPClient builds the ClientRepresentation for the public
// authorization-code+PKCE client used by browser MCP clients. The
// 6 mappers + 4 default scopes apply identically — only the flow
// flags + redirect URIs / web origins differ from the CLI client.
func (o BootstrapOptions) desiredMCPClient() *clientRep {
	return &clientRep{
		ClientID:                  o.MCPClientID,
		Name:                      "MEHO MCP browser-OAuth-2.1 public client",
		Description:               "Public OAuth client for OAuth 2.1 authorization-code + PKCE used by browser-flow MCP clients (mcp-remote shim, MCP Inspector, Claude Code) targeting <backplane-url>/mcp. Provisioned by `meho admin keycloak bootstrap-clients` (#791).",
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
			// Bounds how long an unused MCP login lasts. Without it the
			// realm's Offline Session Idle (30 days by default) applies.
			"client.offline.session.idle.timeout": strconv.Itoa(o.MCPOfflineSessionIdleSeconds),
		},
		RedirectURIs:         append([]string{}, o.MCPRedirectURIs...),
		WebOrigins:           append([]string{}, o.MCPWebOrigins...),
		DefaultClientScopes:  append([]string{"openid", "profile", "email"}, requiredDefaultScopes()...),
		OptionalClientScopes: append([]string{}, requiredMCPOptionalScopes()...),
	}
}

// Result captures the IDs of the resources the bootstrap touched.
// Used by the cobra command to print the "set these in your chart"
// summary, and by tests for assertions.
type Result struct {
	CLIClientID      string // clientId (not UUID) of the device-code client
	CLIClientUUID    string // internal UUID, useful for downstream debugging
	MCPClientID      string // clientId of the MCP browser-flow client
	MCPClientUUID    string
	AdminGroupUUID   string // UUID of MEHO-Admins
	AdminUserCreated bool
	AdminUserJoined  bool // this run added the user to MEHO-Admins
	AdminUserUUID    string
	MapperEventsLog  []string // human-readable bullet list (one line per client / mapper / scope / group event)
	ConfigKeysToSet  []string // chart values + env vars the operator must set
}

// Bootstrap is the public entry point. It mints an admin token, then
// idempotently reconciles the realm against the recipe shape:
//
//  1. Public CLI device-code client (`meho-cli`)
//  2. 6 protocol mappers: two audiences, the groups claim, and three
//     User Attribute mappers (tenant_id, tenant_role, approver)
//  3. 4 default client scopes (basic / roles / web-origins / acr)
//  4. Public MCP browser-flow client (`meho-mcp`)
//  5. Same 6 mappers + 4 default scopes on the MCP client, **plus**
//     `offline_access`, `mcp:read`, `mcp:execute` and `mcp:admin` as
//     *optional* client scopes (the three `mcp:*` scopes are created in
//     the realm first if missing). The CLI
//     device-code client gets the same optional scope only when a
//     deployment opts in via --cli-offline-access (off by default,
//     #2902); see RDC Wall W7 in deploy/values-examples/README.md.
//     The MCP client also gets `client.offline.session.idle.timeout`
//     (48h unless --mcp-offline-session-idle says otherwise).
//  6. The five MEHO role groups with their attributes (roleGroups)
//  7. Admin user, set password, join MEHO-Admins (skipped with
//     SkipUserProvisioning)
//
// The operator-facing progress output (one line per step + a final
// summary block) goes to opts.Out / opts.Err.
//
// Idempotency: every step does a "does this exist?" check before
// mutating, so re-runs print "[skip] …" rather than re-creating.
// Drift is corrected (PUT-to-update) on the client, mapper and group
// level but **not** on the user's password — re-running with a different
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
	// Opt-in only (#2902): the CLI client gets offline_access as an
	// optional scope solely when a deployment set --cli-offline-access.
	// When off (the default) this call is skipped entirely, so the
	// default provisioning path makes no extra Keycloak API calls.
	if cliOptional := opts.requiredCLIOptionalScopes(); len(cliOptional) > 0 {
		cliOptionalEvents, oerr := reconcileOptionalScopes(
			ctx, c, cliUUID, cliOptional)
		if oerr != nil {
			return nil, fmt.Errorf("reconcile CLI optional scopes: %w", oerr)
		}
		res.MapperEventsLog = append(res.MapperEventsLog, cliOptionalEvents...)
	}

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
	realmScopeEvents, err := reconcileRealmClientScopes(
		ctx, c, mcpClientScopes())
	if err != nil {
		return nil, fmt.Errorf("reconcile MCP client scopes: %w", err)
	}
	res.MapperEventsLog = append(res.MapperEventsLog, realmScopeEvents...)
	mcpOptionalEvents, err := reconcileOptionalScopes(
		ctx, c, mcpUUID, requiredMCPOptionalScopes())
	if err != nil {
		return nil, fmt.Errorf("reconcile MCP optional scopes: %w", err)
	}
	res.MapperEventsLog = append(res.MapperEventsLog, mcpOptionalEvents...)

	// --- Role groups + first admin user ------------------------------------

	groupUUIDs, groupEvents, err := reconcileRoleGroups(ctx, c, opts.TenantID)
	if err != nil {
		return nil, err
	}
	res.MapperEventsLog = append(res.MapperEventsLog, groupEvents...)
	res.AdminGroupUUID = groupUUIDs[adminGroupName]

	if !opts.SkipUserProvisioning {
		userCreated, userJoined, userUUID, uerr := reconcileUser(
			ctx, c, &userRep{
				Username:      opts.AdminUserUsername,
				Email:         opts.AdminUserEmail,
				Enabled:       boolPtr(true),
				EmailVerified: boolPtr(true),
			}, opts.AdminUserPassword, res.AdminGroupUUID,
		)
		if uerr != nil {
			return nil, fmt.Errorf(
				"reconcile user %q: %w", opts.AdminUserUsername, uerr)
		}
		res.AdminUserCreated = userCreated
		res.AdminUserJoined = userJoined
		res.AdminUserUUID = userUUID
	} else {
		fmt.Fprintln(out, "==> --skip-user-provisioning: no admin user "+
			"created; put people in the five groups yourself")
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
	fmt.Fprintln(out, "==> role groups (top-level, with attributes):")
	for _, g := range roleGroups {
		fmt.Fprintf(out, "    - %s: %s\n",
			g.Name, formatAttributes(g.desiredAttributes(opts.TenantID)))
	}
	fmt.Fprintln(out, "==> default client scopes (each on both clients):")
	for _, s := range requiredDefaultScopes() {
		fmt.Fprintf(out, "    - %s\n", s)
	}
	fmt.Fprintln(out, "==> realm client scopes (created if missing):")
	for _, s := range mcpClientScopes() {
		fmt.Fprintf(out, "    - %s\n", s.Name)
	}
	fmt.Fprintln(out, "==> optional client scopes on the MCP browser-flow "+
		"client (MEHO's MCP clients ask for these at login):")
	for _, s := range requiredMCPOptionalScopes() {
		fmt.Fprintf(out, "    - %s\n", s)
	}
	fmt.Fprintf(out, "==> MCP browser-flow client: offline session idle "+
		"(client.offline.session.idle.timeout) = %ds\n",
		opts.MCPOfflineSessionIdleSeconds)
	if cliOptional := opts.requiredCLIOptionalScopes(); len(cliOptional) > 0 {
		fmt.Fprintf(out, "==> optional client scopes on the CLI device-code "+
			"client (--cli-offline-access opt-in; offline idle bound %ds):\n",
			opts.CLIOfflineSessionIdleSeconds)
		for _, s := range cliOptional {
			fmt.Fprintf(out, "    - %s\n", s)
		}
	} else {
		fmt.Fprintln(out, "==> CLI device-code client: offline_access not "+
			"assigned (opt in with --cli-offline-access)")
	}
	if !opts.SkipUserProvisioning {
		fmt.Fprintf(out, "==> admin user: %s (member of %s)\n",
			opts.AdminUserUsername, adminGroupName)
	} else {
		fmt.Fprintln(out, "==> admin user skipped")
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
		// Keycloak's PUT replaces the mapper type and config in place.
		// On an older realm this turns the fixed-value tenant-id /
		// tenant-role mappers into User Attribute mappers.
		if got.ProtocolMapper != want.ProtocolMapper {
			events = append(events,
				fmt.Sprintf("[updated] mapper %s (replaced %s with %s)",
					want.Name, got.ProtocolMapper, want.ProtocolMapper))
			continue
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

// reconcileRealmClientScopes creates every desired realm-level client
// scope that does not exist yet, matched by name. An existing scope is
// left as it is: an admin may have changed it on purpose.
func reconcileRealmClientScopes(
	ctx context.Context, c *adminClient, desired []clientScopeRep,
) ([]string, error) {
	allScopes, err := c.listRealmClientScopes(ctx)
	if err != nil {
		return nil, err
	}
	exists := make(map[string]bool, len(allScopes))
	for _, s := range allScopes {
		exists[s.Name] = true
	}
	var events []string
	for _, want := range desired {
		if exists[want.Name] {
			events = append(events,
				fmt.Sprintf("[skip]    client scope %s (already in realm)",
					want.Name))
			continue
		}
		if err := c.createClientScope(ctx, want); err != nil {
			return nil, fmt.Errorf(
				"create client scope %s: %w", want.Name, err)
		}
		events = append(events,
			fmt.Sprintf("[created] client scope %s", want.Name))
	}
	return events, nil
}

// reconcileOptionalScopes resolves each desired-scope name to its
// realm-level UUID, then PUTs every one that isn't already an
// optional scope on the client. Mirrors reconcileDefaultScopes for
// the parallel `/optional-client-scopes` endpoint — the only
// difference is the endpoint path baked into the adminClient calls.
//
// Used to assign `offline_access` and the `mcp:*` scopes to the MCP
// browser-flow client; see requiredMCPOptionalScopes.
func reconcileOptionalScopes(
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
	existing, err := c.listClientOptionalScopes(ctx, clientUUID)
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
				fmt.Sprintf("[WARN]    optional scope %s not in realm — skipping",
					name))
			continue
		}
		if already[id] {
			events = append(events,
				fmt.Sprintf("[skip]    optional scope %s (already optional)",
					name))
			continue
		}
		if err := c.addClientOptionalScope(
			ctx, clientUUID, id); err != nil {
			return nil, fmt.Errorf(
				"add optional scope %s: %w", name, err)
		}
		events = append(events,
			fmt.Sprintf("[added]   optional scope %s", name))
	}
	return events, nil
}

// reconcileRoleGroups makes sure the five MEHO role groups exist as
// top-level groups with the attributes the command owns
// (roleGroupAttributeKeys). A missing group is created; a group whose
// owned attributes differ is updated. Other attributes on the group are
// kept. Returns the group UUIDs by name plus one event line per group.
func reconcileRoleGroups(
	ctx context.Context, c *adminClient, tenantID string,
) (map[string]string, []string, error) {
	uuids := make(map[string]string, len(roleGroups))
	var events []string
	for _, g := range roleGroups {
		want := g.desiredAttributes(tenantID)
		found, err := c.findGroup(ctx, g.Name)
		if err != nil {
			return nil, nil, fmt.Errorf("find group %s: %w", g.Name, err)
		}
		if found == nil {
			uuid, cerr := c.createGroup(ctx, groupRep{Name: g.Name, Attributes: want})
			if cerr != nil {
				return nil, nil, fmt.Errorf("create group %s: %w", g.Name, cerr)
			}
			uuids[g.Name] = uuid
			events = append(events, fmt.Sprintf("[created] group %s (%s)",
				g.Name, formatAttributes(want)))
			continue
		}
		uuids[g.Name] = found.ID
		// The search answer has no attributes; read the full group.
		existing, err := c.getGroup(ctx, found.ID)
		if err != nil {
			return nil, nil, fmt.Errorf("read group %s: %w", g.Name, err)
		}
		merged, changed := mergeOwnedAttributes(existing.Attributes, want)
		if !changed {
			events = append(events, fmt.Sprintf(
				"[skip]    group %s (already matches)", g.Name))
			continue
		}
		if err := c.updateGroup(ctx, groupRep{
			ID: found.ID, Name: g.Name,
			Description: existing.Description, Attributes: merged,
		}); err != nil {
			return nil, nil, fmt.Errorf("update group %s: %w", g.Name, err)
		}
		events = append(events, fmt.Sprintf("[updated] group %s (%s)",
			g.Name, formatAttributes(want)))
	}
	return uuids, events, nil
}

// mergeOwnedAttributes returns existing with the owned keys set to want:
// an owned key in want is set, an owned key not in want is removed, and
// every other key is kept. changed reports whether anything differs.
func mergeOwnedAttributes(
	existing, want map[string][]string,
) (map[string][]string, bool) {
	merged := make(map[string][]string, len(existing)+len(want))
	for k, v := range existing {
		merged[k] = v
	}
	changed := false
	for _, k := range roleGroupAttributeKeys {
		wantVal, keep := want[k]
		gotVal, has := existing[k]
		switch {
		case keep && !slices.Equal(gotVal, wantVal):
			merged[k] = wantVal
			changed = true
		case !keep && has:
			delete(merged, k)
			changed = true
		}
	}
	return merged, changed
}

// formatAttributes renders the owned attributes in a fixed order for
// progress lines, e.g. "tenant_id=<uuid> tenant_role=operator".
func formatAttributes(attrs map[string][]string) string {
	parts := make([]string, 0, len(roleGroupAttributeKeys))
	for _, k := range roleGroupAttributeKeys {
		if v, ok := attrs[k]; ok {
			parts = append(parts, k+"="+strings.Join(v, ","))
		}
	}
	return strings.Join(parts, " ")
}

// reconcileUser ensures a user with the given username exists and
// belongs to the group groupUUID (MEHO-Admins). If the user already exists the
// password is **not** reset (silent password rotation on a re-run is
// strictly worse than the "set it once at create time" rule).
//
// Returns (created, joined, uuid): joined is true when this run added
// the user to the group.
func reconcileUser(
	ctx context.Context, c *adminClient,
	desired *userRep, password, groupUUID string,
) (bool, bool, string, error) {
	existing, err := c.findUserByUsername(ctx, desired.Username)
	if err != nil {
		return false, false, "", err
	}
	var (
		userUUID string
		created  bool
	)
	if existing == nil {
		uuid, cerr := c.createUser(ctx, desired)
		if cerr != nil {
			return false, false, "", cerr
		}
		if err := c.resetUserPassword(ctx, uuid, password); err != nil {
			return false, false, "", fmt.Errorf(
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
		return created, false, userUUID, err
	}
	for _, g := range memberships {
		if g.ID == groupUUID {
			return created, false, userUUID, nil
		}
	}
	if err := c.joinUserToGroup(ctx, userUUID, groupUUID); err != nil {
		return created, false, userUUID, err
	}
	return created, true, userUUID, nil
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
		"MCP browser-flow client_id (paste into your MCP client config — mcp-remote shim / MCP Inspector / Claude Code): " + opts.MCPClientID,
	}
}
