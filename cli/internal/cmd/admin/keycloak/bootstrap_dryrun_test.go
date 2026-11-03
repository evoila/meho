// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeState is the stored realm of the fake, as JSON, to check that a
// dry run changed nothing.
func (f *fakeKeycloak) fakeState(t *testing.T) string {
	t.Helper()
	b, err := json.Marshal([]any{
		f.clients, f.mappers, f.defaultScopes, f.optionalScopes,
		f.realmScopes, f.createdScopes, f.groups, f.users, f.userGroups,
		f.passwords, f.flows,
	})
	if err != nil {
		t.Fatalf("marshal fake state: %v", err)
	}
	return string(b)
}

// countWrites counts every request to the realm that is not a read.
func countWrites(next http.Handler, writes *int) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && strings.HasPrefix(r.URL.Path, "/admin/") {
			*writes++
		}
		next.ServeHTTP(w, r)
	}
}

// runDryRun runs Bootstrap with --dry-run against fake and fails the
// test when the run sent any write to the realm.
func runDryRun(t *testing.T, fake *fakeKeycloak, opts BootstrapOptions) (*Result, string) {
	t.Helper()
	writes := 0
	srv := httptest.NewServer(countWrites(fake.handler(), &writes))
	t.Cleanup(srv.Close)
	prev := httpClientOverride
	httpClientOverride = srv.Client()
	t.Cleanup(func() { httpClientOverride = prev })

	var stdout bytes.Buffer
	opts.KeycloakBaseURL = srv.URL
	opts.Out = &stdout
	opts.DryRun = true
	before := fake.fakeState(t)
	res, err := Bootstrap(t.Context(), opts)
	if err != nil {
		t.Fatalf("Bootstrap dry-run: %v", err)
	}
	if writes != 0 {
		t.Errorf("dry run sent %d writes to the realm, want 0", writes)
	}
	if after := fake.fakeState(t); after != before {
		t.Errorf("dry run changed the realm")
	}
	return res, stdout.String()
}

// eventWith returns the first event that starts with prefix, or "".
func eventWith(res *Result, prefix string) string {
	for _, e := range res.MapperEventsLog {
		if strings.HasPrefix(e, prefix) {
			return e
		}
	}
	return ""
}

// tuneRealm changes what an admin may have changed on purpose in a
// working realm: the redirect URIs and front-channel logout of
// meho-mcp, and one setting of its tenant-id mapper.
func tuneRealm(t *testing.T, fake *fakeKeycloak) {
	t.Helper()
	mcp := fake.clients[fake.clientUUID(t, "meho-mcp")]
	mcp.RedirectURIs = []string{"https://ide.example.com/callback"}
	mcp.FrontchannelLogout = boolPtr(true)
	uuid := fake.clientUUID(t, "meho-mcp")
	for i, m := range fake.mappers[uuid] {
		if m.Name == "tenant-id" {
			fake.mappers[uuid][i].Config["aggregate.attrs"] = "true"
		}
	}
}

// On a fresh realm the dry run lists what it would create and creates
// nothing.
func TestBootstrap_DryRunOnFreshRealmCreatesNothing(t *testing.T) {
	fake := newFakeKeycloak()
	res, stdout := runDryRun(t, fake, fullOpts())
	if !strings.Contains(stdout, "DRY RUN") {
		t.Errorf("dry-run stdout missing banner; got:\n%s", stdout)
	}
	for _, want := range []string{
		"[would create] client meho-cli ",
		"[would create] client meho-cli-approve ",
		"[would create] client meho-mcp ",
		"[would create] mapper tenant-id ",
		"[would create] group MEHO-Admins ",
		`[would create] login flow "meho approval sign-in"`,
	} {
		if eventWith(res, want) == "" {
			t.Errorf("no event %q in %v", want, res.MapperEventsLog)
		}
	}
	for _, e := range res.MapperEventsLog {
		if strings.HasPrefix(e, "[created]") || strings.HasPrefix(e, "[updated]") || strings.HasPrefix(e, "[added]") {
			t.Errorf("dry-run event %q reads as done", e)
		}
	}
	if len(fake.clients) != 0 || len(fake.groups) != 0 || len(fake.users) != 0 {
		t.Errorf("dry run made clients=%d groups=%d users=%d", len(fake.clients), len(fake.groups), len(fake.users))
	}
}

// On a realm an admin tuned, the dry run names each field it would
// overwrite, with the old and the new value, and changes nothing.
func TestBootstrap_DryRunShowsEachChangeOnExistingRealm(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("first Bootstrap: %v", err)
	}
	tuneRealm(t, fake)

	res, _ := runDryRun(t, fake, fullOpts())
	client := eventWith(res, "[would update] client meho-mcp ")
	for _, want := range []string{
		"redirectUris: [https://ide.example.com/callback] -> [http://localhost:* http://127.0.0.1:*]",
		"frontchannelLogout: true -> false",
	} {
		if !strings.Contains(client, want) {
			t.Errorf("client event %q does not contain %q", client, want)
		}
	}
	mapper := eventWith(res, "[would update] mapper tenant-id")
	if !strings.Contains(mapper, `aggregate.attrs: "true" -> "false"`) {
		t.Errorf("mapper event %q does not name the aggregate.attrs change", mapper)
	}
	// The clients and groups the admin did not change stay [skip].
	for _, want := range []string{
		"[skip]    client meho-cli ",
		"[skip]    client meho-cli-approve ",
		"[skip]    group MEHO-Admins ",
	} {
		if eventWith(res, want) == "" {
			t.Errorf("no event %q in %v", want, res.MapperEventsLog)
		}
	}
}

