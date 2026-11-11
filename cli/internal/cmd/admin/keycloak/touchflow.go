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
	"slices"
	"strconv"
	"strings"
)

// The console's login flow with the passkey touch level (#6213). It is
// the same flow as in the chart's realm file
// (deploy/charts/meho/charts/keycloak/files/meho-realm.json). Level 1
// is the normal login. Level 3 (acr_values=meho-touch) asks for a
// passkey with user verification, every time (maximum age 0).
const (
	touchFlowAlias = "meho approval touch"
	touchACR       = "meho-touch"
	touchLevel     = 3
	loaMapKey      = "acr.loa.map"
	amrMapperType  = "oidc-amr-mapper"
	// The realm's passkey policy must ask for user verification, or a
	// touch passes without a PIN or a fingerprint.
	passkeyUVPolicy = "webAuthnPolicyPasswordlessUserVerificationRequirement"
	redirectorType  = "identity-provider-redirector"
	// The name core/company_login.py gives the touch flow's redirect settings.
	touchRedirectAlias = "meho-company-login-redirect-touch"
)

// touchStep is one step of the touch flow, in the order Keycloak lists
// the steps: the steps of a sub-flow follow it. A step with Subflow set
// is a sub-flow; a step with ConfigAlias has settings.
type touchStep struct {
	Level       int
	Parent      string
	Provider    string
	Subflow     string
	Requirement string
	Description string
	ConfigAlias string
	Config      map[string]string
}

func (s touchStep) name() string {
	if s.Subflow != "" {
		return s.Subflow
	}
	return s.Provider
}

// touchFlowSteps lists the steps in the order they are added. Keycloak
// puts a new step after the existing ones of its parent, so this order
// gives every parent its steps in the right order.
func touchFlowSteps() []touchStep {
	t := touchFlowAlias
	return []touchStep{
		{Level: 0, Parent: t, Provider: "auth-cookie", Requirement: "ALTERNATIVE"},
		{Level: 0, Parent: t, Subflow: t + " - levels", Requirement: "ALTERNATIVE",
			Description: "Level 1, then level 3 when it is asked for."},
		{Level: 1, Parent: t + " - levels", Subflow: t + " - level 1", Requirement: "CONDITIONAL",
			Description: "The normal login: company login or password, then the one-time code when the person has one."},
		{Level: 2, Parent: t + " - level 1", Provider: "conditional-level-of-authentication", Requirement: "REQUIRED",
			ConfigAlias: t + " - level 1",
			Config:      map[string]string{"loa-condition-level": "1", "loa-max-age": "36000"}},
		{Level: 2, Parent: t + " - level 1", Subflow: t + " - sign-in", Requirement: "REQUIRED",
			Description: "Company login, or the password form."},
		{Level: 3, Parent: t + " - sign-in", Provider: "identity-provider-redirector", Requirement: "ALTERNATIVE"},
		{Level: 3, Parent: t + " - sign-in", Subflow: t + " - password", Requirement: "ALTERNATIVE",
			Description: "The password form (it also takes a passkey), then the one-time code."},
		{Level: 4, Parent: t + " - password", Provider: "auth-username-password-form", Requirement: "REQUIRED"},
		{Level: 4, Parent: t + " - password", Subflow: t + " - one-time code", Requirement: "CONDITIONAL",
			Description: "Asks for the one-time code when the person has set one up and did not use a passkey."},
		{Level: 5, Parent: t + " - one-time code", Provider: "conditional-user-configured", Requirement: "REQUIRED"},
		{Level: 5, Parent: t + " - one-time code", Provider: "conditional-credential", Requirement: "REQUIRED",
			ConfigAlias: t + " - skip code after passkey",
			Config:      map[string]string{"credentials": "webauthn-passwordless", "included": "false"}},
		{Level: 5, Parent: t + " - one-time code", Provider: "auth-otp-form", Requirement: "REQUIRED"},
		{Level: 1, Parent: t + " - levels", Subflow: t + " - level 3", Requirement: "CONDITIONAL",
			Description: "The passkey touch: a passkey with user verification, every time."},
		{Level: 2, Parent: t + " - level 3", Provider: "conditional-level-of-authentication", Requirement: "REQUIRED",
			ConfigAlias: t + " - level 3",
			Config:      map[string]string{"loa-condition-level": "3", "loa-max-age": "0"}},
		{Level: 2, Parent: t + " - level 3", Provider: "webauthn-authenticator-passwordless", Requirement: "REQUIRED",
			ConfigAlias: t + " - passkey",
			Config:      map[string]string{"default.reference.value": "hwk", "default.reference.maxAge": "120"}},
	}
}

