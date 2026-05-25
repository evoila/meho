// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package approvals hosts the cobra commands under `meho approvals ...`
// for G11.2-T5 (#818) of Initiative #803 (G11.2 Agent identity + RBAC +
// approval). v0.2 ships four operator-facing verbs that wrap the T5 REST
// surface (`backend/src/meho_backplane/api/v1/approvals.py`):
//
//   - `meho approvals list [--status pending] [--limit N] [--offset N] [--json]`
//     — list approval requests via GET /api/v1/approvals. Role: operator.
//   - `meho approvals show <id> [--json]` — inspect one request via
//     GET /api/v1/approvals/{id}. Role: operator. Renders proposed_effect
//     and elicitation_url; --json for the raw envelope.
//   - `meho approvals approve <id> [--reason TEXT] [--json]` — approve via
//     POST /api/v1/approvals/{id}/approve. Role: operator. Resumes the
//     paused agent run via the T4 path.
//   - `meho approvals reject <id> [--reason TEXT] [--json]` — reject via
//     POST /api/v1/approvals/{id}/reject. Role: operator. Aborts the
//     paused agent run.
//
// Authentication piggybacks on the token meho login wrote — same pattern
// as `meho agent`, `meho audit`, `meho conventions`.
//
// The implementation follows the in-package HTTP helper pattern the sibling
// verb trees use (a local doAuthedRequest / renderRequestError pair) rather
// than a shared client package, for the import-cycle reason every sibling
// cites: each verb tree is grafted onto the root command, so a shared helper
// imported from cmd/* and from a per-tree package would close the cycle.
package approvals

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho approvals` parent command. Grafted onto
// the top-level meho tree by cmd/root.go alongside `meho agent`,
// `meho audit`, etc. The parent takes no args and prints its own help;
// every behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "approvals",
		Short: "Manage approval requests (list / show / approve / reject)",
		Long: "Manage pending approval requests wired by G11.2-T5. An " +
			"approval request is created when the policy gate issues a " +
			"needs-approval verdict on a connector operation. Use list " +
			"to see pending requests, show to inspect the proposed " +
			"effect, and approve or reject to decide. On approve, the " +
			"paused agent run resumes. On reject, the run aborts. Both " +
			"read and write verbs require the operator role. Tenant " +
			"scoping is enforced server-side via the JWT.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newApproveCmd())
	cmd.AddCommand(newRejectCmd())
	return cmd
}

// ApprovalSummary mirrors the backend ApprovalRequestSummary pydantic model.
type ApprovalSummary struct {
	ID           string  `json:"id"`
	TenantID     string  `json:"tenant_id"`
	Status       string  `json:"status"`
	ConnectorID  string  `json:"connector_id"`
	OpID         string  `json:"op_id"`
	PrincipalSub string  `json:"principal_sub"`
	PrincipalAct *string `json:"principal_act"`
	CreatedAt    string  `json:"created_at"`
	ExpiresAt    *string `json:"expires_at"`
}

// ApprovalDetail mirrors the backend ApprovalRequestDetail pydantic model.
// ElicitationURL is the MCP elicitation URL-mode forward wire address.
type ApprovalDetail struct {
	ID             string                  `json:"id"`
	TenantID       string                  `json:"tenant_id"`
	Status         string                  `json:"status"`
	AgentRunID     *string                 `json:"agent_run_id"`
	ConnectorID    string                  `json:"connector_id"`
	OpID           string                  `json:"op_id"`
	TargetID       *string                 `json:"target_id"`
	ParamsHash     string                  `json:"params_hash"`
	ProposedEffect *map[string]interface{} `json:"proposed_effect"`
	PrincipalSub   string                  `json:"principal_sub"`
	PrincipalAct   *string                 `json:"principal_act"`
	ReviewedBy     *string                 `json:"reviewed_by"`
	DecidedAt      *string                 `json:"decided_at"`
	ExpiresAt      *string                 `json:"expires_at"`
	CreatedAt      string                  `json:"created_at"`
	ElicitationURL *string                 `json:"elicitation_url"`
}

// ListResponse mirrors the backend ApprovalListResponse envelope.
type ListResponse struct {
	Items  []ApprovalSummary `json:"items"`
	Total  int               `json:"total"`
	Limit  int               `json:"limit"`
	Offset int               `json:"offset"`
}

// decisionBody is the JSON body for approve / reject calls.
type decisionBody struct {
	Reason *string `json:"reason,omitempty"`
}

// errMissingAccessToken is the sentinel doAuthedRequest returns when the
// stored token row exists but access_token is empty.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the response body the CLI will read.
const responseBodyCap int64 = 1 << 20 // 1 MiB

// httpError carries a non-2xx response.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// doAuthedRequest issues an authenticated HTTP request with one-shot
// 401-refresh-retry. Mirrors the pattern agent / conventions use.
func doAuthedRequest(
	ctx context.Context,
	backplaneURL, method, path string,
	body []byte,
) ([]byte, error) {
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
		return nil, fmt.Errorf("response body exceeds %d-byte cap", responseBodyCap)
	}
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

func sendRequest(
	ctx context.Context,
	client *http.Client,
	base, method, path, bearer string,
	body []byte,
) (*http.Response, error) {
	var reqBody io.Reader
	if len(body) > 0 {
		reqBody = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, base+path, reqBody)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	if len(body) > 0 {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
}

// renderRequestError translates a request error into the right
// output.StructuredError category. Mirrors conventions / agent shape.
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
	var he *httpError
	if errors.As(err, &he) {
		return renderHTTPError(cmd, backplaneURL, he, jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

func renderHTTPError(
	cmd *cobra.Command,
	backplaneURL string,
	he *httpError,
	jsonOut bool,
) error {
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
			output.InsufficientRole(he.Body),
			jsonOut,
		)
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("approval_request_not_found"),
			jsonOut,
		)
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(he.Body),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(he.Body),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d: %s", he.StatusCode, he.Body)),
			jsonOut,
		)
	}
}

