// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"maps"
	"slices"
	"testing"
)

// A console approval client that a person made is bound to the approval
// sign-in flow, and its other bindings stay (#6282). A second run keeps it.
func TestBootstrap_ConsoleApprovalClientIsBoundToTheApprovalFlow(t *testing.T) {
	fake := newFakeKeycloak()
	client := &clientRep{
		ID: "uuid-console-approve", ClientID: "meho-web-approve",
		RedirectURIs:                       []string{"https://meho.example.test/ui/auth/approval-callback"},
		AuthenticationFlowBindingOverrides: map[string]string{"direct_grant": "dg-flow"},
	}
	fake.clients[client.ID] = client

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	want := map[string]string{"direct_grant": "dg-flow", "browser": fake.flows[approvalFlowAlias].ID}
	if got := fake.clients[client.ID].AuthenticationFlowBindingOverrides; !maps.Equal(got, want) {
		t.Errorf("console approval bindings = %v, want %v", got, want)
	}
	line := `[updated] client "meho-web-approve" uses login flow "meho approval sign-in level"`
	if !slices.Contains(res.MapperEventsLog, line) {
		t.Errorf("events miss %q: %v", line, res.MapperEventsLog)
	}

	res, _, err = runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	line = `[skip] client "meho-web-approve" uses login flow "meho approval sign-in level"`
	if !slices.Contains(res.MapperEventsLog, line) {
		t.Errorf("events miss %q: %v", line, res.MapperEventsLog)
	}
}

// Without the console approval client the run says how to add it, and
// makes nothing else for it.
func TestBootstrap_ConsoleApprovalClientMissingIsSkipped(t *testing.T) {
	fake := newFakeKeycloak()
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	want := `[skip] console approval client "meho-web-approve" not found; make it next to the console ` +
		`client (see the Keycloak realm guide), then run again to bind it to login flow ` +
		`"meho approval sign-in level"`
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("events miss %q: %v", want, res.MapperEventsLog)
	}
	for _, c := range fake.clients {
		if c.ClientID == "meho-web-approve" {
			t.Errorf("the bootstrap made the console approval client: %+v", c)
		}
	}
}

// A console approval client with its own acr.loa.map stops the run: its
// sign-in would not get the realm's approval level.
func TestBootstrap_ConsoleApprovalClientWithOwnLevelMapStops(t *testing.T) {
	fake := newFakeKeycloak()
	fake.clients["uuid-console-approve"] = &clientRep{
		ID: "uuid-console-approve", ClientID: "meho-web-approve",
		Attributes: map[string]string{levelMapAttribute: `{"gold":2}`},
	}
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err == nil {
		t.Fatal("Bootstrap passed, want a stop for the client's own acr.loa.map")
	}
}