// amrMapper writes the passkey step's value into `amr`.
func amrMapper() protocolMapperRep {
	return protocolMapperRep{
		Name: "amr", Protocol: "openid-connect", ProtocolMapper: amrMapperType,
		Config: map[string]string{
			"id.token.claim":            "true",
			"access.token.claim":        "true",
			"introspection.token.claim": "true",
		},
	}
}

// reconcileTouch makes the touch flow when the realm lacks it, maps
// meho-touch to level 3, and binds the flow to the console client with
// the `amr` mapper and the company login default of the realm's browser
// flow. A flow with another shape, or a realm that maps meho-touch to
// another level, is kept as it is and no client is bound. A realm whose
// passkey policy does not ask for user verification is not changed.
func reconcileTouch(ctx context.Context, c *adminClient, webClientID string) ([]string, error) {
	realm, err := readRealm(ctx, c)
	if err != nil {
		return nil, err
	}
	if have, _ := realm[passkeyUVPolicy].(string); have != "required" {
		return []string{fmt.Sprintf(
			"[skip] login flow %q not made: the realm's passkey policy has %s = %q, not \"required\", "+
				"so a touch could pass without a PIN or fingerprint; set it and run again",
			touchFlowAlias, passkeyUVPolicy, have)}, nil
	}
	flowID, events, err := reconcileTouchFlow(ctx, c)
	if err != nil || flowID == "" {
		return events, err
	}
	loaEvents, ok, err := reconcileTouchLevel(ctx, c)
	events = append(events, loaEvents...)
	if err != nil || !ok {
		return events, err
	}
	bindEvents, err := bindTouchFlow(ctx, c, webClientID, flowID)
	return append(events, bindEvents...), err
}

func findFlowID(ctx context.Context, c *adminClient, alias string) (string, error) {
	flows, err := c.listFlows(ctx)
	if err != nil {
		return "", err
	}
	for _, f := range flows {
		if f.Alias == alias {
			return f.ID, nil
		}
	}
	return "", nil
}

func readTouchSteps(ctx context.Context, c *adminClient) ([]map[string]any, error) {
	body, err := c.flowCall(ctx, http.MethodGet, flowExecutionsPath(touchFlowAlias), nil, http.StatusOK)
	if err != nil {
		return nil, err
	}
	var steps []map[string]any
	if err := json.Unmarshal(body, &steps); err != nil {
		return nil, fmt.Errorf("decode the steps of login flow %q: %w", touchFlowAlias, err)
	}
	return steps, nil
}

// touchShapeDifference names the first listed step that is not the
// wanted one, or returns "". With requirements false only the level and
// the name count (a new step starts DISABLED).
func touchShapeDifference(have []map[string]any, requirements bool) string {
	want := touchFlowSteps()
	for i, w := range want {
		if i >= len(have) {
			return fmt.Sprintf("step %d (%q) is missing", i+1, w.name())
		}
		level, _ := have[i]["level"].(float64)
		requirement, _ := have[i]["requirement"].(string)
		if int(level) != w.Level || stepName(have[i]) != w.name() ||
			(requirements && requirement != w.Requirement) {
			return fmt.Sprintf("step %d is %q (%s); wanted %q (%s)",
				i+1, stepName(have[i]), requirement, w.name(), w.Requirement)
		}
	}
	if len(have) > len(want) {
		return fmt.Sprintf("the flow has %d steps; wanted %d", len(have), len(want))
	}
	return ""
}

