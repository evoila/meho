// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"reflect"
	"slices"
	"strings"
	"testing"
)

// clientByID returns the client stored in the fake under clientId, and
// its UUID, or fails.
func clientByID(t *testing.T, fake *fakeKeycloak, clientID string) (string, *clientRep) {
	t.Helper()
	for uuid, c := range fake.clients {
		if c.ClientID == clientID {
			return uuid, c
		}
	}
	t.Fatalf("client %s not found", clientID)
	return "", nil
}

// sameExceptNames returns a copy of c without the id, the name, the
// description and the UUID, so two clients can be compared by settings.
func sameExceptNames(c *clientRep) clientRep {
	cp := *c
	cp.ID, cp.ClientID, cp.Name, cp.Description = "", "", "", ""
	return cp
}

// The clients of Claude Code and Claude Desktop have the settings,
// mappers and scopes of meho-mcp; only the id, the name and the
// description differ.
func TestBootstrap_ToolClientsCopyTheMCPClient(t *testing.T) {
	fake := newFakeKeycloak()
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	want := []string{"meho-mcp-code", "meho-mcp-desktop"}
	if !slices.Equal(res.ToolMCPClientIDs, want) {
		t.Errorf("ToolMCPClientIDs = %v, want %v", res.ToolMCPClientIDs, want)
	}
	mcpUUID, mcp := clientByID(t, fake, "meho-mcp")
	for _, id := range want {
		uuid, c := clientByID(t, fake, id)
		if c.PublicClient == nil || !*c.PublicClient {
			t.Errorf("%s must be public", id)
		}
		if got := c.Attributes["pkce.code.challenge.method"]; got != "S256" {
			t.Errorf("%s PKCE method = %q, want S256", id, got)
		}
		if c.Name == mcp.Name || c.Name == "" {
			t.Errorf("%s name = %q, want a name of its own", id, c.Name)
		}
		if !reflect.DeepEqual(sameExceptNames(c), sameExceptNames(mcp)) {
			t.Errorf("%s settings differ from meho-mcp:\n got %+v\nwant %+v",
				id, sameExceptNames(c), sameExceptNames(mcp))
		}
		if !reflect.DeepEqual(mapperNames(fake.mappers[uuid]), mapperNames(fake.mappers[mcpUUID])) {
			t.Errorf("%s mappers = %v, want those of meho-mcp %v", id,
				mapperNames(fake.mappers[uuid]), mapperNames(fake.mappers[mcpUUID]))
		}
		if !reflect.DeepEqual(sorted(fake.defaultScopes[uuid]), sorted(fake.defaultScopes[mcpUUID])) {
			t.Errorf("%s default scopes = %v, want those of meho-mcp", id, fake.defaultScopes[uuid])
		}
		if !reflect.DeepEqual(sorted(fake.optionalScopes[uuid]), sorted(fake.optionalScopes[mcpUUID])) {
			t.Errorf("%s optional scopes = %v, want those of meho-mcp %v", id,
				fake.optionalScopes[uuid], fake.optionalScopes[mcpUUID])
		}
		for _, m := range fake.mappers[uuid] {
			if m.ProtocolMapper == "oidc-hardcoded-claim-mapper" {
				t.Errorf("%s carries a fixed claim mapper %s", id, m.Name)
			}
		}
	}
}

// A second run changes nothing on the two clients.
func TestBootstrap_ToolClientsReRunIsNoOp(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("first Bootstrap: %v", err)
	}
	posts := fake.calls["POST /clients"]
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if fake.calls["POST /clients"] != posts {
		t.Errorf("re-run created clients: %d -> %d", posts, fake.calls["POST /clients"])
	}
	for _, id := range []string{"meho-mcp-code", "meho-mcp-desktop"} {
		want := "[skip]    client " + id + " (already matches)"
		if !slices.Contains(res.MapperEventsLog, want) {
			t.Errorf("re-run events miss %q", want)
		}
	}
}

// The dry run and the end summary name the two clients and the switch.
func TestBootstrap_ToolClientsAreNamedInTheOutput(t *testing.T) {
	opts := fullOpts()
	opts.DryRun = true
	_, stdout, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	for _, id := range []string{"meho-mcp-code", "meho-mcp-desktop"} {
		if !strings.Contains(stdout, id) {
			t.Errorf("dry run output does not name %s:\n%s", id, stdout)
		}
	}
	summary := strings.Join(buildConfigKeySummary(fullOpts().withDefaults()), "\n")
	if !strings.Contains(summary, "MCP_CLIENT_PER_TOOL_KIND=true") {
		t.Errorf("summary does not name the switch:\n%s", summary)
	}
}

// Another client may not take the name of a tool client: the run would
// then set up one client twice with two shapes.
func TestBootstrap_RefusesAToolClientName(t *testing.T) {
	for _, set := range []func(*BootstrapOptions){
		func(o *BootstrapOptions) { o.MCPClientID = "meho-mcp-code" },
		func(o *BootstrapOptions) { o.CLIClientID = "meho-mcp-desktop" },
		func(o *BootstrapOptions) { o.CLIApproveClientID = "meho-mcp-code" },
	} {
		opts := fullOpts()
		set(&opts)
		_, _, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
		if err == nil || !strings.Contains(err.Error(), "refusing client name") {
			t.Errorf("want a refusal, got %v", err)
		}
	}
}

func mapperNames(ms []protocolMapperRep) []string {
	names := make([]string, 0, len(ms))
	for _, m := range ms {
		names = append(names, m.Name)
	}
	slices.Sort(names)
	return names
}

func sorted(s []string) []string {
	cp := slices.Clone(s)
	slices.Sort(cp)
	return cp
}
