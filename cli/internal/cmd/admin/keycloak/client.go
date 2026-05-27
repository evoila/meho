// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// adminClient is a thin wrapper around the Keycloak admin REST API
// scoped to one realm + one minted admin access token. The token is
// minted once at construction (mintAdminToken) against the master
// realm using the password grant against `admin-cli`, matching the
// pattern the reference shell script at
// rdc-hetzner-dc/scripts/keycloak-bootstrap-meho-cli.sh uses.
//
// The client is deliberately stdlib-only — no Keycloak Go SDK in
// go.mod. The admin verb runs once per bootstrap and the surface area
// is small (clients + protocol-mappers + client-scopes + users +
// groups, all under /admin/realms/{realm}/...); pulling in a 100k-LoC
// generated SDK for that is a bad tradeoff. Stdlib net/http +
// encoding/json keeps the supply chain stable and the tests free of
// fixture-bloat.
type adminClient struct {
	httpClient *http.Client
	baseURL    string // e.g. "https://keycloak.evba.lab"
	realm      string // target realm (where clients land), e.g. "evba"
	token      string // master-realm admin access token
}

// errKeycloakAPI carries the HTTP status + the raw response body from
// the Keycloak admin REST API. Callers inspect statusCode to branch on
// 404 (not found) vs 409 (already exists) vs other failures; the body
// is preserved verbatim because Keycloak's error JSON shape varies by
// endpoint (sometimes {error,error_description}, sometimes
// {errorMessage}).
type errKeycloakAPI struct {
	method     string
	url        string
	statusCode int
	body       string
}

func (e *errKeycloakAPI) Error() string {
	return fmt.Sprintf("keycloak admin API %s %s: HTTP %d: %s",
		e.method, e.url, e.statusCode, strings.TrimSpace(e.body))
}

// mintAdminToken POSTs to /realms/master/protocol/openid-connect/token
// with grant_type=password against the built-in admin-cli client, just
// like the reference shell script. Returns the bearer access_token
// string. The username + password are passed via form fields; the
// caller is responsible for not echoing them — meho admin uses
// ReadPassword + an env-var fallback that never enters argv.
func mintAdminToken(
	ctx context.Context,
	httpClient *http.Client,
	keycloakBase, adminUser, adminPassword string,
) (string, error) {
	form := url.Values{}
	form.Set("grant_type", "password")
	form.Set("client_id", "admin-cli")
	form.Set("username", adminUser)
	form.Set("password", adminPassword)

	endpoint := strings.TrimRight(keycloakBase, "/") +
		"/realms/master/protocol/openid-connect/token"

	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("build admin-token request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("mint admin token: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read admin-token response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return "", &errKeycloakAPI{
			method:     http.MethodPost,
			url:        endpoint,
			statusCode: resp.StatusCode,
			body:       string(body),
		}
	}

	var tokenResp struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.Unmarshal(body, &tokenResp); err != nil {
		return "", fmt.Errorf("decode admin-token response: %w", err)
	}
	if tokenResp.AccessToken == "" {
		return "", errors.New(
			"keycloak admin token response carried no access_token")
	}
	return tokenResp.AccessToken, nil
}

// newAdminClient constructs an adminClient. The caller passes a
// pre-built *http.Client so tests can swap in httptest.Server URLs +
// the production caller can configure TLS (with --insecure-skip-tls-
// verify when the operator workstation has no system trust for the
// realm's CA, mirroring the shell script's `curl -k`).
func newAdminClient(
	httpClient *http.Client,
	keycloakBase, realm, token string,
) *adminClient {
	return &adminClient{
		httpClient: httpClient,
		baseURL:    strings.TrimRight(keycloakBase, "/"),
		realm:      realm,
		token:      token,
	}
}