// touchConfigDifference names the first step whose settings are not the
// wanted ones, or returns "".
func touchConfigDifference(ctx context.Context, c *adminClient, have []map[string]any) (string, error) {
	for i, w := range touchFlowSteps() {
		if w.ConfigAlias == "" {
			continue
		}
		configID, _ := have[i]["authenticationConfig"].(string)
		if configID == "" {
			return fmt.Sprintf("step %d (%q) has no settings", i+1, w.name()), nil
		}
		body, err := c.flowCall(ctx, http.MethodGet,
			"authentication/config/"+url.PathEscape(configID), nil, http.StatusOK)
		if err != nil {
			return "", err
		}
		var rep struct {
			Config map[string]string `json:"config"`
		}
		if err := json.Unmarshal(body, &rep); err != nil {
			return "", fmt.Errorf("decode the settings of step %d: %w", i+1, err)
		}
		if !maps.Equal(rep.Config, w.Config) {
			return fmt.Sprintf("step %d (%q) has other settings", i+1, w.name()), nil
		}
	}
	return "", nil
}

// missingTouchProviders names the steps of the flow this Keycloak does
// not have. Keycloak 26.4 added `conditional-credential`; an older one
// would refuse it half-way through making the flow.
func missingTouchProviders(ctx context.Context, c *adminClient) ([]string, error) {
	body, err := c.flowCall(ctx, http.MethodGet, "authentication/authenticator-providers", nil, http.StatusOK)
	if err != nil {
		return nil, err
	}
	var providers []struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(body, &providers); err != nil {
		return nil, fmt.Errorf("decode the authenticator providers: %w", err)
	}
	have := map[string]bool{}
	for _, p := range providers {
		have[p.ID] = true
	}
	var missing []string
	for _, s := range touchFlowSteps() {
		if s.Provider != "" && !have[s.Provider] && !slices.Contains(missing, s.Provider) {
			missing = append(missing, s.Provider)
		}
	}
	return missing, nil
}

// reconcileTouchFlow returns the flow's id, or "" when the realm has a
// flow with that name and another shape.
func reconcileTouchFlow(ctx context.Context, c *adminClient) (string, []string, error) {
	id, err := findFlowID(ctx, c, touchFlowAlias)
	if err != nil {
		return "", nil, err
	}
	if id == "" {
		missing, err := missingTouchProviders(ctx, c)
		if err != nil {
			return "", nil, err
		}
		if len(missing) > 0 {
			return "", []string{fmt.Sprintf(
				"[skip] login flow %q not made: this Keycloak has no step %s (Keycloak 26.4 or newer has it); "+
					"the console client is not bound", touchFlowAlias, strings.Join(missing, ", "))}, nil
		}
		return makeTouchFlow(ctx, c)
	}
	have, err := readTouchSteps(ctx, c)
	if err != nil {
		return "", nil, err
	}
	diff := touchShapeDifference(have, true)
	if diff == "" {
		if diff, err = touchConfigDifference(ctx, c, have); err != nil {
			return "", nil, err
		}
	}
	if diff != "" {
		return "", []string{fmt.Sprintf(
			"[skip] login flow %q has another shape (%s); the console client is not bound to it. "+
				"Fix it by hand, or delete it and run again", touchFlowAlias, diff)}, nil
	}
	return id, []string{fmt.Sprintf("[skip] login flow %q exists", touchFlowAlias)}, nil
}

