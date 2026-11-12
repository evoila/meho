// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"testing"
)

// seedTouchFlow gives the fake the finished touch flow with its settings.
func seedTouchFlow(fake *fakeKeycloak) *fakeFlow {
	flow := &fakeFlow{ID: "touch-flow-existing", TopLevel: true}
	fake.flows[touchFlowAlias] = flow
	for _, s := range touchFlowSteps() {
		if s.Subflow != "" {
			fake.flows[s.Subflow] = &fakeFlow{ID: fake.mintID()}
		}
		step := &fakeFlowStep{
			ID: fake.mintID(), Provider: s.Provider, Subflow: s.Subflow, Requirement: s.Requirement,
		}
		if s.ConfigAlias != "" {
			step.Config = fake.mintID()
			config := map[string]any{}
			for k, v := range s.Config {
				config[k] = v
			}
			fake.stepConfigs[step.Config] = map[string]any{
				"id": step.Config, "alias": s.ConfigAlias, "config": config,
			}
		}
		fake.flows[s.Parent].Steps = append(fake.flows[s.Parent].Steps, step)
	}
	return flow
}

// seedConsoleClient gives the fake a console client like the one a
// person makes by hand.
func seedConsoleClient(fake *fakeKeycloak) *clientRep {
	client := &clientRep{
		ID: "uuid-console", ClientID: "meho-web",
		RedirectURIs:                       []string{"https://meho.example.test/ui/auth/callback"},
		Attributes:                         map[string]string{"pkce.code.challenge.method": "S256"},
		AuthenticationFlowBindingOverrides: map[string]string{"direct_grant": "dg-flow"},
	}
	fake.clients[client.ID] = client
	return client
}

// touchFlowLines lists the fake's touch flow as "<level> <name>
// <requirement> <settings>" lines.
func touchFlowLines(fake *fakeKeycloak) []string {
	var got []string
	for _, s := range fake.flowSteps(touchFlowAlias, 0) {
		name, _ := s["providerId"].(string)
		if s["authenticationFlow"] == true {
			name = s["displayName"].(string)
		}
		settings := ""
		if id, _ := s["authenticationConfig"].(string); id != "" {
			c := fake.stepConfigs[id]
			text, _ := json.Marshal(c["config"])
			settings = fmt.Sprintf("%s=%s", c["alias"], text)
		}
		got = append(got, strings.TrimSpace(fmt.Sprintf("%v %s %s %s", s["level"], name, s["requirement"], settings)))
	}
	return got
}

func wantTouchFlowLines() []string {
	var want []string
	for _, s := range touchFlowSteps() {
		settings := ""
		if s.ConfigAlias != "" {
			text, _ := json.Marshal(s.Config)
			settings = fmt.Sprintf("%s=%s", s.ConfigAlias, text)
		}
		want = append(want, strings.TrimSpace(fmt.Sprintf("%d %s %s %s", s.Level, s.name(), s.Requirement, settings)))
	}
	return want
}

func touchLevels(t *testing.T, fake *fakeKeycloak) map[string]any {
	t.Helper()
	attributes, _ := fake.realm["attributes"].(map[string]any)
	raw, _ := attributes[loaMapKey].(string)
	levels := map[string]any{}
	if raw != "" {
		if err := json.Unmarshal([]byte(raw), &levels); err != nil {
			t.Fatalf("realm attribute %s = %q: %v", loaMapKey, raw, err)
		}
	}
	return levels
}

func hasAMRMapper(fake *fakeKeycloak, clientUUID string) bool {
	return slices.ContainsFunc(fake.mappers[clientUUID], func(m protocolMapperRep) bool {
		return m.ProtocolMapper == amrMapperType
	})
}