// do executes an admin-API request against /admin/realms/{realm}/...,
// returning the (status, body) tuple. Non-2xx responses surface
// errKeycloakAPI; the caller decides whether the status means
// "already exists" (409 / 404 on GET-by-id) or a real failure.
//
// pathRelative is appended after /admin/realms/{realm}/ — leading
// slash is stripped so callers can pass either form.
func (c *adminClient) do(
	ctx context.Context,
	method, pathRelative string,
	body any,
) (int, []byte, error) {
	endpoint := fmt.Sprintf(
		"%s/admin/realms/%s/%s",
		c.baseURL, c.realm, strings.TrimLeft(pathRelative, "/"))

	var reader io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return 0, nil, fmt.Errorf("marshal %s body: %w", method, err)
		}
		reader = bytes.NewReader(buf)
	}

	req, err := http.NewRequestWithContext(ctx, method, endpoint, reader)
	if err != nil {
		return 0, nil, fmt.Errorf("build %s %s: %w", method, endpoint, err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return 0, nil, fmt.Errorf("%s %s: %w", method, endpoint, err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp.StatusCode, nil, fmt.Errorf(
			"read %s %s response: %w", method, endpoint, err)
	}
	return resp.StatusCode, respBody, nil
}

// clientRep matches Keycloak's ClientRepresentation enough for the
// fields we set / read. We intentionally use json.RawMessage for
// attributes and json-typed slices so a re-PUT preserves keys the
// admin console may have added (e.g. `clientAuthenticatorType`).
type clientRep struct {
	ID                        string                     `json:"id,omitempty"`
	ClientID                  string                     `json:"clientId"`
	Name                      string                     `json:"name,omitempty"`
	Description               string                     `json:"description,omitempty"`
	Enabled                   *bool                      `json:"enabled,omitempty"`
	PublicClient              *bool                      `json:"publicClient,omitempty"`
	StandardFlowEnabled       *bool                      `json:"standardFlowEnabled,omitempty"`
	ImplicitFlowEnabled       *bool                      `json:"implicitFlowEnabled,omitempty"`
	DirectAccessGrantsEnabled *bool                      `json:"directAccessGrantsEnabled,omitempty"`
	ServiceAccountsEnabled    *bool                      `json:"serviceAccountsEnabled,omitempty"`
	FrontchannelLogout        *bool                      `json:"frontchannelLogout,omitempty"`
	Attributes                map[string]string          `json:"attributes,omitempty"`
	RedirectURIs              []string                   `json:"redirectUris"`
	WebOrigins                []string                   `json:"webOrigins"`
	DefaultClientScopes       []string                   `json:"defaultClientScopes,omitempty"`
	OptionalClientScopes      []string                   `json:"optionalClientScopes,omitempty"`
	ProtocolMappers           []protocolMapperRep        `json:"-"` // never marshalled on create — installed separately
	Extra                     map[string]json.RawMessage `json:"-"` // preserved from GET
}

// protocolMapperRep mirrors ProtocolMapperRepresentation closely
// enough for the five mapper shapes the bootstrap recipe needs.
type protocolMapperRep struct {
	ID              string            `json:"id,omitempty"`
	Name            string            `json:"name"`
	Protocol        string            `json:"protocol"`
	ProtocolMapper  string            `json:"protocolMapper"`
	ConsentRequired bool              `json:"consentRequired"`
	Config          map[string]string `json:"config"`
}

// scopeRep is the slice of ClientScopeRepresentation we read off
// /client-scopes; we only ever need name+id.
type scopeRep struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// groupRep / userRep / credentialRep are the minimum shape we set or
// read for the meho-admins group + admin-user provisioning at Step 5
// of the recipe.
type groupRep struct {
	ID   string `json:"id,omitempty"`
	Name string `json:"name"`
}

type userRep struct {
	ID            string   `json:"id,omitempty"`
	Username      string   `json:"username"`
	Email         string   `json:"email,omitempty"`
	Enabled       *bool    `json:"enabled,omitempty"`
	EmailVerified *bool    `json:"emailVerified,omitempty"`
	Groups        []string `json:"groups,omitempty"`
}

type credentialRep struct {
	Type      string `json:"type"`
	Value     string `json:"value"`
	Temporary bool   `json:"temporary"`
}

// findClient queries /clients?clientId=<id> and returns the matching
// ClientRepresentation (a single-element list, or empty). Returns
// (nil, nil) when the client doesn't exist — the caller distinguishes
// not-found from error.
func (c *adminClient) findClient(
	ctx context.Context, clientID string,
) (*clientRep, error) {
	status, body, err := c.do(
		ctx,
		http.MethodGet,
		"clients?clientId="+url.QueryEscape(clientID),
		nil,
	)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method: http.MethodGet, statusCode: status, body: string(body),
			url: "clients?clientId=" + clientID,
		}
	}
	var matches []clientRep
	if err := json.Unmarshal(body, &matches); err != nil {
		return nil, fmt.Errorf("decode findClient response: %w", err)
	}
	if len(matches) == 0 {
		return nil, nil
	}
	return &matches[0], nil
}