// makeTouchFlow makes the flow, its steps, their requirements and their
// settings, and checks them.
func makeTouchFlow(ctx context.Context, c *adminClient) (string, []string, error) {
	created := []string{fmt.Sprintf("[created] login flow %q (passkey touch level %s)", touchFlowAlias, touchACR)}
	top := flowRep{
		Alias:       touchFlowAlias,
		Description: "Console login of meho-web. Level 1 is the normal login. Level 3 (acr meho-touch) asks for a passkey touch every time. Provisioned by `meho admin keycloak bootstrap-clients`.",
		ProviderID:  "basic-flow",
		TopLevel:    true,
	}
	if _, err := c.flowCall(ctx, http.MethodPost, "authentication/flows", top, http.StatusCreated); err != nil {
		return "", nil, err
	}
	if c.dryRun {
		return dryRunNewID, created, nil
	}
	for _, s := range touchFlowSteps() {
		path := flowExecutionsPath(s.Parent) + "/execution"
		var body any = map[string]string{"provider": s.Provider}
		if s.Subflow != "" {
			path = flowExecutionsPath(s.Parent) + "/flow"
			body = map[string]string{"alias": s.Subflow, "type": "basic-flow", "description": s.Description}
		}
		if _, err := c.flowCall(ctx, http.MethodPost, path, body, http.StatusCreated); err != nil {
			return "", nil, err
		}
	}
	have, err := readTouchSteps(ctx, c)
	if err != nil {
		return "", nil, err
	}
	if diff := touchShapeDifference(have, false); diff != "" {
		return "", nil, fmt.Errorf("login flow %q was made, but its steps read back wrong: %s", touchFlowAlias, diff)
	}
	// Steps are matched by place, not by name: the level condition is in
	// the flow twice. A new step starts DISABLED.
	for i, w := range touchFlowSteps() {
		step := have[i]
		if step["requirement"] != w.Requirement {
			step["requirement"] = w.Requirement
			if _, err := c.flowCall(ctx, http.MethodPut, flowExecutionsPath(touchFlowAlias), step, http.StatusNoContent); err != nil {
				return "", nil, err
			}
		}
		if w.ConfigAlias != "" {
			stepID, _ := step["id"].(string)
			config := map[string]any{"alias": w.ConfigAlias, "config": w.Config}
			if _, err := c.flowCall(ctx, http.MethodPost,
				"authentication/executions/"+url.PathEscape(stepID)+"/config", config, http.StatusCreated); err != nil {
				return "", nil, err
			}
		}
	}
	if have, err = readTouchSteps(ctx, c); err != nil {
		return "", nil, err
	}
	diff := touchShapeDifference(have, true)
	if diff == "" {
		if diff, err = touchConfigDifference(ctx, c, have); err != nil {
			return "", nil, err
		}
	}
	if diff != "" {
		return "", nil, fmt.Errorf("login flow %q was made, but it reads back wrong: %s", touchFlowAlias, diff)
	}
	id, err := findFlowID(ctx, c, touchFlowAlias)
	if err != nil {
		return "", nil, err
	}
	if id == "" {
		return "", nil, fmt.Errorf("login flow %q not found after it was created", touchFlowAlias)
	}
	return id, created, nil
}

// reconcileTouchLevel maps meho-touch to level 3 in the realm, next to
// the names it maps already. ok is false when the realm maps meho-touch
// to another level: then nothing is changed and no client is bound.
func reconcileTouchLevel(ctx context.Context, c *adminClient) ([]string, bool, error) {
	realm, err := readRealm(ctx, c)
	if err != nil {
		return nil, false, err
	}
	attributes, _ := realm["attributes"].(map[string]any)
	if attributes == nil {
		attributes = map[string]any{}
	}
	levels := map[string]any{}
	if raw, _ := attributes[loaMapKey].(string); raw != "" {
		if err := json.Unmarshal([]byte(raw), &levels); err != nil {
			return []string{fmt.Sprintf(
				"[skip] realm attribute %s is not a JSON map; the console client is not bound", loaMapKey)}, false, nil
		}
	}
	if have, ok := levels[touchACR]; ok {
		if fmt.Sprint(have) == strconv.Itoa(touchLevel) {
			return []string{fmt.Sprintf("[skip] realm maps %s to level %d", touchACR, touchLevel)}, true, nil
		}
		return []string{fmt.Sprintf(
			"[skip] realm maps %s to level %v, not %d; the console client is not bound",
			touchACR, have, touchLevel)}, false, nil
	}
	levels[touchACR] = touchLevel
	text, err := json.Marshal(levels)
	if err != nil {
		return nil, false, err
	}
	attributes[loaMapKey] = string(text)
	realm["attributes"] = attributes
	status, body, err := c.do(ctx, http.MethodPut, "", realm)
	if err != nil {
		return nil, false, err
	}
	if status != http.StatusNoContent {
		return nil, false, &errKeycloakAPI{method: http.MethodPut, url: "realm", statusCode: status, body: string(body)}
	}
	return []string{fmt.Sprintf("[updated] realm maps %s to level %d", touchACR, touchLevel)}, true, nil
}

