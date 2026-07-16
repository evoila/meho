// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package runnerprincipal hosts the cobra commands under
// `meho runner-principal ...` for Initiative #2415 (#2502) — the scoped
// per-runner service principal of the remote-execution gateway. v1 ships
// four lifecycle verbs that wrap the REST surface
// (`backend/src/meho_backplane/api/v1/runner_principals.py`):
//
//   - `meho runner-principal list [--include-revoked] [--json]` — list active
//     runner principals via GET /api/v1/runner-principals. Role: operator.
//   - `meho runner-principal show <name> [--json]` — show one runner
//     principal via GET /api/v1/runner-principals/{name}. Role: operator.
//   - `meho runner-principal register <name> [--owner-sub S] [--json]` —
//     register a new runner principal via POST /api/v1/runner-principals.
//     Creates a Keycloak client tagged kind=runner (principal_kind=runner,
//     tenant_role=read_only) + inserts a DB row. Role: tenant_admin.
//   - `meho runner-principal revoke <name> [--json]` — revoke a runner
//     principal (kill switch) via DELETE
//     /api/v1/runner-principals/{name}/revoke. Role: tenant_admin.
//
// Authentication piggybacks on the token `meho login` wrote — same pattern
// as `meho agent-principal`, `meho agent`, `meho kb`. Moulded directly on
// the agent-principal verb tree (G0.12-T4 #1262 generated-client shape):
// every verb drives the typed `api.ClientWithResponses` surface and
// consumes `api.RunnerPrincipalRead` / `api.RunnerPrincipalListResponse`
// directly, so consumer-side struct drift cannot recur.
package runnerprincipal

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

// NewRootCmd returns the `meho runner-principal` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "runner-principal",
		Short: "Manage runner principals (register / list / show / revoke)",
		Long: "Manage tenant-scoped satellite-runner principals for the " +
			"remote-execution gateway (Initiative #2415). A runner principal " +
			"is a Keycloak client tagged kind=runner whose token carries " +
			"principal_kind=runner and a read-only tenant_role, letting a " +
			"push-only satellite runner authenticate to MEHO. That token is " +
			"caged to the gateway path prefixes and 403'd everywhere else. " +
			"Write verbs (register / revoke) require tenant_admin; read verbs " +
			"(list / show) are operator-level. register creates the Keycloak " +
			"client and a DB row; revoke disables the Keycloak client (kill " +
			"switch) and marks the row revoked.",
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newRegisterCmd())
	cmd.AddCommand(newRevokeCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns when the
// stored token row exists but its access_token is empty — a credential-
// state failure renderRequestError maps to auth_expired with a `meho
// login` hint. Mirrors the agent-principal package's shape.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient for the supplied backplane
// URL and verifies a non-empty bearer is loaded.
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

// retryOn401 invokes call once, and if the typed response carries a 401,
// runs a one-shot bearer refresh and re-issues call. Mirrors the
// agent-principal package's transparent-retry contract.
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

// renderRequestError translates a transport-layer request error into the
// right output.StructuredError category (missing bearer, no-refresh-token,
// token-not-found, generic transport-down).
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

// renderHTTPStatus classifies a non-2xx response (or 401 after a failed
// refresh) carried in the typed envelope. Mapping (mirrors the
// agent-principal surface):
//
//   - 401 → auth_expired.
//   - 403 → insufficient_role with the backend's detail (also the code a
//     runner-kind token would hit off the gateway prefixes:
//     runner_scope_violation).
//   - 404 → unexpected with the backend detail (runner_principal_not_found;
//     cross-tenant probes land here per the no-existence-leak posture).
//   - 409 → unexpected (runner_principal_already_exists).
//   - 503 → unexpected with the keycloak_admin_not_configured hint.
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

// decodeDetailString pulls the `detail` field out of a FastAPI error body.
// Returns the raw body string if the body is not valid JSON or the
// `detail` field is missing.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil && env.Detail != "" {
		return env.Detail
	}
	return body
}

// printEntrySummary renders a RunnerPrincipalRead as a short operator-
// facing block. Same shape as the agent-principal renderer.
func printEntrySummary(w io.Writer, e *api.RunnerPrincipalRead) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "  id:                  %s\n", e.Id.String())
	fmt.Fprintf(w, "  keycloak_client_id:  %s\n", e.KeycloakClientId)
	fmt.Fprintf(w, "  owner_sub:           %s\n", e.OwnerSub)
	fmt.Fprintf(w, "  revoked:             %v\n", e.Revoked)
	fmt.Fprintf(w, "  created_at:          %s\n", e.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
}
