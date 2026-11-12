// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"slices"
	"strings"
	"testing"
)

// seedDefaultLogin gives the fake the "browser" flow a new realm gets in
// Keycloak 26.8, written out by hand from Keycloak's
// DefaultAuthenticationFlows.browserFlow.
func seedDefaultLogin(fake *fakeKeycloak) {
	fake.stepConfigs["browser-conditional-credential"] = map[string]any{
		"id": "browser-conditional-credential", "alias": "browser-conditional-credential",
		"config": map[string]any{"credentials": "webauthn-passwordless"},
	}
	fake.flows["browser"] = &fakeFlow{ID: "browser-flow", TopLevel: true, Steps: []*fakeFlowStep{
		{ID: "browser-cookie", Provider: "auth-cookie", Requirement: "ALTERNATIVE"},
		{ID: "browser-spnego", Provider: "auth-spnego", Requirement: "DISABLED"},
		{ID: "browser-redirector", Provider: redirectorType, Requirement: "ALTERNATIVE"},
		{ID: "browser-organization", Subflow: "Organization", Requirement: "ALTERNATIVE"},
		{ID: "browser-forms", Subflow: "forms", Requirement: "ALTERNATIVE"},
	}}
	fake.flows["Organization"] = &fakeFlow{ID: "organization-flow", Steps: []*fakeFlowStep{
		{ID: "browser-org-condition", Subflow: "Browser - Conditional Organization", Requirement: "CONDITIONAL"},
	}}
	fake.flows["Browser - Conditional Organization"] = &fakeFlow{ID: "org-condition-flow", Steps: []*fakeFlowStep{
		{ID: "browser-org-user", Provider: "conditional-user-configured", Requirement: "REQUIRED"},
		{ID: "browser-org", Provider: "organization", Requirement: "ALTERNATIVE"},
	}}
	fake.flows["forms"] = &fakeFlow{ID: "forms-flow", Steps: []*fakeFlowStep{
		{ID: "browser-pw", Provider: "auth-username-password-form", Requirement: "REQUIRED"},
		{ID: "browser-2fa", Subflow: "Browser - Conditional 2FA", Requirement: "CONDITIONAL"},
	}}
	fake.flows["Browser - Conditional 2FA"] = &fakeFlow{ID: "2fa-flow", Steps: []*fakeFlowStep{
		{ID: "browser-2fa-user", Provider: "conditional-user-configured", Requirement: "REQUIRED"},
		{ID: "browser-2fa-passkey", Provider: passkeyConditionType, Requirement: "REQUIRED",
			Config: "browser-conditional-credential"},
		{ID: "browser-otp", Provider: "auth-otp-form", Requirement: "ALTERNATIVE"},
		{ID: "browser-webauthn", Provider: "webauthn-authenticator", Requirement: "DISABLED"},
		{ID: "browser-recovery", Provider: "auth-recovery-authn-code-form", Requirement: "DISABLED"},
	}}
}

// listedSteps lists the fake's flow the way the client reads it: the
// level is a JSON number.
func listedSteps(fake *fakeKeycloak, alias string) ([]map[string]any, []map[string]any) {
	steps := fake.flowSteps(alias, 0)
	settings := make([]map[string]any, len(steps))
	for i, s := range steps {
		s["level"] = float64(s["level"].(int))
		if id, _ := s["authenticationConfig"].(string); id != "" {
			settings[i], _ = fake.stepConfigs[id]["config"].(map[string]any)
		}
	}
	return steps, settings
}

func TestDefaultLogin_EachKeycloak26ShapeMatches(t *testing.T) {
	fake := newFakeKeycloak()
	if diff := defaultLoginDifference(listedSteps(fake, "browser")); diff != "" {
		t.Errorf("Keycloak 26.8 login: %s", diff)
	}
	seedLoginSteps(fake, "browser", "")
	if diff := defaultLoginDifference(listedSteps(fake, "browser")); diff != "" {
		t.Errorf("Keycloak 26.0 login: %s", diff)
	}
}