// bindTouchFlow gives the console client the `amr` mapper and binds the
// flow as its browser flow. A realm without the client is skipped: the
// bootstrap does not make the console client.
func bindTouchFlow(ctx context.Context, c *adminClient, clientID, flowID string) ([]string, error) {
	client, err := c.findClient(ctx, clientID)
	if err != nil {
		return nil, err
	}
	if client == nil {
		return []string{fmt.Sprintf(
			"[skip] console client %q not found; bind it to login flow %q by hand", clientID, touchFlowAlias)}, nil
	}
	events, err := reconcileAMRMapper(ctx, c, client.ID, clientID)
	if err != nil {
		return events, err
	}
	redirectEvents, err := copyTouchRedirect(ctx, c, flowID)
	events = append(events, redirectEvents...)
	if err != nil {
		return events, err
	}
	if client.AuthenticationFlowBindingOverrides["browser"] == flowID {
		return append(events, fmt.Sprintf("[skip] client %q uses login flow %q", clientID, touchFlowAlias)), nil
	}
	overrides := maps.Clone(client.AuthenticationFlowBindingOverrides)
	if overrides == nil {
		overrides = map[string]string{}
	}
	overrides["browser"] = flowID
	client.AuthenticationFlowBindingOverrides = overrides
	if err := c.updateClient(ctx, client.ID, client); err != nil {
		return nil, err
	}
	return append(events, fmt.Sprintf("[updated] client %q uses login flow %q", clientID, touchFlowAlias)), nil
}

func readRealm(ctx context.Context, c *adminClient) (map[string]any, error) {
	status, body, err := c.do(ctx, http.MethodGet, "", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{method: http.MethodGet, url: "realm", statusCode: status, body: string(body)}
	}
	var realm map[string]any
	if err := json.Unmarshal(body, &realm); err != nil {
		return nil, fmt.Errorf("decode the realm: %w", err)
	}
	return realm, nil
}

// amrMapperOK says whether m writes `amr` into every token the mapper
// of the bootstrap writes it into.
func amrMapperOK(m protocolMapperRep) bool {
	for k, v := range amrMapper().Config {
		if m.Config[k] != v {
			return false
		}
	}
	return true
}

// reconcileAMRMapper gives the console client an `amr` mapper that writes
// into the ID token, the access token and the introspection. A mapper
// with one of those off gets them turned on, and is read back.
func reconcileAMRMapper(ctx context.Context, c *adminClient, clientUUID, clientID string) ([]string, error) {
	mappers, err := c.listClientMappers(ctx, clientUUID)
	if err != nil {
		return nil, err
	}
	var amr []protocolMapperRep
	for _, m := range mappers {
		if m.ProtocolMapper == amrMapperType {
			amr = append(amr, m)
		}
	}
	if slices.ContainsFunc(amr, amrMapperOK) {
		return nil, nil
	}
	if len(amr) == 0 {
		if err := c.createClientMapper(ctx, clientUUID, amrMapper()); err != nil {
			return nil, err
		}
		return []string{fmt.Sprintf("[added] mapper amr on client %q", clientID)}, nil
	}
	fixed := amr[0]
	fixed.Config = maps.Clone(fixed.Config)
	if fixed.Config == nil {
		fixed.Config = map[string]string{}
	}
	maps.Copy(fixed.Config, amrMapper().Config)
	if err := c.updateClientMapper(ctx, clientUUID, fixed.ID, fixed); err != nil {
		return nil, err
	}
	event := fmt.Sprintf("[updated] mapper %s on client %q writes amr into every token", fixed.Name, clientID)
	if c.dryRun {
		return []string{event}, nil
	}
	if mappers, err = c.listClientMappers(ctx, clientUUID); err != nil {
		return nil, err
	}
	if !slices.ContainsFunc(mappers, func(m protocolMapperRep) bool { return m.ID == fixed.ID && amrMapperOK(m) }) {
		return nil, fmt.Errorf("mapper %s on client %q does not read back with its token settings on; "+
			"the client is not bound to login flow %q", fixed.Name, clientID, touchFlowAlias)
	}
	return []string{event}, nil
}

