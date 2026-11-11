// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"net/url"
)

// The approval client's own browser sign-in flow (#5515, #6281). It is
// the same flow as in the chart's realm file
// (deploy/charts/meho/charts/keycloak/files/meho-realm.json): one
// sub-flow at approval level 2 with the level step, the password form,
// then the one-time code when the person has set one up. It has no
// other step, so every sign-in of `meho approvals approve` asks for the
// password. The level step gives the sign-in the approval level, named
// meho-approve in the realm's acr.loa.map; no other flow gives it.
const (
	approvalFlowAlias      = "meho approval sign-in level"
	approvalLevelFlowAlias = "meho approval sign-in level - level 2"
	approvalCodeFlowAlias  = "meho approval sign-in level - one-time code"
	approvalLevelStep      = "conditional-level-of-authentication"
	// approvalLevelName is the acr of a sign-in through the flow.
	approvalLevelName = "meho-approve"
	approvalLevel     = 2
	levelMapAttribute = "acr.loa.map"
)

// approvalLevelSettings are the settings of the level step: level 2,
// asked for again at every sign-in (maximum age 0).
func approvalLevelSettings() map[string]string {
	return map[string]string{"loa-condition-level": "2", "loa-max-age": "0"}
}

// approvalSubflowDescriptions are the descriptions of the sub-flows.
var approvalSubflowDescriptions = map[string]string{
	approvalLevelFlowAlias: "Level 2: the password, then the one-time code. Asked for again at every sign-in.",
	approvalCodeFlowAlias:  "Asks for the one-time code when the person has set one up.",
}

// approvalFlowStep is one step of the approval flow: an authenticator,
// or (with Subflow set) the one-time code sub-flow.
type approvalFlowStep struct {
	Parent      string
	Provider    string
	Subflow     string
	Requirement string
}

// approvalFlowSteps lists the steps in the order they are added.
// Keycloak gives each new step the next priority, so the order here is
// the order of the flow.
func approvalFlowSteps() []approvalFlowStep {
	return []approvalFlowStep{
		{Parent: approvalFlowAlias, Subflow: approvalLevelFlowAlias, Requirement: "CONDITIONAL"},
		{Parent: approvalLevelFlowAlias, Provider: approvalLevelStep, Requirement: "REQUIRED"},
		{Parent: approvalLevelFlowAlias, Provider: "auth-username-password-form", Requirement: "REQUIRED"},
		{Parent: approvalLevelFlowAlias, Subflow: approvalCodeFlowAlias, Requirement: "CONDITIONAL"},
		{Parent: approvalCodeFlowAlias, Provider: "conditional-user-configured", Requirement: "REQUIRED"},
		{Parent: approvalCodeFlowAlias, Provider: "auth-otp-form", Requirement: "REQUIRED"},
	}
}

// flowRep is the part of AuthenticationFlowRepresentation the bootstrap
// reads and sends.
type flowRep struct {
	ID          string `json:"id,omitempty"`
	Alias       string `json:"alias"`
	Description string `json:"description"`
	ProviderID  string `json:"providerId"`
	TopLevel    bool   `json:"topLevel"`
	BuiltIn     bool   `json:"builtIn"`
}

// listFlows GETs the realm's top-level authentication flows.
func (c *adminClient) listFlows(ctx context.Context) ([]flowRep, error) {
	status, body, err := c.do(ctx, http.MethodGet, "authentication/flows", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method: http.MethodGet, url: "authentication/flows",
			statusCode: status, body: string(body),
		}
	}
	var flows []flowRep
	if err := json.Unmarshal(body, &flows); err != nil {
		return nil, fmt.Errorf("decode listFlows: %w", err)
	}
	return flows, nil
}

// flowCall sends one request to the flow API and checks the status.
func (c *adminClient) flowCall(
	ctx context.Context, method, path string, body any, want int,
) ([]byte, error) {
	status, resp, err := c.do(ctx, method, path, body)
	if err != nil {
		return nil, err
	}
	if status != want {
		return nil, &errKeycloakAPI{
			method: method, url: path, statusCode: status, body: string(resp),
		}
	}
	return resp, nil
}

func flowExecutionsPath(alias string) string {
	return "authentication/flows/" + url.PathEscape(alias) + "/executions"
}

// wantApprovalFlowSteps is the requirement of every step of the approval
// flow, by step name (a sub-flow by its alias, a step by its provider).
func wantApprovalFlowSteps() map[string]string {
	want := map[string]string{}
	for _, s := range approvalFlowSteps() {
		name := s.Provider
		if s.Subflow != "" {
			name = s.Subflow
		}
		want[name] = s.Requirement
	}
	return want
}