// The flow of the bootstrap is the flow of the chart's realm file.
func TestTouchFlow_IsTheFlowOfTheRealmFile(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "..", "deploy", "charts", "meho", "charts",
		"keycloak", "files", "meho-realm.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read the realm file: %v", err)
	}
	var realm struct {
		Attributes map[string]string `json:"attributes"`
		Flows      []struct {
			Alias string `json:"alias"`
			Steps []struct {
				Authenticator string `json:"authenticator"`
				FlowAlias     string `json:"flowAlias"`
				Subflow       bool   `json:"authenticatorFlow"`
				Requirement   string `json:"requirement"`
				Priority      int    `json:"priority"`
				Config        string `json:"authenticatorConfig"`
			} `json:"authenticationExecutions"`
		} `json:"authenticationFlows"`
		Configs []struct {
			Alias  string            `json:"alias"`
			Config map[string]string `json:"config"`
		} `json:"authenticatorConfig"`
	}
	if err := json.Unmarshal(raw, &realm); err != nil {
		t.Fatalf("decode the realm file: %v", err)
	}
	configs := map[string]map[string]string{}
	for _, c := range realm.Configs {
		configs[c.Alias] = c.Config
	}
	byAlias := map[string]int{}
	for i, f := range realm.Flows {
		byAlias[f.Alias] = i
	}
	var lines func(alias string, level int) []string
	lines = func(alias string, level int) []string {
		steps := realm.Flows[byAlias[alias]].Steps
		sort.SliceStable(steps, func(i, j int) bool { return steps[i].Priority < steps[j].Priority })
		var out []string
		for _, s := range steps {
			name := s.Authenticator
			if s.Subflow {
				name = s.FlowAlias
			}
			settings := ""
			if s.Config != "" {
				text, _ := json.Marshal(configs[s.Config])
				settings = fmt.Sprintf("%s=%s", s.Config, text)
			}
			out = append(out, strings.TrimSpace(fmt.Sprintf("%d %s %s %s", level, name, s.Requirement, settings)))
			if s.Subflow {
				out = append(out, lines(s.FlowAlias, level+1)...)
			}
		}
		return out
	}
	if got := lines(touchFlowAlias, 0); !slices.Equal(got, wantTouchFlowLines()) {
		t.Errorf("realm file flow =\n%s\nbootstrap flow =\n%s",
			strings.Join(got, "\n"), strings.Join(wantTouchFlowLines(), "\n"))
	}
	if got := realm.Attributes[loaMapKey]; got != `{"meho-touch":3}` {
		t.Errorf("realm file %s = %q", loaMapKey, got)
	}
}

// A realm without the flow gets it, the level, the amr mapper and the
// binding; a second run changes nothing.
func TestBootstrap_TouchFlowIsMadeAndBoundToTheConsole(t *testing.T) {
	fake := newFakeKeycloak()
	fake.realm["attributes"].(map[string]any)[loaMapKey] = `{"meho-approve":2}`
	console := seedConsoleClient(fake)

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := touchFlowLines(fake); !slices.Equal(got, wantTouchFlowLines()) {
		t.Errorf("touch flow =\n%s\nwant\n%s", strings.Join(got, "\n"), strings.Join(wantTouchFlowLines(), "\n"))
	}
	if got := touchLevels(t, fake); !maps.Equal(got, map[string]any{"meho-approve": 2.0, "meho-touch": 3.0}) {
		t.Errorf("realm levels = %v, want meho-approve 2 and meho-touch 3", got)
	}
	if fake.realm["attributes"].(map[string]any)["frontendUrl"] != "" {
		t.Errorf("the realm lost its other attributes: %v", fake.realm["attributes"])
	}
	bound := fake.clients[console.ID]
	want := map[string]string{"direct_grant": "dg-flow", "browser": fake.flows[touchFlowAlias].ID}
	if !maps.Equal(bound.AuthenticationFlowBindingOverrides, want) {
		t.Errorf("console bindings = %v, want %v", bound.AuthenticationFlowBindingOverrides, want)
	}
	if !slices.Equal(bound.RedirectURIs, console.RedirectURIs) {
		t.Errorf("console redirect URIs = %v", bound.RedirectURIs)
	}
	if !hasAMRMapper(fake, console.ID) {
		t.Errorf("console has no amr mapper: %v", fake.mappers[console.ID])
	}
	for _, line := range []string{
		`[created] login flow "meho approval touch" (passkey touch level meho-touch)`,
		"[updated] realm maps meho-touch to level 3",
		`[added] mapper amr on client "meho-web"`,
		`[updated] client "meho-web" uses login flow "meho approval touch"`,
	} {
		if !slices.Contains(res.MapperEventsLog, line) {
			t.Errorf("events miss %q: %v", line, res.MapperEventsLog)
		}
	}

	fake.calls = map[string]int{}
	res, _, err = runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	for _, call := range []string{
		"POST /authentication/flows...", "PUT /authentication/flows...", "PUT /realm",
		"POST /authentication/executions/{id}/config",
	} {
		if fake.calls[call] != 0 {
			t.Errorf("second run made %d calls %q, want 0", fake.calls[call], call)
		}
	}
	if !slices.Contains(res.MapperEventsLog, `[skip] login flow "meho approval touch" exists`) {
		t.Errorf("events miss the kept flow line: %v", res.MapperEventsLog)
	}
}