// readAuthConfig reads the settings of a flow step by their id.
func readAuthConfig(ctx context.Context, c *adminClient, id string) (map[string]any, error) {
	body, err := c.flowCall(ctx, http.MethodGet, "authentication/config/"+url.PathEscape(id), nil, http.StatusOK)
	if err != nil {
		return nil, err
	}
	var rep map[string]any
	if err := json.Unmarshal(body, &rep); err != nil {
		return nil, fmt.Errorf("decode the step settings %q: %w", id, err)
	}
	return rep, nil
}

// redirectDefault returns the redirect step of steps, its settings, and
// their company login default ("" when there is none).
func redirectDefault(ctx context.Context, c *adminClient, steps []map[string]any) (
	map[string]any, map[string]any, string, error,
) {
	i := slices.IndexFunc(steps, func(s map[string]any) bool { return s["providerId"] == redirectorType })
	if i < 0 {
		return nil, nil, "", nil
	}
	configID, _ := steps[i]["authenticationConfig"].(string)
	if configID == "" {
		return steps[i], nil, "", nil
	}
	rep, err := readAuthConfig(ctx, c, configID)
	if err != nil {
		return nil, nil, "", err
	}
	config, _ := rep["config"].(map[string]any)
	provider, _ := config["defaultProvider"].(string)
	return steps[i], rep, provider, nil
}

// copyTouchRedirect gives the touch flow's redirect step the company
// login default of the realm's browser flow. Without it, binding the
// flow would stop the console from going straight to the company login.
// The chart's setup step does the same at each upgrade
// (backend/src/meho_backplane/core/company_login.py).
func copyTouchRedirect(ctx context.Context, c *adminClient, flowID string) ([]string, error) {
	realm, err := readRealm(ctx, c)
	if err != nil {
		return nil, err
	}
	browser, _ := realm["browserFlow"].(string)
	if browser == "" {
		browser = "browser"
	}
	if browser == touchFlowAlias {
		return nil, nil
	}
	body, err := c.flowCall(ctx, http.MethodGet, flowExecutionsPath(browser), nil, http.StatusOK)
	if err != nil {
		return nil, err
	}
	var browserSteps []map[string]any
	if err := json.Unmarshal(body, &browserSteps); err != nil {
		return nil, fmt.Errorf("decode the steps of login flow %q: %w", browser, err)
	}
	_, _, wanted, err := redirectDefault(ctx, c, browserSteps)
	if err != nil || wanted == "" {
		return nil, err
	}
	event := fmt.Sprintf("[updated] login flow %q goes straight to the company login %q, like login flow %q",
		touchFlowAlias, wanted, browser)
	if c.dryRun && flowID == dryRunNewID {
		return []string{event}, nil
	}
	steps, err := readTouchSteps(ctx, c)
	if err != nil {
		return nil, err
	}
	step, rep, have, err := redirectDefault(ctx, c, steps)
	if err != nil {
		return nil, err
	}
	if step == nil {
		return nil, fmt.Errorf("login flow %q has no %s step", touchFlowAlias, redirectorType)
	}
	if have == wanted {
		return nil, nil
	}
	if rep != nil {
		config, _ := rep["config"].(map[string]any)
		config = maps.Clone(config)
		if config == nil {
			config = map[string]any{}
		}
		config["defaultProvider"] = wanted
		rep["config"] = config
		id, _ := rep["id"].(string)
		if _, err := c.flowCall(ctx, http.MethodPut,
			"authentication/config/"+url.PathEscape(id), rep, http.StatusNoContent); err != nil {
			return nil, err
		}
	} else {
		stepID, _ := step["id"].(string)
		config := map[string]any{"alias": touchRedirectAlias, "config": map[string]string{"defaultProvider": wanted}}
		if _, err := c.flowCall(ctx, http.MethodPost,
			"authentication/executions/"+url.PathEscape(stepID)+"/config", config, http.StatusCreated); err != nil {
			return nil, err
		}
	}
	if c.dryRun {
		return []string{event}, nil
	}
	if steps, err = readTouchSteps(ctx, c); err != nil {
		return nil, err
	}
	if _, _, have, err = redirectDefault(ctx, c, steps); err != nil {
		return nil, err
	}
	if have != wanted {
		return nil, fmt.Errorf("the company login default of login flow %q does not read back; "+
			"the console client is not bound to it", touchFlowAlias)
	}
	return []string{event}, nil
}
