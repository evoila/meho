// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"reflect"
	"slices"
	"strings"
	"testing"
)

func profileOpts(names ...string) BootstrapOptions {
	opts := fullOpts()
	opts.MCPProfiles = names
	return opts
}

// A profile client has the settings and scopes of meho-mcp, the same
// mappers plus one fixed claim mapper with the profile name.
func TestBootstrap_ProfileClientCopiesTheMCPClientAndAddsTheClaim(t *testing.T) {
	fake := newFakeKeycloak()
	res, _, err := runBootstrapOnce(t, fake, profileOpts("k8s"))
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if want := []string{"meho-mcp-profile-k8s"}; !slices.Equal(res.ProfileMCPClientIDs, want) {
		t.Errorf("ProfileMCPClientIDs = %v, want %v", res.ProfileMCPClientIDs, want)
	}
	mcpUUID, mcp := clientByID(t, fake, "meho-mcp")
	uuid, c := clientByID(t, fake, "meho-mcp-profile-k8s")
	if !reflect.DeepEqual(sameExceptNames(c), sameExceptNames(mcp)) {
		t.Errorf("settings differ from meho-mcp:\n got %+v\nwant %+v",
			sameExceptNames(c), sameExceptNames(mcp))
	}
	wantMappers := append(mapperNames(fake.mappers[mcpUUID]), profileClaimMapperName)
	slices.Sort(wantMappers)
	if got := mapperNames(fake.mappers[uuid]); !reflect.DeepEqual(got, wantMappers) {
		t.Errorf("mappers = %v, want %v", got, wantMappers)
	}
	if !reflect.DeepEqual(sorted(fake.optionalScopes[uuid]), sorted(fake.optionalScopes[mcpUUID])) {
		t.Errorf("optional scopes = %v, want those of meho-mcp", fake.optionalScopes[uuid])
	}
	var claim *protocolMapperRep
	for i := range fake.mappers[uuid] {
		if fake.mappers[uuid][i].Name == profileClaimMapperName {
			claim = &fake.mappers[uuid][i]
		}
	}
	if claim == nil {
		t.Fatal("no fixed claim mapper")
	}
	if claim.ProtocolMapper != "oidc-hardcoded-claim-mapper" ||
		claim.Config["claim.name"] != "mcp_profile" ||
		claim.Config["claim.value"] != "k8s" ||
		claim.Config["access.token.claim"] != "true" {
		t.Errorf("claim mapper = %+v", claim)
	}
	for _, m := range fake.mappers[mcpUUID] {
		if m.ProtocolMapper == "oidc-hardcoded-claim-mapper" {
			t.Errorf("meho-mcp carries a fixed claim mapper %s", m.Name)
		}
	}
}

// Without --mcp-profile no profile client is made.
func TestBootstrap_NoProfileClientByDefault(t *testing.T) {
	fake := newFakeKeycloak()
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if len(res.ProfileMCPClientIDs) != 0 {
		t.Errorf("ProfileMCPClientIDs = %v, want none", res.ProfileMCPClientIDs)
	}
	for _, c := range fake.clients {
		if strings.HasPrefix(c.ClientID, profileClientPrefix) {
			t.Errorf("unexpected client %s", c.ClientID)
		}
	}
}

// A second run changes nothing on the profile client.
func TestBootstrap_ProfileClientReRunIsNoOp(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, profileOpts("k8s")); err != nil {
		t.Fatalf("first Bootstrap: %v", err)
	}
	posts := fake.calls["POST /clients"]
	res, _, err := runBootstrapOnce(t, fake, profileOpts("k8s"))
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if fake.calls["POST /clients"] != posts {
		t.Errorf("re-run created clients: %d -> %d", posts, fake.calls["POST /clients"])
	}
	want := "[skip]    client meho-mcp-profile-k8s (already matches)"
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("re-run events miss %q", want)
	}
}

// A bad or doubled profile name is refused before any change.
func TestBootstrap_RefusesABadProfileName(t *testing.T) {
	cases := map[string]BootstrapOptions{
		"upper case": profileOpts("K8s"),
		"space":      profileOpts("k 8s"),
		"empty":      profileOpts(""),
		"twice":      profileOpts("k8s", "k8s"),
	}
	clash := profileOpts("x")
	clash.MCPClientID = "meho-mcp-profile-x"
	cases["clash"] = clash
	for name, opts := range cases {
		fake := newFakeKeycloak()
		if _, _, err := runBootstrapOnce(t, fake, opts); err == nil {
			t.Errorf("%s: want an error", name)
		}
		if fake.calls["POST /clients"] != 0 {
			t.Errorf("%s: clients were created", name)
		}
	}
}

// The dry run and the end summary name the profile client and its URL.
func TestBootstrap_ProfileClientIsNamedInTheOutput(t *testing.T) {
	opts := profileOpts("k8s")
	opts.DryRun = true
	_, stdout, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if !strings.Contains(stdout, "meho-mcp-profile-k8s") {
		t.Errorf("dry run output does not name the client:\n%s", stdout)
	}
	summary := strings.Join(buildConfigKeySummary(profileOpts("k8s").withDefaults()), "\n")
	for _, want := range []string{"meho-mcp-profile-k8s", "/mcp?profile=k8s", "MCP_CONNECTOR_PROFILES"} {
		if !strings.Contains(summary, want) {
			t.Errorf("summary does not name %q:\n%s", want, summary)
		}
	}
}