// Without the console client the flow and the level are made, and the
// run says to bind the client by hand.
func TestBootstrap_TouchFlowWithoutConsoleClient(t *testing.T) {
	fake := newFakeKeycloak()
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if _, ok := fake.flows[touchFlowAlias]; !ok {
		t.Error("the touch flow was not made")
	}
	want := `[skip] console client "meho-web" not found; bind it to login flow "meho approval touch" by hand`
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("events miss %q: %v", want, res.MapperEventsLog)
	}
}

// A flow with other settings is kept as it is, and the console client
// is not bound to it.
func TestBootstrap_TouchFlowWithOtherSettingsIsNotBound(t *testing.T) {
	fake := newFakeKeycloak()
	seedTouchFlow(fake)
	console := seedConsoleClient(fake)
	for _, c := range fake.stepConfigs {
		if c["alias"] == touchFlowAlias+" - level 3" {
			c["config"].(map[string]any)["loa-max-age"] = "300"
		}
	}

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]; got != "" {
		t.Errorf("the console is bound to %q, want no browser flow", got)
	}
	if fake.calls["PUT /realm"] != 0 {
		t.Error("the realm was changed")
	}
	if !slices.ContainsFunc(res.MapperEventsLog, func(e string) bool {
		return strings.HasPrefix(e, `[skip] login flow "meho approval touch" has another shape (step 14`) &&
			strings.Contains(e, "has other settings")
	}) {
		t.Errorf("events miss the other shape line: %v", res.MapperEventsLog)
	}
}

// A realm that maps meho-touch to another level is not changed, and the
// console client is not bound.
func TestBootstrap_TouchLevelTakenIsNotChanged(t *testing.T) {
	fake := newFakeKeycloak()
	fake.realm["attributes"].(map[string]any)[loaMapKey] = `{"meho-touch":2}`
	console := seedConsoleClient(fake)

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if fake.calls["PUT /realm"] != 0 {
		t.Error("the realm was changed")
	}
	if got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]; got != "" {
		t.Errorf("the console is bound to %q, want no browser flow", got)
	}
	want := "[skip] realm maps meho-touch to level 2, not 3; the console client is not bound"
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("events miss %q: %v", want, res.MapperEventsLog)
	}
}

// A dry run changes nothing and says what a real run would do.
func TestBootstrap_DryRunMakesNoTouchFlow(t *testing.T) {
	fake := newFakeKeycloak()
	console := seedConsoleClient(fake)
	opts := fullOpts()
	opts.DryRun = true

	res, _, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if _, ok := fake.flows[touchFlowAlias]; ok {
		t.Error("a dry run made the touch flow")
	}
	if len(touchLevels(t, fake)) != 0 || len(fake.clients[console.ID].AuthenticationFlowBindingOverrides) != 1 {
		t.Error("a dry run changed the realm or the console client")
	}
	for _, line := range []string{
		`[would create] login flow "meho approval touch" (passkey touch level meho-touch)`,
		`[would update] client "meho-web" uses login flow "meho approval touch"`,
	} {
		if !slices.Contains(res.MapperEventsLog, line) {
			t.Errorf("events miss %q: %v", line, res.MapperEventsLog)
		}
	}
}