// createClient POSTs a new ClientRepresentation. Returns the new
// client's UUID (parsed from the Location header on 201).
func (c *adminClient) createClient(
	ctx context.Context, rep *clientRep,
) (string, error) {
	endpoint := fmt.Sprintf(
		"%s/admin/realms/%s/clients",
		c.baseURL, c.realm)

	buf, err := json.Marshal(rep)
	if err != nil {
		return "", fmt.Errorf("marshal createClient body: %w", err)
	}
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost, endpoint, bytes.NewReader(buf))
	if err != nil {
		return "", fmt.Errorf("build createClient request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("createClient: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusCreated {
		return "", &errKeycloakAPI{
			method:     http.MethodPost,
			url:        endpoint,
			statusCode: resp.StatusCode,
			body:       string(body),
		}
	}
	loc := resp.Header.Get("Location")
	if loc == "" {
		return "", errors.New(
			"createClient: HTTP 201 but no Location header")
	}
	// Location: .../admin/realms/{realm}/clients/{uuid}
	idx := strings.LastIndex(loc, "/")
	if idx < 0 || idx == len(loc)-1 {
		return "", fmt.Errorf(
			"createClient: malformed Location header %q", loc)
	}
	return loc[idx+1:], nil
}

// updateClient PUTs a merged ClientRepresentation. The caller is
// responsible for preserving fields it doesn't want to clobber
// (Keycloak's PUT is a full replacement, not a merge — exception: a
// missing `attributes` key preserves stored attributes, but a missing
// `defaultClientScopes` clears them).
func (c *adminClient) updateClient(
	ctx context.Context, uuid string, rep *clientRep,
) error {
	status, body, err := c.do(
		ctx, http.MethodPut, "clients/"+uuid, rep)
	if err != nil {
		return err
	}
	if status != http.StatusNoContent {
		return &errKeycloakAPI{
			method:     http.MethodPut,
			url:        "clients/" + uuid,
			statusCode: status,
			body:       string(body),
		}
	}
	return nil
}

// listClientMappers GETs the currently-installed protocol mappers on
// a client. Used to make mapper installation idempotent (skip if
// already present; PUT to update if present-but-different).
func (c *adminClient) listClientMappers(
	ctx context.Context, clientUUID string,
) ([]protocolMapperRep, error) {
	status, body, err := c.do(
		ctx, http.MethodGet,
		"clients/"+clientUUID+"/protocol-mappers/models",
		nil,
	)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method:     http.MethodGet,
			url:        "clients/" + clientUUID + "/protocol-mappers/models",
			statusCode: status,
			body:       string(body),
		}
	}
	var got []protocolMapperRep
	if err := json.Unmarshal(body, &got); err != nil {
		return nil, fmt.Errorf("decode listClientMappers: %w", err)
	}
	return got, nil
}

// createClientMapper POSTs a single mapper. Errors on non-201.
func (c *adminClient) createClientMapper(
	ctx context.Context, clientUUID string, mapper protocolMapperRep,
) error {
	status, body, err := c.do(
		ctx, http.MethodPost,
		"clients/"+clientUUID+"/protocol-mappers/models",
		mapper,
	)
	if err != nil {
		return err
	}
	if status != http.StatusCreated {
		return &errKeycloakAPI{
			method:     http.MethodPost,
			url:        "clients/" + clientUUID + "/protocol-mappers/models",
			statusCode: status,
			body:       string(body),
		}
	}
	return nil
}