// reconcileApprovalFlow makes the approval sign-in flow when the realm
// does not have it, finishes one that an earlier run left half-made,
// and returns its id. A flow with other steps than the ones here is
// kept as it is, and the id is "": the approval client is not bound to
// a flow that is not the one the bootstrap makes.
func reconcileApprovalFlow(ctx context.Context, c *adminClient) (string, []string, error) {
	id, err := c.findApprovalFlow(ctx)
	if err != nil {
		return "", nil, err
	}
	created := id == ""
	if created {
		top := flowRep{
			Alias:       approvalFlowAlias,
			Description: "Sign-in of `meho approvals approve`. It gives the approval level 2 (acr meho-approve) and nothing else gives it: the password form, then the one-time code when the person has set one up, every time. Provisioned by `meho admin keycloak bootstrap-clients`.",
			ProviderID:  "basic-flow",
			TopLevel:    true,
		}
		if _, err := c.flowCall(ctx, http.MethodPost, "authentication/flows", top, http.StatusCreated); err != nil {
			return "", nil, err
		}
		if c.dryRun {
			return dryRunNewID, []string{fmt.Sprintf("[created] login flow %q", approvalFlowAlias)}, nil
		}
		if id, err = c.findApprovalFlow(ctx); err != nil {
			return "", nil, err
		}
		if id == "" {
			return "", nil, fmt.Errorf("login flow %q not found after it was created", approvalFlowAlias)
		}
	}
	have, err := c.readApprovalFlowSteps(ctx)
	if err != nil {
		return "", nil, err
	}
	changed, err := finishApprovalFlow(ctx, c, have)
	if err != nil {
		return "", nil, err
	}
	requirements := stepRequirements(have)
	switch {
	case changed && c.dryRun:
		// The dry run sent no write: take the steps as a real run
		// leaves them, so an extra step is found the same way.
		maps.Copy(requirements, wantApprovalFlowSteps())
	case changed:
		if have, err = c.readApprovalFlowSteps(ctx); err != nil {
			return "", nil, err
		}
		requirements = stepRequirements(have)
	}
	var events []string
	switch {
	case created:
		events = append(events, fmt.Sprintf("[created] login flow %q", approvalFlowAlias))
	case changed:
		events = append(events, fmt.Sprintf("[updated] login flow %q: finished the steps an earlier run left out", approvalFlowAlias))
	}
	if !maps.Equal(requirements, wantApprovalFlowSteps()) {
		return "", append(events, otherStepsEvent()), nil
	}
	settings, err := ensureApprovalLevelSettings(ctx, c)
	if err != nil {
		return "", nil, err
	}
	switch settings {
	case "differs":
		return "", append(events, otherStepsEvent()), nil
	case "set":
		if len(events) == 0 {
			events = append(events, fmt.Sprintf("[updated] login flow %q: finished the steps an earlier run left out", approvalFlowAlias))
		}
	}
	if len(events) == 0 {
		events = append(events, fmt.Sprintf("[skip] login flow %q exists", approvalFlowAlias))
	}
	return id, events, nil
}

// findApprovalFlow returns the id of the approval flow, or "" when the
// realm does not have it.
func (c *adminClient) findApprovalFlow(ctx context.Context) (string, error) {
	flows, err := c.listFlows(ctx)
	if err != nil {
		return "", err
	}
	for _, f := range flows {
		if f.Alias == approvalFlowAlias {
			return f.ID, nil
		}
	}
	return "", nil
}

// readApprovalFlowSteps GETs the steps of the approval flow as Keycloak
// lists them: the steps of a sub-flow follow the sub-flow.
func (c *adminClient) readApprovalFlowSteps(ctx context.Context) ([]map[string]any, error) {
	body, err := c.flowCall(ctx, http.MethodGet, flowExecutionsPath(approvalFlowAlias), nil, http.StatusOK)
	if err != nil {
		return nil, err
	}
	var steps []map[string]any
	if err := json.Unmarshal(body, &steps); err != nil {
		return nil, fmt.Errorf("decode the steps of login flow %q: %w", approvalFlowAlias, err)
	}
	return steps, nil
}

// stepName is the name of a listed step: the alias of a sub-flow, else
// the step's provider.
func stepName(step map[string]any) string {
	if isFlow, _ := step["authenticationFlow"].(bool); isFlow {
		name, _ := step["displayName"].(string)
		return name
	}
	name, _ := step["providerId"].(string)
	return name
}

// stepRequirements maps the listed steps to their requirement by name.
func stepRequirements(steps []map[string]any) map[string]string {
	have := map[string]string{}
	for _, step := range steps {
		requirement, _ := step["requirement"].(string)
		have[stepName(step)] = requirement
	}
	return have
}

// finishApprovalFlow adds the steps the flow lacks and sets the
// requirement of every step; it reports whether anything changed. A
// run that stopped half-way leaves missing steps and steps that are
// still DISABLED (a new step starts so). Keycloak puts a new step after
// the existing ones, so the first steps a stopped run left get the rest
// in the right order.
func finishApprovalFlow(ctx context.Context, c *adminClient, have []map[string]any) (bool, error) {
	present := stepRequirements(have)
	added := false
	for _, s := range approvalFlowSteps() {
		name := s.Provider
		path := flowExecutionsPath(s.Parent) + "/execution"
		body := map[string]string{"provider": s.Provider}
		if s.Subflow != "" {
			name = s.Subflow
			path = flowExecutionsPath(s.Parent) + "/flow"
			body = map[string]string{
				"alias": s.Subflow, "type": "basic-flow",
				"description": approvalSubflowDescriptions[s.Subflow],
			}
		}
		if _, ok := present[name]; ok {
			continue
		}
		if _, err := c.flowCall(ctx, http.MethodPost, path, body, http.StatusCreated); err != nil {
			return false, err
		}
		added = true
	}
	set, err := setApprovalFlowRequirements(ctx, c)
	if err != nil {
		return false, err
	}
	return added || set, nil
}

