// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"fmt"
	"strings"
)

// Level 1 of the touch flow is a fixed copy of the login that Keycloak
// puts into every new realm as "browser". An admin can change that flow,
// for example make the one-time code required for everyone; binding the
// touch flow would then drop the change from the console login. So the
// console is bound only when "browser" still has the default steps,
// requirements and settings.
//
// The default shapes come from Keycloak's DefaultAuthenticationFlows.
// browserFlow (26.0 to 26.8) and the migration that adds the organization
// step (26.6.1). The same list is in
// backend/src/meho_backplane/connectors/keycloak/ops_authflow_default_login.py.

const defaultLoginFlow = "browser"

// loginRow is one step of a login flow as Keycloak lists it.
type loginRow struct {
	Level       int
	Name        string
	Requirement string
}

var (
	defaultLoginHead = []loginRow{
		{0, "auth-cookie", "ALTERNATIVE"},
		{0, "auth-spnego", "DISABLED"},
		{0, redirectorType, "ALTERNATIVE"},
	}
	defaultLoginOrganization = []loginRow{
		{0, "Organization", "ALTERNATIVE"},
		{1, "Browser - Conditional Organization", "CONDITIONAL"},
		{2, "conditional-user-configured", "REQUIRED"},
		{2, "organization", "ALTERNATIVE"},
	}
	defaultLoginForms = []loginRow{
		{0, "forms", "ALTERNATIVE"},
		{1, "auth-username-password-form", "REQUIRED"},
	}
	// Keycloak 26.0 to 26.2.
	defaultLoginOTP = []loginRow{
		{1, "Browser - Conditional OTP", "CONDITIONAL"},
		{2, "conditional-user-configured", "REQUIRED"},
		{2, "auth-otp-form", "REQUIRED"},
	}
)

// defaultLoginTwoFactor is the one-time code sub-flow of Keycloak 26.3
// and newer. The passkey condition (26.4), WebAuthn and recovery codes
// are there only when their feature is on.
func defaultLoginTwoFactor(passkey, webauthn, recovery bool) []loginRow {
	rows := []loginRow{
		{1, "Browser - Conditional 2FA", "CONDITIONAL"},
		{2, "conditional-user-configured", "REQUIRED"},
	}
	if passkey {
		rows = append(rows, loginRow{2, passkeyConditionType, "REQUIRED"})
	}
	rows = append(rows, loginRow{2, "auth-otp-form", "ALTERNATIVE"})
	if webauthn {
		rows = append(rows, loginRow{2, "webauthn-authenticator", "DISABLED"})
	}
	if recovery {
		rows = append(rows, loginRow{2, "auth-recovery-authn-code-form", "DISABLED"})
	}
	return rows
}

// defaultLoginShapes lists every shape Keycloak 26 gives the "browser"
// flow of a new or upgraded realm.
func defaultLoginShapes() [][]loginRow {
	codes := [][]loginRow{defaultLoginOTP}
	for _, passkey := range []bool{true, false} {
		for _, webauthn := range []bool{true, false} {
			for _, recovery := range []bool{true, false} {
				codes = append(codes, defaultLoginTwoFactor(passkey, webauthn, recovery))
			}
		}
	}
	var shapes [][]loginRow
	for _, organization := range [][]loginRow{defaultLoginOrganization, nil} {
		for _, code := range codes {
			var shape []loginRow
			shape = append(shape, defaultLoginHead...)
			shape = append(shape, organization...)
			shape = append(shape, defaultLoginForms...)
			shape = append(shape, code...)
			shapes = append(shapes, shape)
		}
	}
	return shapes
}

// defaultLoginSettingsOK says whether a step of the default login may
// have these settings. The company login step may have a default company
// login (MEHO's company login setup sets it, and the touch flow copies
// it); the passkey condition has the passkey settings; no other step has
// settings.
func defaultLoginSettingsOK(name string, config map[string]any) bool {
	switch name {
	case redirectorType:
		for k := range config {
			if k != "defaultProvider" {
				return false
			}
		}
		return true
	case passkeyConditionType:
		if config == nil || config["credentials"] != "webauthn-passwordless" {
			return false
		}
		for k := range config {
			if k != "credentials" && k != "included" {
				return false
			}
		}
		included, ok := config["included"].(string)
		return !ok || strings.EqualFold(strings.TrimSpace(included), "false")
	default:
		return config == nil
	}
}

// firstLoginDifference says how far steps match shape, and how they
// differ ("" when they match). settings holds the settings of each step.
func firstLoginDifference(steps []map[string]any, settings []map[string]any, shape []loginRow) (int, string) {
	for i, want := range shape {
		if i >= len(steps) {
			return i, fmt.Sprintf("step %d (%q) is missing", i+1, want.Name)
		}
		level, _ := steps[i]["level"].(float64)
		requirement, _ := steps[i]["requirement"].(string)
		have := loginRow{int(level), stepName(steps[i]), requirement}
		if _, isNumber := steps[i]["level"].(float64); !isNumber || have != want {
			return i, fmt.Sprintf("step %d is %q (%s) at level %v; wanted %q (%s) at level %d",
				i+1, have.Name, have.Requirement, steps[i]["level"], want.Name, want.Requirement, want.Level)
		}
		if !defaultLoginSettingsOK(want.Name, settings[i]) {
			return i, fmt.Sprintf("step %d (%q) has other settings", i+1, want.Name)
		}
	}
	if len(steps) > len(shape) {
		return len(shape), fmt.Sprintf("it has %d steps; wanted %d", len(steps), len(shape))
	}
	return len(shape), ""
}

// defaultLoginDifference says how steps differ from Keycloak's default
// login, or returns "" when they match. The difference named is the one
// against the default shape that matches the most steps from the top.
func defaultLoginDifference(steps []map[string]any, settings []map[string]any) string {
	best, reason := -1, ""
	for _, shape := range defaultLoginShapes() {
		reached, diff := firstLoginDifference(steps, settings, shape)
		if diff == "" {
			return ""
		}
		if reached > best {
			best, reason = reached, diff
		}
	}
	return reason
}

// readDefaultLoginDifference reads the realm's "browser" flow and the
// settings of its steps, then compares them (defaultLoginDifference).
func readDefaultLoginDifference(ctx context.Context, c *adminClient) (string, error) {
	steps, err := readFlowSteps(ctx, c, defaultLoginFlow)
	if err != nil {
		return "", err
	}
	settings := make([]map[string]any, len(steps))
	for i, step := range steps {
		if settings[i], err = stepSettings(ctx, c, step); err != nil {
			return "", err
		}
	}
	return defaultLoginDifference(steps, settings), nil
}
