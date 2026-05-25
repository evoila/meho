// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package agentprincipal hosts the cobra commands under
// `meho agent-principal ...` for G11.2-T1 (#815) of Initiative #803
// (G11.2 Agent identity + RBAC + approval). v0.2 ships three lifecycle
// verbs that wrap the T1 REST surface
// (`backend/src/meho_backplane/api/v1/agent_principals.py`):
//
//   - `meho agent-principal list [--include-revoked] [--json]` — list active
//     agent principals via GET /api/v1/agent-principals. Role: operator.
//   - `meho agent-principal register <name> [--owner-sub S] [--json]` —
//     register a new agent principal via POST /api/v1/agent-principals.
//     Creates a Keycloak client tagged kind=agent + inserts a DB row.
//     Role: tenant_admin.
//   - `meho agent-principal revoke <name> [--json]` — revoke an agent
//     principal (kill switch) via DELETE /api/v1/agent-principals/{name}/revoke.
//     Role: tenant_admin.
//
// Authentication piggybacks on the token `meho login` wrote — same
// pattern as `meho agent`, `meho kb`, `meho broadcast`.
package agentprincipal

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho agent-principal` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "agent-principal",
		Short: "Manage agent principals (register / list / revoke)",
		Long: "Manage tenant-scoped agent principals for G11.2. " +
			"An agent principal is a Keycloak client tagged kind=agent " +
			"that allows an agent to authenticate to MEHO. " +
			"Write verbs (register / revoke) require tenant_admin; " +
			"read verbs (list) are operator-level. " +
			"register creates the Keycloak client and a DB row; " +
			"revoke disables the Keycloak client (kill switch) and marks " +
			"the row revoked.",
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newRegisterCmd())
	cmd.AddCommand(newRevokeCmd())
	return cmd
}

// Entry mirrors the backend AgentPrincipalRead pydantic model.
type Entry struct {
	ID                 string `json:"id"`
	TenantID           string `json:"tenant_id"`
	Name               string `json:"name"`
	KeycloakClientID   string `json:"keycloak_client_id"`
	KeycloakInternalID string `json:"keycloak_internal_id"`
	OwnerSub           string `json:"owner_sub"`
	Revoked            bool   `json:"revoked"`
	CreatedBySub       string `json:"created_by_sub"`
	CreatedAt          string `json:"created_at"`
	UpdatedAt          string `json:"updated_at"`
}

// ListResponse mirrors the AgentPrincipalListResponse envelope.
type ListResponse struct {
	Principals []Entry `json:"principals"`
}

// errMissingAccessToken is the sentinel doAuthedRequest returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure renderRequestError maps to auth_expired
// with a `meho login` hint. Mirrors the agent package's shape.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// doAuthedRequest issues a single authenticated HTTP request against the
// backplane with bearer injection and one-shot 401-refresh-retry. Returns
// the raw response body on 2xx, an *httpError on non-2xx, or an error
// categorised by api.IsTokenNotFound / api.IsNoRefreshToken / generic
// transport. A 204 yields nil without error (used by the revoke verb).
func doAuthedRequest(ctx context.Context, backplaneURL, method, path string, body []byte) ([]byte, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	httpClient := authed.HTTPClient()
	bearer := authed.AccessToken()
	if bearer == "" {
		return nil, errMissingAccessToken
	}

	resp, err := sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close() //nolint:errcheck
			return nil, rerr
		}
		resp.Body.Close() //nolint:errcheck
		bearer = authed.AccessToken()
		resp, err = sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close() //nolint:errcheck

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, responseBodyCap+1))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if int64(len(raw)) > responseBodyCap {
		return nil, fmt.Errorf(
			"response body exceeds %d-byte cap; refusing to decode possibly-truncated JSON",
			responseBodyCap,
		)
	}
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// responseBodyCap bounds the response body the CLI will read — 1 MiB is
// comfortable headroom for any realistic agent-principal record.
const responseBodyCap int64 = 1 << 20

// httpError carries a non-2xx response so per-verb runners can render the
// right category.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// sendRequest builds and dispatches a single HTTP request using the
// supplied bearer token. It does NOT drain the response body — callers
// are responsible for closing resp.Body.
func sendRequest(
	ctx context.Context,
	client *http.Client,
	backplaneURL, method, path, bearer string,
	body []byte,
) (*http.Response, error) {
	fullURL := backplaneURL + path
	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, fullURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
}

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail string `json:"detail"`
}

// decodeDetailString pulls the “detail“ field out of a FastAPI error
// body. Returns the raw body string if the body is not valid JSON.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil && env.Detail != "" {
		return env.Detail
	}
	return body
}

// renderRequestError translates a request error into the right
// output.StructuredError category. Maps the agent-principals REST surface:
//
//   - empty stored bearer → auth_expired.
//   - 401 (refresh failed / token rejected) → auth_expired.
//   - 403 → insufficient_role.
//   - 404 → unexpected (agent_principal_not_found; cross-tenant probes land here).
//   - 409 → unexpected (agent_principal_already_exists).
//   - 503 → unexpected (keycloak_admin_not_configured).
//   - Pure transport errors → unreachable.
func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	if errors.Is(err, errMissingAccessToken) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored credentials for %s are incomplete; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	var he *httpError
	if errors.As(err, &he) {
		switch he.StatusCode {
		case http.StatusUnauthorized:
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf(
					"backplane rejected the stored token; run `meho login %s`",
					backplaneURL,
				)),
				jsonOut,
			)
		case http.StatusForbidden:
			return output.RenderError(cmd.ErrOrStderr(),
				output.InsufficientRole(decodeDetailString(he.Body)),
				jsonOut,
			)
		case http.StatusNotFound:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(decodeDetailString(he.Body)),
				jsonOut,
			)
		case http.StatusConflict:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(decodeDetailString(he.Body)),
				jsonOut,
			)
		case http.StatusServiceUnavailable:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("keycloak_admin_not_configured: contact your MEHO administrator"),
				jsonOut,
			)
		default:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
					backplaneURL, he.StatusCode, he.Body)),
				jsonOut,
			)
		}
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

func printEntrySummary(w io.Writer, e *Entry) {
	fmt.Fprintf(w, "  id:                  %s\n", e.ID)
	fmt.Fprintf(w, "  keycloak_client_id:  %s\n", e.KeycloakClientID)
	fmt.Fprintf(w, "  owner_sub:           %s\n", e.OwnerSub)
	fmt.Fprintf(w, "  revoked:             %v\n", e.Revoked)
	fmt.Fprintf(w, "  created_at:          %s\n", e.CreatedAt)
}
