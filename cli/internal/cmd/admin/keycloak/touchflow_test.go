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