// updateClientMapper PUTs an existing mapper (by its UUID) to the
// desired shape. Lets a re-run flip a mistakenly-flipped flag back to
// the recipe shape without manual cleanup.
func (c *adminClient) updateClientMapper(
	ctx context.Context, clientUUID, mapperID string, mapper protocolMapperRep,
) error {
	status, body, err := c.do(
		ctx, http.MethodPut,
		"clients/"+clientUUID+"/protocol-mappers/models/"+mapperID,
		mapper,
	)
	if err != nil {
		return err
	}
	if status != http.StatusNoContent {
		return &errKeycloakAPI{
			method:     http.MethodPut,
			url:        "clients/" + clientUUID + "/protocol-mappers/models/" + mapperID,
			statusCode: status,
			body:       string(body),
		}
	}
	return nil
}

// listRealmClientScopes GETs the realm's client-scopes. We need this
// to resolve the (name → id) mapping required by
// PUT /clients/{uuid}/default-client-scopes/{scopeId} (which takes
// the *scope's* UUID, not its name).
func (c *adminClient) listRealmClientScopes(
	ctx context.Context,
) ([]scopeRep, error) {
	status, body, err := c.do(ctx, http.MethodGet, "client-scopes", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method: http.MethodGet, url: "client-scopes",
			statusCode: status, body: string(body),
		}
	}
	var got []scopeRep
	if err := json.Unmarshal(body, &got); err != nil {
		return nil, fmt.Errorf("decode listRealmClientScopes: %w", err)
	}
	return got, nil
}

// listClientDefaultScopes GETs the default-client-scopes currently
// assigned to a client. The result drives the idempotency check
// before PUTting more.
func (c *adminClient) listClientDefaultScopes(
	ctx context.Context, clientUUID string,
) ([]scopeRep, error) {
	status, body, err := c.do(
		ctx, http.MethodGet,
		"clients/"+clientUUID+"/default-client-scopes", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method:     http.MethodGet,
			url:        "clients/" + clientUUID + "/default-client-scopes",
			statusCode: status, body: string(body),
		}
	}
	var got []scopeRep
	if err := json.Unmarshal(body, &got); err != nil {
		return nil, fmt.Errorf("decode listClientDefaultScopes: %w", err)
	}
	return got, nil
}

// addClientDefaultScope PUTs to /default-client-scopes/{scopeId} to
// add one scope. Keycloak treats this as idempotent on its own (a
// second PUT to a scope that is already a default returns 204), so
// the caller may skip the check-then-PUT pattern if it wants — but
// the shell script does the check first and we mirror that.
func (c *adminClient) addClientDefaultScope(
	ctx context.Context, clientUUID, scopeID string,
) error {
	status, body, err := c.do(
		ctx, http.MethodPut,
		"clients/"+clientUUID+"/default-client-scopes/"+scopeID,
		nil,
	)
	if err != nil {
		return err
	}
	if status != http.StatusNoContent {
		return &errKeycloakAPI{
			method:     http.MethodPut,
			url:        "clients/" + clientUUID + "/default-client-scopes/" + scopeID,
			statusCode: status, body: string(body),
		}
	}
	return nil
}

// listClientOptionalScopes GETs the optional-client-scopes currently
// assigned to a client. Mirrors listClientDefaultScopes — the Keycloak
// admin REST API exposes default and optional scopes via parallel
// endpoints (`/default-client-scopes` vs `/optional-client-scopes`)
// with identical request/response shapes. The result drives the
// idempotency check before PUTting more.
func (c *adminClient) listClientOptionalScopes(
	ctx context.Context, clientUUID string,
) ([]scopeRep, error) {
	status, body, err := c.do(
		ctx, http.MethodGet,
		"clients/"+clientUUID+"/optional-client-scopes", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method:     http.MethodGet,
			url:        "clients/" + clientUUID + "/optional-client-scopes",
			statusCode: status, body: string(body),
		}
	}
	var got []scopeRep
	if err := json.Unmarshal(body, &got); err != nil {
		return nil, fmt.Errorf("decode listClientOptionalScopes: %w", err)
	}
	return got, nil
}