// A Keycloak older than 26.4 has no `conditional-credential` step. The
// flow is not made at all, so no half-made flow stays behind.
func TestBootstrap_TouchFlowNeedsEveryStep(t *testing.T) {
	fake := newFakeKeycloak()
	fake.noProviders = []string{"conditional-credential"}
	console := seedConsoleClient(fake)

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if _, ok := fake.flows[touchFlowAlias]; ok {
		t.Error("the touch flow was made")
	}
	if fake.calls["PUT /realm"] != 0 || len(fake.clients[console.ID].AuthenticationFlowBindingOverrides) != 1 {
		t.Error("the realm or the console client was changed")
	}
	want := `[skip] login flow "meho approval touch" not made: this Keycloak has no step conditional-credential ` +
		`(Keycloak 26.4 or newer has it); the console client is not bound`
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("events miss %q: %v", want, res.MapperEventsLog)
	}
}

// A realm whose passkey policy does not ask for user verification is
// not changed: a touch there could pass without a PIN or a fingerprint.
func TestBootstrap_TouchFlowNeedsUserVerification(t *testing.T) {
	for _, policy := range []any{"preferred", "discouraged", "not specified", nil} {
		t.Run(fmt.Sprint(policy), func(t *testing.T) {
			fake := newFakeKeycloak()
			if policy == nil {
				delete(fake.realm, passkeyUVPolicy)
			} else {
				fake.realm[passkeyUVPolicy] = policy
			}
			console := seedConsoleClient(fake)

			res, _, err := runBootstrapOnce(t, fake, fullOpts())
			if err != nil {
				t.Fatalf("Bootstrap: %v", err)
			}
			if _, ok := fake.flows[touchFlowAlias]; ok {
				t.Error("the touch flow was made")
			}
			if fake.calls["PUT /realm"] != 0 || len(fake.clients[console.ID].AuthenticationFlowBindingOverrides) != 1 {
				t.Error("the realm or the console client was changed")
			}
			if hasAMRMapper(fake, console.ID) {
				t.Error("the console client got the amr mapper")
			}
			if !slices.ContainsFunc(res.MapperEventsLog, func(e string) bool {
				return strings.HasPrefix(e, `[skip] login flow "meho approval touch" not made: the realm's passkey policy`)
			}) {
				t.Errorf("events miss the passkey policy line: %v", res.MapperEventsLog)
			}
		})
	}
}

// seedBrowserRedirect gives the realm's browser flow a redirect step that
// goes straight to the company login.
func seedBrowserRedirect(fake *fakeKeycloak, provider string) {
	fake.stepConfigs["browser-redirect"] = map[string]any{
		"id": "browser-redirect", "alias": "meho-company-login-redirect",
		"config": map[string]any{"defaultProvider": provider},
	}
	fake.flows["browser"].Steps[2].Config = "browser-redirect"
}

// touchRedirectConfig returns the settings of the touch flow's redirect
// step, or nil.
func touchRedirectConfig(fake *fakeKeycloak) map[string]any {
	for _, s := range fake.flowSteps(touchFlowAlias, 0) {
		if s["providerId"] == redirectorType {
			if id, _ := s["authenticationConfig"].(string); id != "" {
				return fake.stepConfigs[id]
			}
		}
	}
	return nil
}

// With the company login redirect on, the console still goes straight
// to the company login after the bind.
func TestBootstrap_TouchFlowGetsTheCompanyLoginRedirect(t *testing.T) {
	fake := newFakeKeycloak()
	seedBrowserRedirect(fake, "company")
	console := seedConsoleClient(fake)

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	got := touchRedirectConfig(fake)
	if got == nil || got["alias"] != touchRedirectAlias ||
		got["config"].(map[string]any)["defaultProvider"] != "company" {
		t.Fatalf("touch flow redirect settings = %v, want %s going to company", got, touchRedirectAlias)
	}
	if fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"] != fake.flows[touchFlowAlias].ID {
		t.Error("the console client is not bound to the touch flow")
	}
	want := `[updated] login flow "meho approval touch" goes straight to the company login "company", ` +
		`like login flow "browser"`
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("events miss %q: %v", want, res.MapperEventsLog)
	}

	fake.calls = map[string]int{}
	res, _, err = runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if fake.calls["POST /authentication/executions/{id}/config"] != 0 || fake.calls["PUT /authentication/config/{id}"] != 0 {
		t.Errorf("second run changed step settings: %v", fake.calls)
	}
	if slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("second run says it changed the redirect: %v", res.MapperEventsLog)
	}
}

