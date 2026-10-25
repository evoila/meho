// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package addon hosts the cobra commands under `meho addon ...`. An add-on
// is a separate product that works with MEHO, for example the automation
// add-on. Pairing gives the add-on its own login to MEHO: a client id and a
// client secret. MEHO shows the secret only once.
//
// The four verbs wrap the operator routes in
// `backend/src/meho_backplane/api/v1/addon_pairing.py`:
//
//   - `meho addon pair <name>` — POST /api/v1/addons/pairings. Role: tenant_admin.
//   - `meho addon list` — GET /api/v1/addons/pairings. Role: operator.
//   - `meho addon show <name>` — GET /api/v1/addons/pairings/{name}. Role: operator.
//   - `meho addon unpair <name>` — DELETE /api/v1/addons/pairings/{name}. Role: tenant_admin.
//
// The package copies the shape of `meho runner-principal`: the typed
// generated client, one retry after an expired login (401), `--json` and
// `--backplane` on every verb, and the same error output.
package addon

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho addon` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "addon",
		Short: "Manage add-on pairings (pair / list / show / unpair)",
		Long: "Manage the pairings between MEHO and its add-ons. An add-on is " +
			"a separate product that works with MEHO, for example the " +
			"automation add-on. Pairing gives the add-on its own login to " +
			"MEHO: a client id and a client secret. MEHO shows the secret " +
			"only once. pair and unpair need the tenant_admin role; list and " +
			"show need the operator role. Pairing health is part of " +
			"`meho status --json`.",
	}
	cmd.AddCommand(newPairCmd())
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newUnpairCmd())
	return cmd
}

// errMissingAccessToken is returned when the stored token row exists but
// has no access token. renderRequestError maps it to auth_expired.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

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

// retryOn401 runs call once. If the answer is a 401, it refreshes the
// login once and runs call again.
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

// renderRequestError turns a transport or credential error into the
// matching structured error.
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

// Role advice for a 403, per verb. pair and unpair are tenant_admin
// actions; list and show are operator reads.
const (
	adminRoleAdvice    = "pair and unpair need the tenant_admin role"
	operatorRoleAdvice = "list and show need the operator role"
)

// renderHTTPStatus turns a non-success answer into a structured error.
// It prints the server's code and one line that says what to do. name is
// the add-on name the verb worked on; it is empty for list. roleAdvice is
// the line printed after a 403.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	name string,
	roleAdvice string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	detail := decodeDetailString(strings.TrimSpace(string(body)))
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
			output.InsufficientRole(detail+": "+roleAdvice),
			jsonOut,
		)
	case http.StatusNotFound, http.StatusConflict,
		http.StatusBadGateway, http.StatusServiceUnavailable:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(adviceFor(statusCode, detail, name)),
			jsonOut,
		)
	default:
		msg := fmt.Sprintf("call %s: HTTP %d", backplaneURL, statusCode)
		if detail != "" {
			msg += ": " + detail
		}
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(msg), jsonOut)
	}
}

// adviceFor returns "<server code>: <what to do>" for the error codes the
// pairing routes answer with. An unknown code is returned as is.
func adviceFor(statusCode int, code, name string) string {
	switch {
	case statusCode == http.StatusConflict && code == "addon_contract_below_backplane_minimum":
		return code + ": the add-on is too old for this MEHO. Upgrade the add-on."
	case statusCode == http.StatusConflict && code == "backplane_contract_below_addon_minimum":
		return code + ": this MEHO is too old for the add-on. Upgrade MEHO."
	case statusCode == http.StatusConflict && code == "addon_already_paired":
		return fmt.Sprintf("%s: run `meho addon unpair %s` first, then pair again.", code, name)
	case statusCode == http.StatusNotFound && code == "addon_not_paired":
		return fmt.Sprintf("%s: no add-on named %q is paired.", code, name)
	case statusCode == http.StatusServiceUnavailable:
		return "keycloak_admin_not_configured: this install cannot create logins " +
			"for add-ons. The Keycloak admin client is not set up."
	case statusCode == http.StatusBadGateway:
		return code + ": the call to Keycloak failed. Try again. If it fails again, check Keycloak."
	}
	return code
}

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail string `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error body.
// Returns the raw body when it is not JSON or has no `detail` string.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil && env.Detail != "" {
		return env.Detail
	}
	return body
}

const timeLayout = "2006-01-02T15:04:05Z"

func formatTime(t time.Time) string {
	return t.UTC().Format(timeLayout)
}

// formatLastSeen renders the add-on's last heartbeat, or "never" when the
// add-on has not reported yet.
func formatLastSeen(t *time.Time) string {
	if t == nil {
		return "never"
	}
	return formatTime(*t)
}

// printPairingFields renders every field of one pairing.
func printPairingFields(w io.Writer, e *api.PairedAddonRead) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "  id:                           %s\n", e.Id.String())
	fmt.Fprintf(w, "  tenant_id:                    %s\n", e.TenantId.String())
	fmt.Fprintf(w, "  name:                         %s\n", e.Name)
	fmt.Fprintf(w, "  keycloak_client_id:           %s\n", e.KeycloakClientId)
	fmt.Fprintf(w, "  owner_sub:                    %s\n", e.OwnerSub)
	fmt.Fprintf(w, "  contract_version:             %d\n", e.ContractVersion)
	fmt.Fprintf(w, "  addon_contract_version:       %d\n", e.AddonContractVersion)
	fmt.Fprintf(w, "  addon_min_backplane_version:  %d\n", e.AddonMinBackplaneVersion)
	fmt.Fprintf(w, "  created_by_sub:               %s\n", e.CreatedBySub)
	fmt.Fprintf(w, "  paired_at:                    %s\n", formatTime(e.PairedAt))
	fmt.Fprintf(w, "  last_seen_at:                 %s\n", formatLastSeen(e.LastSeenAt))
	fmt.Fprintf(w, "  updated_at:                   %s\n", formatTime(e.UpdatedAt))
}