// A real run on a tuned realm prints the same field changes, and a run
// on a realm that matches sends no client PUT.
func TestBootstrap_RealRunNamesChangesAndSkipsMatchingClients(t *testing.T) {
	fake := newFakeKeycloak()
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("first Bootstrap: %v", err)
	}
	puts := fake.calls["PUT /clients/{uuid}"]
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if got := fake.calls["PUT /clients/{uuid}"]; got != puts {
		t.Errorf("re-run on a matching realm sent %d client PUTs, want 0", got-puts)
	}
	if eventWith(res, "[skip]    client meho-mcp ") == "" {
		t.Errorf("no [skip] event for meho-mcp in %v", res.MapperEventsLog)
	}

	tuneRealm(t, fake)
	res, _, err = runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("third Bootstrap: %v", err)
	}
	if !strings.Contains(eventWith(res, "[updated] client meho-mcp "), "frontchannelLogout: true -> false") {
		t.Errorf("update event does not name the change: %v", res.MapperEventsLog)
	}
	if got := fake.clients[fake.clientUUID(t, "meho-mcp")].FrontchannelLogout; got == nil || *got {
		t.Errorf("real run did not set frontchannelLogout back to false")
	}
}

// A dry run on a realm with a half-made approval flow says it would
// finish it, and does not touch it.
func TestBootstrap_DryRunOnHalfMadeFlow(t *testing.T) {
	fake := newFakeKeycloak()
	fake.flows[approvalFlowAlias] = &fakeFlow{ID: "flow-1", TopLevel: true}
	res, _ := runDryRun(t, fake, fullOpts())
	want := `[would update] login flow "meho approval sign-in": finished`
	if eventWith(res, want) == "" {
		t.Errorf("no event %q in %v", want, res.MapperEventsLog)
	}
}

func TestClientChanges(t *testing.T) {
	existing := &clientRep{
		Name:               "MEHO MCP",
		FrontchannelLogout: boolPtr(true),
		RedirectURIs:       []string{"b", "a"},
		WebOrigins:         []string{"+"},
		Attributes:         map[string]string{"pkce.code.challenge.method": "S256", "kept": "x"},
	}
	desired := &clientRep{
		Name:                               "MEHO MCP",
		FrontchannelLogout:                 boolPtr(false),
		StandardFlowEnabled:                boolPtr(true),
		RedirectURIs:                       []string{"a", "b"},
		WebOrigins:                         []string{},
		Attributes:                         map[string]string{"pkce.code.challenge.method": "", "use.refresh.tokens": "true"},
		AuthenticationFlowBindingOverrides: map[string]string{"browser": "flow-1"},
	}
	got := strings.Join(clientChanges(existing, desired), "\n")
	for _, want := range []string{
		"standardFlowEnabled: (none) -> true",
		"frontchannelLogout: true -> false",
		"webOrigins: [+] -> []",
		`attribute pkce.code.challenge.method: "S256" -> (none)`,
		`attribute use.refresh.tokens: (none) -> "true"`,
		`login flow override browser: (none) -> "flow-1"`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("changes miss %q:\n%s", want, got)
		}
	}
	for _, notWant := range []string{"name:", "redirectUris", "kept"} {
		if strings.Contains(got, notWant) {
			t.Errorf("changes name %q, which does not change:\n%s", notWant, got)
		}
	}
	if c := clientChanges(desired, desired); len(c) != 0 {
		t.Errorf("same client gives changes %v", c)
	}
}

func TestMapperChanges(t *testing.T) {
	got := protocolMapperRep{ProtocolMapper: "oidc-hardcoded-claim-mapper",
		Config: map[string]string{"claim.value": "x", "claim.name": "tenant_id"}}
	want := protocolMapperRep{ProtocolMapper: "oidc-usermodel-attribute-mapper",
		Config: map[string]string{"claim.name": "tenant_id", "user.attribute": "tenant_id"}}
	text := strings.Join(mapperChanges(got, want), "\n")
	for _, w := range []string{
		"type: oidc-hardcoded-claim-mapper -> oidc-usermodel-attribute-mapper",
		`claim.value: "x" -> (removed)`,
		`user.attribute: (none) -> "tenant_id"`,
	} {
		if !strings.Contains(text, w) {
			t.Errorf("changes miss %q:\n%s", w, text)
		}
	}
	if strings.Contains(text, "claim.name") {
		t.Errorf("changes name claim.name, which does not change:\n%s", text)
	}
}

func TestDryRunEvent(t *testing.T) {
	for in, want := range map[string]string{
		"[created] client x":           "[would create] client x",
		"[updated] mapper y: [a] -> b": "[would update] mapper y: [a] -> b",
		"[added]   scope z":            "[would add]   scope z",
		"[removed] optional scope z":   "[would remove] optional scope z",
		"[skip]    group g":            "[skip]    group g",
		"no tag":                       "no tag",
	} {
		if got := dryRunEvent(in); got != want {
			t.Errorf("dryRunEvent(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestPrintSummaryDryRun(t *testing.T) {
	var out bytes.Buffer
	opts := fullOpts()
	opts.DryRun = true
	printSummary(&out, opts, &Result{
		MapperEventsLog:  []string{"[would create] client meho-cli (uuid=dry-run-new)"},
		AdminUserCreated: true,
	})
	text := out.String()
	for _, want := range []string{
		"DRY RUN: the changes a real run would make (nothing was changed)",
		"[would create] user deployer@example.com",
		"Nothing was changed. Run without --dry-run",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("summary misses %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "Ready for:") {
		t.Errorf("dry-run summary says the realm is ready:\n%s", text)
	}
}
