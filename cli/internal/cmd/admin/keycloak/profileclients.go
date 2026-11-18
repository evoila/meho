// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"fmt"
	"io"
	"regexp"
)

// A profile client is a login client made for one MCP tool profile. It
// has the settings of the MCP browser-flow client, plus one fixed claim
// mapper that writes `mcp_profile: <name>` into every token of the
// client. The mapper sits on the client, not in a scope, so a session
// cannot ask for a token without the claim, also not with fewer scopes
// or a renewed token. MEHO takes such a token only at
// `/mcp?profile=<name>` (backend: auth/mcp_profile_claim.py).

// profileClientPrefix starts the clientId of every profile client.
const profileClientPrefix = "meho-mcp-profile-"

// profileClaimMapperName is the name of the fixed claim mapper.
const profileClaimMapperName = "mcp-profile"

// profileNamePattern is the rule for a profile name, the same as on the
// backplane: lower-case letters, digits and dashes, at most 63 characters.
var profileNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// profileClientID returns the clientId of the client of profile name.
func profileClientID(name string) string { return profileClientPrefix + name }

// validateProfiles refuses a profile name the backplane would refuse, and
// a name given twice.
func (o BootstrapOptions) validateProfiles() error {
	seen := map[string]bool{}
	for _, name := range o.MCPProfiles {
		if !profileNamePattern.MatchString(name) {
			return fmt.Errorf(
				"--mcp-profile %q: use lower-case letters, digits and dashes "+
					"(at most 63 characters)", name)
		}
		if seen[name] {
			return fmt.Errorf("--mcp-profile %q is given twice", name)
		}
		seen[name] = true
		id := profileClientID(name)
		if id == o.CLIClientID || id == o.MCPClientID || id == o.CLIApproveClientID {
			return fmt.Errorf(
				"refusing client name %s: it is the client of the %s tool profile", id, name)
		}
	}
	return nil
}

// profileClaimMapper is the fixed claim mapper of the client of name.
// It writes the claim into the access token and the introspection
// answer only.
func profileClaimMapper(name string) protocolMapperRep {
	return protocolMapperRep{
		Name:           profileClaimMapperName,
		Protocol:       "openid-connect",
		ProtocolMapper: "oidc-hardcoded-claim-mapper",
		Config: map[string]string{
			"claim.name":                "mcp_profile",
			"claim.value":               name,
			"jsonType.label":            "String",
			"id.token.claim":            "false",
			"access.token.claim":        "true",
			"userinfo.token.claim":      "false",
			"introspection.token.claim": "true",
		},
	}
}

// desiredProfileMCPClient builds the client of profile name: a copy of
// desiredMCPClient with its own id, name and description.
func (o BootstrapOptions) desiredProfileMCPClient(name string) *clientRep {
	c := o.desiredMCPClient()
	c.ClientID = profileClientID(name)
	c.Name = "MEHO MCP for the " + name + " tool profile"
	c.Description = "Public OAuth client for one MCP tool profile (authorization code + PKCE). " +
		"Every token carries mcp_profile=" + name + "; MEHO takes it only at " +
		"/mcp?profile=" + name + ". Provisioned by `meho admin keycloak bootstrap-clients`."
	return c
}

// reconcileProfileMCPClient sets up the client of profile name like the
// MCP browser-flow client, plus the fixed claim mapper.
func reconcileProfileMCPClient(
	ctx context.Context, c *adminClient, opts BootstrapOptions,
	name string, createdScopes []string,
) ([]string, error) {
	desired := opts.desiredProfileMCPClient(name)
	uuid, events, err := reconcileClient(ctx, c, desired)
	if err != nil {
		return nil, err
	}
	mappers := append(opts.desiredMappers(), profileClaimMapper(name))
	mapperEvents, err := reconcileMappers(ctx, c, uuid, mappers)
	if err != nil {
		return nil, fmt.Errorf("mappers: %w", err)
	}
	events = append(events, mapperEvents...)
	scopeEvents, err := reconcileDefaultScopes(ctx, c, uuid, requiredDefaultScopes())
	if err != nil {
		return nil, fmt.Errorf("default scopes: %w", err)
	}
	events = append(events, scopeEvents...)
	optionalEvents, err := reconcileOptionalScopes(
		ctx, c, uuid, requiredMCPOptionalScopes(), createdScopes)
	if err != nil {
		return nil, fmt.Errorf("optional scopes: %w", err)
	}
	return append(events, optionalEvents...), nil
}

// reconcileProfileMCPClients sets up the client of each --mcp-profile.
func reconcileProfileMCPClients(
	ctx context.Context, c *adminClient, opts BootstrapOptions,
	createdScopes []string, res *Result,
) error {
	for _, name := range opts.MCPProfiles {
		events, err := reconcileProfileMCPClient(ctx, c, opts, name, createdScopes)
		if err != nil {
			return fmt.Errorf("reconcile MCP client %q: %w", profileClientID(name), err)
		}
		res.ProfileMCPClientIDs = append(res.ProfileMCPClientIDs, profileClientID(name))
		res.MapperEventsLog = append(res.MapperEventsLog, events...)
	}
	return nil
}

// dryRunProfileSummary names the profile clients a run sets up.
func dryRunProfileSummary(opts BootstrapOptions, out io.Writer) {
	for _, name := range opts.MCPProfiles {
		fmt.Fprintf(out, "==> MCP client of the %s tool profile: %s (same settings as %s, "+
			"plus the fixed claim mcp_profile=%s)\n",
			name, profileClientID(name), opts.MCPClientID, name)
	}
}

// profileConfigKeySummary tells the operator how a profile client is used.
func profileConfigKeySummary(opts BootstrapOptions) []string {
	if len(opts.MCPProfiles) == 0 {
		return nil
	}
	lines := []string{""}
	for _, name := range opts.MCPProfiles {
		lines = append(lines, fmt.Sprintf(
			"Client of the %s tool profile: %s. Its tokens work only at <backplane-url>/mcp?profile=%s.",
			name, profileClientID(name), name))
	}
	return append(lines,
		"# A profile with a connector list must also be in MCP_CONNECTOR_PROFILES,",
		"# with MCP_PROFILE_CONNECTOR_LISTS=true; else MEHO refuses its tokens.")
}