// A touch flow that goes to another company login gets the realm's one.
func TestBootstrap_TouchFlowRedirectToAnotherCompanyLoginIsUpdated(t *testing.T) {
	fake := newFakeKeycloak()
	seedTouchFlow(fake)
	seedBrowserRedirect(fake, "company")
	seedConsoleClient(fake)
	for _, s := range fake.flows[touchFlowAlias+" - sign-in"].Steps {
		if s.Provider == redirectorType {
			s.Config = "old"
		}
	}
	fake.stepConfigs["old"] = map[string]any{
		"id": "old", "alias": touchRedirectAlias, "config": map[string]any{"defaultProvider": "old-company"},
	}

	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := fake.stepConfigs["old"]["config"].(map[string]any)["defaultProvider"]; got != "company" {
		t.Errorf("touch flow redirect goes to %v, want company", got)
	}
	if fake.calls["PUT /authentication/config/{id}"] != 1 {
		t.Errorf("PUT of the step settings = %d, want 1", fake.calls["PUT /authentication/config/{id}"])
	}
}

// Redirect on, run, redirect off, run again: the touch flow loses the
// old default too, so the console stops going to that company login.
func TestBootstrap_TouchFlowRedirectRemovedWithTheRealms(t *testing.T) {
	fake := newFakeKeycloak()
	seedBrowserRedirect(fake, "company")
	seedConsoleClient(fake)
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if touchRedirectConfig(fake) == nil {
		t.Fatal("the first run did not copy the redirect")
	}
	fake.flows["browser"].Steps[2].Config = ""
	delete(fake.stepConfigs, "browser-redirect")

	fake.calls = map[string]int{}
	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("second Bootstrap: %v", err)
	}
	if got := touchRedirectConfig(fake); got != nil {
		t.Errorf("touch flow still has redirect settings %v", got)
	}
	if fake.calls["DELETE /authentication/config/{id}"] != 1 {
		t.Errorf("DELETE of the step settings = %d, want 1", fake.calls["DELETE /authentication/config/{id}"])
	}
	want := `[updated] login flow "meho approval touch" no longer goes straight to a company login, ` +
		`like login flow "browser"`
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("events miss %q: %v", want, res.MapperEventsLog)
	}

	fake.calls = map[string]int{}
	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("third Bootstrap: %v", err)
	}
	if fake.calls["DELETE /authentication/config/{id}"] != 0 {
		t.Error("third run removed the redirect settings again")
	}
}

// A realm whose login flow is not the built-in one keeps its console
// login: the touch flow is made but not bound.
func TestBootstrap_TouchFlowNotBoundOverTheRealmsOwnLogin(t *testing.T) {
	fake := newFakeKeycloak()
	fake.flows["browser with extra checks"] = &fakeFlow{ID: "custom-flow", TopLevel: true}
	fake.realm["browserFlow"] = "browser with extra checks"
	console := seedConsoleClient(fake)

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if _, ok := fake.flows[touchFlowAlias]; !ok {
		t.Error("the touch flow was not made")
	}
	if got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]; got != "" {
		t.Errorf("console bound to %q over the realm's own login flow", got)
	}
	if !slices.ContainsFunc(res.MapperEventsLog, func(e string) bool {
		return strings.HasPrefix(e, `[skip] client "meho-web" not bound`) &&
			strings.Contains(e, `"browser with extra checks"`)
	}) {
		t.Errorf("events miss the not-bound line: %v", res.MapperEventsLog)
	}
}

// A console client with its own login flow keeps it.
func TestBootstrap_TouchFlowNotBoundOverTheClientsOwnLogin(t *testing.T) {
	fake := newFakeKeycloak()
	console := seedConsoleClient(fake)
	console.AuthenticationFlowBindingOverrides["browser"] = "client-own-flow"

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]; got != "client-own-flow" {
		t.Errorf("console browser flow = %q, want client-own-flow", got)
	}
	if !slices.ContainsFunc(res.MapperEventsLog, func(e string) bool {
		return strings.HasPrefix(e, `[skip] client "meho-web" not bound`) && strings.Contains(e, `"client-own-flow"`)
	}) {
		t.Errorf("events miss the not-bound line: %v", res.MapperEventsLog)
	}
}