func TestDefaultLogin_ChangesAreNamed(t *testing.T) {
	cases := map[string]struct {
		edit func(fake *fakeKeycloak)
		want string
	}{
		"code required for everyone": {
			edit: func(fake *fakeKeycloak) { fake.flows["forms"].Steps[1].Requirement = "REQUIRED" },
			want: `"Browser - Conditional 2FA" (REQUIRED)`,
		},
		"kerberos turned on": {
			edit: func(fake *fakeKeycloak) { fake.flows["browser"].Steps[1].Requirement = "ALTERNATIVE" },
			want: `"auth-spnego" (ALTERNATIVE)`,
		},
		"extra step": {
			edit: func(fake *fakeKeycloak) {
				fake.flows["forms"].Steps = append(fake.flows["forms"].Steps,
					&fakeFlowStep{ID: "extra", Provider: "auth-otp-form", Requirement: "REQUIRED"})
			},
			want: "steps; wanted",
		},
		"settings on the password form": {
			edit: func(fake *fakeKeycloak) {
				fake.stepConfigs["pw"] = map[string]any{"id": "pw", "config": map[string]any{"a": "b"}}
				fake.flows["forms"].Steps[0].Config = "pw"
			},
			want: "other settings",
		},
		"passkey condition includes the passkey": {
			edit: func(fake *fakeKeycloak) {
				fake.stepConfigs["browser-conditional-credential"]["config"] = map[string]any{
					"credentials": "webauthn-passwordless", "included": "true",
				}
			},
			want: "other settings",
		},
		"other redirect settings": {
			edit: func(fake *fakeKeycloak) {
				fake.stepConfigs["r"] = map[string]any{"id": "r", "config": map[string]any{
					"defaultProvider": "company", "other": "x",
				}}
				fake.flows["browser"].Steps[2].Config = "r"
			},
			want: "other settings",
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			fake := newFakeKeycloak()
			tc.edit(fake)
			diff := defaultLoginDifference(listedSteps(fake, "browser"))
			if !strings.Contains(diff, tc.want) {
				t.Errorf("difference = %q, want it to contain %q", diff, tc.want)
			}
		})
	}
}

// The company login default is MEHO's own setting on "browser".
func TestDefaultLogin_CompanyLoginDefaultMatches(t *testing.T) {
	fake := newFakeKeycloak()
	seedBrowserRedirect(fake, "company")
	if diff := defaultLoginDifference(listedSteps(fake, "browser")); diff != "" {
		t.Errorf("difference = %q, want none", diff)
	}
}

// A changed "browser" flow keeps the console login it has; an unchanged
// one is bound (TestBootstrap_TouchFlowIsMadeAndBoundToTheConsole).
func TestBootstrap_TouchFlowNotBoundOverAChangedBrowserFlow(t *testing.T) {
	for name, passkeyCopy := range map[string]bool{"browser": false, "browser with passkeys": true} {
		t.Run(name, func(t *testing.T) {
			fake := newFakeKeycloak()
			if passkeyCopy {
				seedPasskeyCopy(fake)
				fake.flows[passkeyFlowAlias+" forms"].Steps[1].Requirement = "REQUIRED"
			}
			fake.flows["forms"].Steps[1].Requirement = "REQUIRED"
			console := seedConsoleClient(fake)

			res, _, err := runBootstrapOnce(t, fake, fullOpts())
			if err != nil {
				t.Fatalf("Bootstrap: %v", err)
			}
			if _, ok := fake.flows[touchFlowAlias]; !ok {
				t.Error("the touch flow was not made")
			}
			if got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]; got != "" {
				t.Errorf("console bound to %q over a changed browser flow", got)
			}
			if !slices.ContainsFunc(res.MapperEventsLog, func(e string) bool {
				return strings.HasPrefix(e, `[skip] client "meho-web" not bound`) &&
					strings.Contains(e, `"browser" is not Keycloak's default login`)
			}) {
				t.Errorf("events miss the not-bound line: %v", res.MapperEventsLog)
			}
		})
	}
}

// The default "browser" flow has the Organization step. Level 1 of the
// touch flow has none, so a realm with organizations turned on keeps its
// console login; with organizations off the console is bound.
func TestBootstrap_TouchFlowNotBoundWithOrganizationsOn(t *testing.T) {
	for _, organizations := range []bool{true, false} {
		t.Run(fmt.Sprintf("organizations %v", organizations), func(t *testing.T) {
			fake := newFakeKeycloak()
			fake.realm["organizationsEnabled"] = organizations
			console := seedConsoleClient(fake)

			res, _, err := runBootstrapOnce(t, fake, fullOpts())
			if err != nil {
				t.Fatalf("Bootstrap: %v", err)
			}
			got := fake.clients[console.ID].AuthenticationFlowBindingOverrides["browser"]
			if bound := got == fake.flows[touchFlowAlias].ID; bound == organizations {
				t.Errorf("console bound to %q with organizations %v", got, organizations)
			}
			skipped := slices.ContainsFunc(res.MapperEventsLog, func(e string) bool {
				return strings.HasPrefix(e, `[skip] client "meho-web" not bound`) &&
					strings.Contains(e, organizationsOnReason)
			})
			if skipped != organizations {
				t.Errorf("organizations %v: events = %v", organizations, res.MapperEventsLog)
			}
		})
	}
}

// Without the Organization step, organizations being on changes nothing.
func TestDefaultLogin_OrganizationsOnWithoutTheStepMatches(t *testing.T) {
	fake := newFakeKeycloak()
	steps, _ := listedSteps(fake, "browser")
	if diff := organizationsDifference(steps, true); diff != organizationsOnReason {
		t.Errorf("with the step: difference = %q", diff)
	}
	seedLoginSteps(fake, "browser", "")
	steps, _ = listedSteps(fake, "browser")
	if diff := organizationsDifference(steps, true); diff != "" {
		t.Errorf("without the step: difference = %q, want none", diff)
	}
}