// addClientOptionalScope PUTs to /optional-client-scopes/{scopeId} to
// add one scope. Same idempotency semantics as
// addClientDefaultScope — a second PUT to a scope that is already an
// optional returns 204.
func (c *adminClient) addClientOptionalScope(
	ctx context.Context, clientUUID, scopeID string,
) error {
	status, body, err := c.do(
		ctx, http.MethodPut,
		"clients/"+clientUUID+"/optional-client-scopes/"+scopeID,
		nil,
	)
	if err != nil {
		return err
	}
	if status != http.StatusNoContent {
		return &errKeycloakAPI{
			method:     http.MethodPut,
			url:        "clients/" + clientUUID + "/optional-client-scopes/" + scopeID,
			statusCode: status, body: string(body),
		}
	}
	return nil
}

// findGroup queries /groups?search=<name> and returns the
// top-level group that exactly matches by name. The Keycloak admin
// GET /groups endpoint does not currently support `exact=true` on all
// supported versions; we filter client-side rather than rely on
// substring-match behaviour.
func (c *adminClient) findGroup(
	ctx context.Context, name string,
) (*groupRep, error) {
	status, body, err := c.do(
		ctx, http.MethodGet,
		"groups?search="+url.QueryEscape(name), nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method: http.MethodGet, url: "groups?search=" + name,
			statusCode: status, body: string(body),
		}
	}
	var groups []groupRep
	if err := json.Unmarshal(body, &groups); err != nil {
		return nil, fmt.Errorf("decode findGroup: %w", err)
	}
	for i := range groups {
		if groups[i].Name == name {
			return &groups[i], nil
		}
	}
	return nil, nil
}

// createGroup POSTs a new top-level group; returns its UUID.
func (c *adminClient) createGroup(
	ctx context.Context, name string,
) (string, error) {
	endpoint := fmt.Sprintf(
		"%s/admin/realms/%s/groups", c.baseURL, c.realm)

	buf, err := json.Marshal(groupRep{Name: name})
	if err != nil {
		return "", fmt.Errorf("marshal createGroup body: %w", err)
	}
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost, endpoint, bytes.NewReader(buf))
	if err != nil {
		return "", fmt.Errorf("build createGroup request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("createGroup: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusCreated {
		return "", &errKeycloakAPI{
			method:     http.MethodPost,
			url:        endpoint,
			statusCode: resp.StatusCode,
			body:       string(body),
		}
	}
	loc := resp.Header.Get("Location")
	if loc == "" {
		// Fall back to a re-query — some admin builds omit the
		// Location header on group create.
		got, qerr := c.findGroup(ctx, name)
		if qerr != nil {
			return "", fmt.Errorf(
				"createGroup: 201 but no Location and re-query: %w", qerr)
		}
		if got == nil {
			return "", errors.New(
				"createGroup: 201 but group not visible on re-query")
		}
		return got.ID, nil
	}
	idx := strings.LastIndex(loc, "/")
	if idx < 0 || idx == len(loc)-1 {
		return "", fmt.Errorf(
			"createGroup: malformed Location header %q", loc)
	}
	return loc[idx+1:], nil
}

// findUserByUsername GETs /users?username=<n>&exact=true and returns
// the matching user (or nil). exact=true is honoured by Keycloak 22+
// per the recipe's prerequisite version pin.
func (c *adminClient) findUserByUsername(
	ctx context.Context, username string,
) (*userRep, error) {
	status, body, err := c.do(
		ctx, http.MethodGet,
		"users?exact=true&username="+url.QueryEscape(username),
		nil,
	)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method: http.MethodGet, statusCode: status,
			body: string(body), url: "users?username=" + username,
		}
	}
	var users []userRep
	if err := json.Unmarshal(body, &users); err != nil {
		return nil, fmt.Errorf("decode findUserByUsername: %w", err)
	}
	for i := range users {
		if users[i].Username == username {
			return &users[i], nil
		}
	}
	return nil, nil
}