// setApprovalFlowRequirements sets the requirement of every step and
// reports whether any changed. A new step starts as DISABLED.
func setApprovalFlowRequirements(ctx context.Context, c *adminClient) (bool, error) {
	want := wantApprovalFlowSteps()
	steps, err := c.readApprovalFlowSteps(ctx)
	if err != nil {
		return false, err
	}
	changed := false
	for _, step := range steps {
		requirement, ok := want[stepName(step)]
		if !ok || step["requirement"] == requirement {
			continue
		}
		step["requirement"] = requirement
		if _, err := c.flowCall(ctx, http.MethodPut, flowExecutionsPath(approvalFlowAlias), step, http.StatusNoContent); err != nil {
			return false, err
		}
		changed = true
	}
	return changed, nil
}

func otherStepsEvent() string {
	return fmt.Sprintf(
		"[skip] login flow %q has other steps than the bootstrap makes; the approval client is not bound to it",
		approvalFlowAlias)
}

// ensureApprovalLevelSettings gives the level step of the approval flow
// its settings when it has none. It returns "kept", "set", or "differs"
// when the step has settings with other values (they are not changed:
// the client is then not bound to the flow). A dry run that would make
// the flow has no level step to read yet: that counts as "set".
func ensureApprovalLevelSettings(ctx context.Context, c *adminClient) (string, error) {
	steps, err := c.readApprovalFlowSteps(ctx)
	if err != nil {
		return "", err
	}
	var step map[string]any
	for _, s := range steps {
		if stepName(s) == approvalLevelStep {
			step = s
		}
	}
	if step == nil {
		if c.dryRun {
			return "set", nil
		}
		return "differs", nil
	}
	if configID, _ := step["authenticationConfig"].(string); configID != "" {
		body, err := c.flowCall(ctx, http.MethodGet, "authentication/config/"+url.PathEscape(configID), nil, http.StatusOK)
		if err != nil {
			return "", err
		}
		var have struct {
			Config map[string]string `json:"config"`
		}
		if err := json.Unmarshal(body, &have); err != nil {
			return "", fmt.Errorf("decode the settings of the level step: %w", err)
		}
		for key, value := range approvalLevelSettings() {
			if have.Config[key] != value {
				return "differs", nil
			}
		}
		return "kept", nil
	}
	stepID, _ := step["id"].(string)
	body := map[string]any{"alias": approvalLevelFlowAlias, "config": approvalLevelSettings()}
	path := "authentication/executions/" + url.PathEscape(stepID) + "/config"
	if _, err := c.flowCall(ctx, http.MethodPost, path, body, http.StatusCreated); err != nil {
		return "", err
	}
	return "set", nil
}

// ensureApprovalLevel names level 2 meho-approve in the realm attribute
// acr.loa.map, next to the names the realm has (#6281). Keycloak puts
// the name in the acr claim of a sign-in that reached the level. The
// whole attribute map is written back: Keycloak drops the realm
// attributes a write leaves out.
func ensureApprovalLevel(ctx context.Context, c *adminClient) ([]string, error) {
	body, err := c.flowCall(ctx, http.MethodGet, "", nil, http.StatusOK)
	if err != nil {
		return nil, err
	}
	var realm struct {
		Attributes map[string]string `json:"attributes"`
	}
	if err := json.Unmarshal(body, &realm); err != nil {
		return nil, fmt.Errorf("decode the realm: %w", err)
	}
	if realm.Attributes == nil {
		realm.Attributes = map[string]string{}
	}
	levels := map[string]any{}
	if raw := realm.Attributes[levelMapAttribute]; raw != "" {
		if err := json.Unmarshal([]byte(raw), &levels); err != nil {
			return nil, fmt.Errorf("the realm attribute %s is not a JSON object; fix it in the Keycloak admin console and run again", levelMapAttribute)
		}
	}
	if level, ok := levels[approvalLevelName].(float64); ok && level == approvalLevel {
		return []string{fmt.Sprintf("[skip] sign-in level %q exists", approvalLevelName)}, nil
	}
	levels[approvalLevelName] = approvalLevel
	encoded, err := json.Marshal(levels)
	if err != nil {
		return nil, err
	}
	realm.Attributes[levelMapAttribute] = string(encoded)
	if _, err := c.flowCall(ctx, http.MethodPut, "", map[string]any{"attributes": realm.Attributes}, http.StatusNoContent); err != nil {
		return nil, err
	}
	return []string{fmt.Sprintf("[updated] sign-in level %q: level %d of the realm", approvalLevelName, approvalLevel)}, nil
}