// seedLoginSteps gives the fake the Keycloak 26.0 login as flow alias;
// the sub-flows of a copy carry prefix in front of their names.
func seedLoginSteps(fake *fakeKeycloak, alias, prefix string) {
	forms, code := prefix+"forms", prefix+"Browser - Conditional OTP"
	fake.flows[alias] = &fakeFlow{ID: alias + "-flow", TopLevel: true, Steps: []*fakeFlowStep{
		{ID: alias + "-cookie", Provider: "auth-cookie", Requirement: "ALTERNATIVE"},
		{ID: alias + "-spnego", Provider: "auth-spnego", Requirement: "DISABLED"},
		{ID: alias + "-redirector", Provider: redirectorType, Requirement: "ALTERNATIVE"},
		{ID: alias + "-forms", Subflow: forms, Requirement: "ALTERNATIVE"},
	}}
	fake.flows[forms] = &fakeFlow{ID: forms + "-flow", Steps: []*fakeFlowStep{
		{ID: alias + "-pw", Provider: "auth-username-password-form", Requirement: "REQUIRED"},
		{ID: alias + "-code", Subflow: code, Requirement: "CONDITIONAL"},
	}}
	fake.flows[code] = &fakeFlow{ID: code + "-flow", Steps: []*fakeFlowStep{
		{ID: alias + "-user", Provider: "conditional-user-configured", Requirement: "REQUIRED"},
		{ID: alias + "-otp", Provider: "auth-otp-form", Requirement: "REQUIRED"},
	}}
}

// seedPasskeyCopy gives the fake the built-in login and the copy that
// keycloak.authflow.ensure_passkey_code_skip makes, and binds the copy.
func seedPasskeyCopy(fake *fakeKeycloak) {
	seedLoginSteps(fake, "browser", "")
	seedLoginSteps(fake, passkeyFlowAlias, passkeyFlowAlias+" ")
	fake.stepConfigs["skip-code"] = map[string]any{
		"id": "skip-code", "alias": "browser with passkeys - skip code after passkey",
		"config": map[string]any{"credentials": "webauthn-passwordless", "included": "false"},
	}
	code := fake.flows[passkeyFlowAlias+" Browser - Conditional OTP"]
	code.Steps = slices.Insert(code.Steps, 1, &fakeFlowStep{
		ID: "condition", Provider: passkeyConditionType, Requirement: "REQUIRED", Config: "skip-code",
	})
	fake.realm["browserFlow"] = passkeyFlowAlias
}

// The passkey copy of the built-in login is not a login of its own.
func TestBootstrap_TouchFlowBoundOverThePasskeyCopy(t *testing.T) {
	fake := newFakeKeycloak()
	seedPasskeyCopy(fake)
	console := seedConsoleClient(fake)

	if _, _, err := runBootstrapOnce(t, fake, fullOpts()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"] != fake.flows[touchFlowAlias].ID {
		t.Error("the console client is not bound to the touch flow")
	}
}

// "browser with passkeys" can be edited: a copy with an added or changed
// step keeps the console login it has.
func TestBootstrap_TouchFlowNotBoundOverAnEditedPasskeyCopy(t *testing.T) {
	forms := passkeyFlowAlias + " forms"
	code := passkeyFlowAlias + " Browser - Conditional OTP"
	cases := map[string]func(fake *fakeKeycloak){
		"extra required step": func(fake *fakeKeycloak) {
			fake.flows[forms].Steps = append(fake.flows[forms].Steps,
				&fakeFlowStep{ID: "extra", Provider: "auth-otp-form", Requirement: "REQUIRED"})
		},
		"code step turned off": func(fake *fakeKeycloak) {
			fake.flows[code].Steps[2].Requirement = "DISABLED"
		},
		"condition settings": func(fake *fakeKeycloak) {
			fake.stepConfigs["skip-code"]["config"].(map[string]any)["included"] = "true"
		},
		"no condition": func(fake *fakeKeycloak) {
			fake.flows[code].Steps = slices.Delete(fake.flows[code].Steps, 1, 2)
		},
	}
	for name, edit := range cases {
		t.Run(name, func(t *testing.T) {
			fake := newFakeKeycloak()
			seedPasskeyCopy(fake)
			edit(fake)
			console := seedConsoleClient(fake)

			res, _, err := runBootstrapOnce(t, fake, fullOpts())
			if err != nil {
				t.Fatalf("Bootstrap: %v", err)
			}
			if got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]; got != "" {
				t.Errorf("console bound to %q over an edited passkey copy", got)
			}
			if !slices.ContainsFunc(res.MapperEventsLog, func(e string) bool {
				return strings.HasPrefix(e, `[skip] client "meho-web" not bound`) &&
					strings.Contains(e, `"browser with passkeys"`)
			}) {
				t.Errorf("events miss the not-bound line: %v", res.MapperEventsLog)
			}
		})
	}
}