// createUser POSTs a new UserRepresentation, returning the new
// user's UUID parsed from the Location header.
func (c *adminClient) createUser(
	ctx context.Context, rep *userRep,
) (string, error) {
	endpoint := fmt.Sprintf(
		"%s/admin/realms/%s/users", c.baseURL, c.realm)

	buf, err := json.Marshal(rep)
	if err != nil {
		return "", fmt.Errorf("marshal createUser body: %w", err)
	}
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost, endpoint, bytes.NewReader(buf))
	if err != nil {
		return "", fmt.Errorf("build createUser request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("createUser: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusCreated {
		return "", &errKeycloakAPI{
			method:     http.MethodPost,
			url:        endpoint,
			statusCode: resp.StatusCode,
			body:       string(body),
		}
	}
	loc := resp.Header.Get("Location")
	if loc == "" {
		got, qerr := c.findUserByUsername(ctx, rep.Username)
		if qerr != nil {
			return "", fmt.Errorf(
				"createUser: 201 but no Location and re-query: %w", qerr)
		}
		if got == nil {
			return "", errors.New(
				"createUser: 201 but user not visible on re-query")
		}
		return got.ID, nil
	}
	idx := strings.LastIndex(loc, "/")
	if idx < 0 || idx == len(loc)-1 {
		return "", fmt.Errorf(
			"createUser: malformed Location header %q", loc)
	}
	return loc[idx+1:], nil
}

// resetUserPassword PUTs to /users/{id}/reset-password with a
// CredentialRepresentation. `temporary` is false in the recipe — the
// realm's password policy controls whether the user is forced to
// change on first login.
func (c *adminClient) resetUserPassword(
	ctx context.Context, userUUID, password string,
) error {
	cred := credentialRep{
		Type:      "password",
		Value:     password,
		Temporary: false,
	}
	status, body, err := c.do(
		ctx, http.MethodPut,
		"users/"+userUUID+"/reset-password",
		cred,
	)
	if err != nil {
		return err
	}
	if status != http.StatusNoContent {
		return &errKeycloakAPI{
			method:     http.MethodPut,
			url:        "users/" + userUUID + "/reset-password",
			statusCode: status, body: string(body),
		}
	}
	return nil
}

// joinUserToGroup PUTs to /users/{userId}/groups/{groupId}. This is
// the admin-API path that mirrors clicking "Join Group" in the
// console; sending it twice is a no-op so the function is naturally
// idempotent (we still pre-check listUserGroups for cleaner stdout).
func (c *adminClient) joinUserToGroup(
	ctx context.Context, userUUID, groupUUID string,
) error {
	status, body, err := c.do(
		ctx, http.MethodPut,
		"users/"+userUUID+"/groups/"+groupUUID, nil)
	if err != nil {
		return err
	}
	if status != http.StatusNoContent {
		return &errKeycloakAPI{
			method:     http.MethodPut,
			url:        "users/" + userUUID + "/groups/" + groupUUID,
			statusCode: status, body: string(body),
		}
	}
	return nil
}

// listUserGroups GETs the groups a user already belongs to. Used to
// skip the join PUT when the user is already a member, so re-runs
// print "skip" instead of (silently-correct but noisy) "joined".
func (c *adminClient) listUserGroups(
	ctx context.Context, userUUID string,
) ([]groupRep, error) {
	status, body, err := c.do(
		ctx, http.MethodGet,
		"users/"+userUUID+"/groups", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, &errKeycloakAPI{
			method: http.MethodGet, url: "users/" + userUUID + "/groups",
			statusCode: status, body: string(body),
		}
	}
	var got []groupRep
	if err := json.Unmarshal(body, &got); err != nil {
		return nil, fmt.Errorf("decode listUserGroups: %w", err)
	}
	return got, nil
}

// defaultHTTPClient is the http.Client adminClient and mintAdminToken
// fall back to when the caller doesn't supply one. 30 s is generous
// for an admin-API round-trip but bounded — without a timeout the
// admin verb would hang indefinitely on a misconfigured proxy.
func defaultHTTPClient() *http.Client {
	return &http.Client{Timeout: 30 * time.Second}
}
