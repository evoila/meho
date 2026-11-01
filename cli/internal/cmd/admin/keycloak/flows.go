// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
)

// The approval client's own browser sign-in flow (#5515). It is the
// same flow as in the chart's realm file
// (deploy/charts/meho/charts/keycloak/files/meho-realm.json): the
// password form, then the one-time code when the person has set one
// up. It has no other step, so every sign-in of `meho approvals
// approve` asks for the password.
const (
	approvalFlowAlias     = "meho approval sign-in"
	approvalCodeFlowAlias = "meho approval sign-in - one-time code"
)

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
		{Parent: approvalFlowAlias, Provider: "auth-username-password-form", Requirement: "REQUIRED"},
		{Parent: approvalFlowAlias, Subflow: approvalCodeFlowAlias, Requirement: "CONDITIONAL"},
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

// reconcileApprovalFlow makes the approval sign-in flow when the realm
// does not have it, and returns its id. An existing flow is kept as it
// is.
func reconcileApprovalFlow(ctx context.Context, c *adminClient) (string, []string, error) {
	flows, err := c.listFlows(ctx)
	if err != nil {
		return "", nil, err
	}
	for _, f := range flows {
		if f.Alias == approvalFlowAlias {
			return f.ID, []string{fmt.Sprintf("[skip] login flow %q exists", approvalFlowAlias)}, nil
		}
	}
	top := flowRep{
		Alias:       approvalFlowAlias,
		Description: "Sign-in of `meho approvals approve`: always asks for the password, and for the one-time code when the person has set one up. Provisioned by `meho admin keycloak bootstrap-clients`.",
		ProviderID:  "basic-flow",
		TopLevel:    true,
	}
	if _, err := c.flowCall(ctx, http.MethodPost, "authentication/flows", top, http.StatusCreated); err != nil {
		return "", nil, err
	}
	for _, s := range approvalFlowSteps() {
		path := flowExecutionsPath(s.Parent) + "/execution"
		body := map[string]string{"provider": s.Provider}
		if s.Subflow != "" {
			path = flowExecutionsPath(s.Parent) + "/flow"
			body = map[string]string{
				"alias": s.Subflow, "type": "basic-flow",
				"description": "Asks for the one-time code when the person has set one up.",
			}
		}
		if _, err := c.flowCall(ctx, http.MethodPost, path, body, http.StatusCreated); err != nil {
			return "", nil, err
		}
	}
	if err := setApprovalFlowRequirements(ctx, c); err != nil {
		return "", nil, err
	}
	flows, err = c.listFlows(ctx)
	if err != nil {
		return "", nil, err
	}
	for _, f := range flows {
		if f.Alias == approvalFlowAlias {
			return f.ID, []string{fmt.Sprintf("[created] login flow %q", approvalFlowAlias)}, nil
		}
	}
	return "", nil, fmt.Errorf("login flow %q not found after it was created", approvalFlowAlias)
}

// setApprovalFlowRequirements sets the requirement of every step. A new
// step starts as DISABLED.
func setApprovalFlowRequirements(ctx context.Context, c *adminClient) error {
	want := map[string]string{}
	for _, s := range approvalFlowSteps() {
		name := s.Provider
		if s.Subflow != "" {
			name = s.Subflow
		}
		want[name] = s.Requirement
	}
	path := flowExecutionsPath(approvalFlowAlias)
	body, err := c.flowCall(ctx, http.MethodGet, path, nil, http.StatusOK)
	if err != nil {
		return err
	}
	var steps []map[string]any
	if err := json.Unmarshal(body, &steps); err != nil {
		return fmt.Errorf("decode the steps of login flow %q: %w", approvalFlowAlias, err)
	}
	for _, step := range steps {
		name, _ := step["providerId"].(string)
		if isFlow, _ := step["authenticationFlow"].(bool); isFlow {
			name, _ = step["displayName"].(string)
		}
		requirement, ok := want[name]
		if !ok || step["requirement"] == requirement {
			continue
		}
		step["requirement"] = requirement
		if _, err := c.flowCall(ctx, http.MethodPut, path, step, http.StatusNoContent); err != nil {
			return err
		}
	}
	return nil
}
