// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"strings"
	"testing"
)

const offlineIdleAttr = "client.offline.session.idle.timeout"

// mcpClient returns the meho-mcp client stored in the fake, or fails.
func mcpClient(t *testing.T, fake *fakeKeycloak) *clientRep {
	t.Helper()
	for _, c := range fake.clients {
		if c.ClientID == "meho-mcp" {
			return c
		}
	}
	t.Fatalf("meho-mcp client not found")
	return nil
}

// A fresh realm gets the 48h offline idle limit on meho-mcp, so an MCP
// login does not fall back to the realm's 30-day default.
func TestBootstrap_FreshRunSetsMCPOfflineIdle(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := mcpClient(t, fake).Attributes[offlineIdleAttr]; got != "172800" {
		t.Errorf("meho-mcp %s = %q, want 172800", offlineIdleAttr, got)
	}
}

// An existing meho-mcp client made before this setting existed gets
// the attribute on the next run, through the normal update path.
func TestBootstrap_ReRunAddsMCPOfflineIdleToExistingClient(t *testing.T) {
	fake := newFakeKeycloak()
	old := fullOpts().withDefaults().desiredMCPClient()
	delete(old.Attributes, offlineIdleAttr)
	old.ID = "uuid-existing-mcp"
	fake.clients[old.ID] = old

	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := fake.clients[old.ID].Attributes[offlineIdleAttr]; got != "172800" {
		t.Errorf("existing meho-mcp %s = %q, want 172800", offlineIdleAttr, got)
	}
	if n := fake.calls["POST /clients"]; n != 2 {
		t.Errorf("POST /clients called %d times, want 2 (only meho-cli and meho-cli-approve are new)", n)
	}
}

// A run where the attribute is already set leaves it as it is and
// creates nothing new.
func TestBootstrap_ReRunKeepsMCPOfflineIdle(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("first Bootstrap: %v", err)
	}
	posts := fake.calls["POST /clients"]
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if got := mcpClient(t, fake).Attributes[offlineIdleAttr]; got != "172800" {
		t.Errorf("meho-mcp %s = %q after re-run, want 172800", offlineIdleAttr, got)
	}
	if fake.calls["POST /clients"] != posts || len(fake.clients) != 3 {
		t.Errorf("re-run created clients: posts %d -> %d, clients=%d",
			posts, fake.calls["POST /clients"], len(fake.clients))
	}
}

func TestBootstrap_CustomMCPOfflineIdle(t *testing.T) {
	fake := newFakeKeycloak()
	opts := fullOpts()
	opts.MCPOfflineSessionIdleSeconds = 3600
	if _, _, err := runBootstrapOnce(t, fake, opts); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := mcpClient(t, fake).Attributes[offlineIdleAttr]; got != "3600" {
		t.Errorf("meho-mcp %s = %q, want 3600", offlineIdleAttr, got)
	}
}

func TestBootstrap_DryRunShowsMCPOfflineIdle(t *testing.T) {
	opts := fullOpts()
	opts.DryRun = true
	_, stdout, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err != nil {
		t.Fatalf("Bootstrap dry-run: %v", err)
	}
	if !strings.Contains(stdout, offlineIdleAttr+") = 172800s") {
		t.Errorf("dry-run stdout does not show the MCP offline idle; got:\n%s", stdout)
	}
}

func TestBootstrap_RejectsNegativeMCPOfflineIdle(t *testing.T) {
	opts := fullOpts()
	opts.MCPOfflineSessionIdleSeconds = -1
	_, _, err := runBootstrapOnce(t, newFakeKeycloak(), opts)
	if err == nil || !strings.Contains(err.Error(), "--mcp-offline-session-idle") {
		t.Fatalf("error = %v, want mention of --mcp-offline-session-idle", err)
	}
}

func TestBootstrapClientsCmd_MCPOfflineIdleFlagDefault(t *testing.T) {
	f := newBootstrapClientsCmd().Flags().Lookup("mcp-offline-session-idle")
	if f == nil {
		t.Fatalf("--mcp-offline-session-idle flag not registered")
	}
	if f.DefValue != "172800" {
		t.Errorf("--mcp-offline-session-idle default = %q, want 172800", f.DefValue)
	}
}