// A console client bound to the touch flow stays bound, also when the
// realm has its own login flow (the op bound it with replace_custom_login).
func TestBootstrap_TouchFlowBindingIsKeptOverTheRealmsOwnLogin(t *testing.T) {
	fake := newFakeKeycloak()
	fake.flows["browser with extra checks"] = &fakeFlow{ID: "custom-flow", TopLevel: true}
	fake.realm["browserFlow"] = "browser with extra checks"
	flow := seedTouchFlow(fake)
	console := seedConsoleClient(fake)
	console.AuthenticationFlowBindingOverrides["browser"] = flow.ID

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]; got != flow.ID {
		t.Errorf("console browser flow = %q, want the touch flow", got)
	}
	if !slices.Contains(res.MapperEventsLog, `[skip] client "meho-web" uses login flow "meho approval touch"`) {
		t.Errorf("events miss the bound line: %v", res.MapperEventsLog)
	}
	if slices.ContainsFunc(res.MapperEventsLog, func(e string) bool { return strings.Contains(e, "not bound") }) {
		t.Errorf("events say not bound: %v", res.MapperEventsLog)
	}
}

// A dry run says it would copy the redirect and changes nothing.
func TestBootstrap_DryRunTouchFlowRedirect(t *testing.T) {
	fake := newFakeKeycloak()
	seedBrowserRedirect(fake, "company")
	seedConsoleClient(fake)
	opts := fullOpts()
	opts.DryRun = true

	res, _, err := runBootstrapOnce(t, fake, opts)
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	want := `[would update] login flow "meho approval touch" goes straight to the company login "company", ` +
		`like login flow "browser"`
	if !slices.Contains(res.MapperEventsLog, want) {
		t.Errorf("events miss %q: %v", want, res.MapperEventsLog)
	}
	if len(fake.stepConfigs) != 2 {
		t.Errorf("a dry run changed step settings: %v", fake.stepConfigs)
	}
}

// An amr mapper that leaves the claim out of the ID token gets its token
// settings turned on before the bind.
func TestBootstrap_AMRMapperWithoutIDTokenIsFixed(t *testing.T) {
	fake := newFakeKeycloak()
	console := seedConsoleClient(fake)
	fake.mappers[console.ID] = []protocolMapperRep{{
		ID: "amr-old", Name: "amr", Protocol: "openid-connect", ProtocolMapper: amrMapperType,
		Config: map[string]string{
			"id.token.claim": "false", "access.token.claim": "true", "userinfo.token.claim": "true",
		},
	}}

	res, _, err := runBootstrapOnce(t, fake, fullOpts())
	if err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	var amr []protocolMapperRep
	for _, m := range fake.mappers[console.ID] {
		if m.ProtocolMapper == amrMapperType {
			amr = append(amr, m)
		}
	}
	want := map[string]string{
		"id.token.claim": "true", "access.token.claim": "true",
		"introspection.token.claim": "true", "userinfo.token.claim": "true",
	}
	if len(amr) != 1 || amr[0].ID != "amr-old" || !maps.Equal(amr[0].Config, want) {
		t.Errorf("amr mappers = %v, want amr-old with %v", amr, want)
	}
	if fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"] != fake.flows[touchFlowAlias].ID {
		t.Error("the console client is not bound to the touch flow")
	}
	line := `[updated] mapper amr on client "meho-web" writes amr into every token`
	if !slices.Contains(res.MapperEventsLog, line) {
		t.Errorf("events miss %q: %v", line, res.MapperEventsLog)
	}
}
