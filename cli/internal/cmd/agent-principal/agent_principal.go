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
//
// G0.12-T4 #1262 migrated this package off the sibling-verb pattern
// of hand-rolled HTTP + hand-typed copies of backend pydantic models.
// Every verb here drives the generated `api.ClientWithResponses`
// surface directly: `api.NewAuthedClient` wires the bearer + lazy
// 401-refresh editor onto the embedded `ClientWithResponses`, and
// the verbs call the typed `*WithResponse` methods
// (`ListAgentPrincipalsApiV1AgentPrincipalsGetWithResponse` etc.).
// Consumer-side struct drift — the #1069 root cause Initiative #1118
// targets — can't recur because we now consume `api.AgentPrincipalRead`
// and `api.AgentPrincipalListResponse` directly.
package agentprincipal

import (
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

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure renderRequestError maps to auth_expired
// with a `meho login` hint. Mirrors the agent / approvals packages'
// shape so an operator sees the same hint across every verb tree.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Centralised
// so every verb's typed-call path goes through the same
// "stored-token-loaded + non-empty bearer" gate; the caller forwards
// any returned error to renderRequestError for category mapping.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once, and if the typed response carries a
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors the
// behaviour `api.AuthedClient.GetHealth` implements for the
// /api/v1/health endpoint, generalised so every agent-principal verb
// runs the same transparent-retry contract.
//
// statusOf reads the StatusCode off the typed response envelope (the
// generated *Response types expose StatusCode() through their embedded
// *http.Response). A nil response counts as "no retry" — the transport
// already failed and the caller surfaces err directly.
func retryOn401[R any](
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*R, error),
	statusOf func(*R) int,
) (*R, error) {
	resp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if resp == nil || statusOf(resp) != http.StatusUnauthorized {
		return resp, nil
	}
	if rerr := authed.Refresh(ctx); rerr != nil {
		return resp, rerr
	}
	return call(ctx)
}

// renderRequestError translates a transport-layer request error into
// the right output.StructuredError category. Maps the agent-principals
// REST surface's pre-response failures: missing bearer, no-refresh-
// token, token-not-found, plus the generic transport-down case.
// Non-2xx status codes carried in a typed response envelope are
// classified by renderHTTPStatus instead.
func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
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
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPStatus classifies a non-2xx response (or 401 after a
// failed refresh) carried in the typed envelope into the right
// StructuredError category. Mirrors the pre-migration `httpError`
// switch but acts on the (statusCode, body) pair lifted off the
// generated `*Response.HTTPResponse` + `Body` fields rather than a
// sentinel value. The mapping preserved across the migration:
//
//   - 401 → auth_expired (refresh impossible / token rejected).
//   - 403 → insufficient_role with the backend's detail string.
//   - 404 → unexpected with the backend's detail (agent_principal_not_found;
//     cross-tenant probes land here per the no-existence-leak posture).
//   - 409 → unexpected with the backend's detail (agent_principal_already_exists).
//   - 503 → unexpected with the keycloak_admin_not_configured hint
//     (the constant the backend's auth/keycloak_admin.py raises).
//   - Other non-2xx → unexpected with the raw body.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	bodyStr := strings.TrimSpace(string(body))
	switch statusCode {
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
			output.InsufficientRole(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
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
				backplaneURL, statusCode, bodyStr)),
			jsonOut,
		)
	}
}

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail string `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body. Returns the raw body string if the body is not valid JSON or
// the `detail` field is missing.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil && env.Detail != "" {
		return env.Detail
	}
	return body
}

// printEntrySummary renders an AgentPrincipalRead as a short
// operator-facing block. Same shape as the pre-migration renderer
// (load-bearing for the acceptance criterion that requires byte-
// identical output across the migration); the only delta is the
// type the verb threads in (now api.AgentPrincipalRead, no consumer-
// side duplicate).
func printEntrySummary(w io.Writer, e *api.AgentPrincipalRead) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "  id:                  %s\n", e.Id.String())
	fmt.Fprintf(w, "  keycloak_client_id:  %s\n", e.KeycloakClientId)
	fmt.Fprintf(w, "  owner_sub:           %s\n", e.OwnerSub)
	fmt.Fprintf(w, "  revoked:             %v\n", e.Revoked)
	fmt.Fprintf(w, "  created_at:          %s\n", e.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
}
