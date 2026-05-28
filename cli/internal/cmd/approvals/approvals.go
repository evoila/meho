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
//     POST /api/v1/approvals/{id}/decide. Role: operator. Resumes the
//     paused agent run via the T4 path.
//   - `meho approvals reject <id> [--reason TEXT] [--json]` — reject via
//     POST /api/v1/approvals/{id}/decide. Role: operator. Aborts the
//     paused agent run.
//
// Authentication piggybacks on the token meho login wrote — same pattern
// as `meho agent`, `meho audit`, `meho conventions`.
//
// G0.12-T1 #1251 migrated this package off the sibling-verb pattern of
// hand-rolled HTTP + hand-typed copies of backend pydantic models.
// Every verb here drives the generated `api.ClientWithResponses`
// surface directly: `api.NewAuthedClient` wires the bearer + lazy
// 401-refresh editor onto the embedded `ClientWithResponses`, and
// the verbs call the typed `*WithResponse` methods
// (`ListApprovalsApiV1ApprovalsGetWithResponse` etc.). Consumer-side
// struct drift — the #1069 root cause — can't recur because we now
// consume `api.ApprovalRequestView` directly.
package approvals

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// httpResponseError carries a non-2xx status from a typed-client
// `*WithResponse` call up to the verb's renderer. The typed-client
// surface returns non-2xx responses in-band on the `(*Response, nil)`
// tuple (transport-layer failures come back on the `(nil, err)`
// tuple instead) — we lift the HTTP-failure case to an error type so
// the call sites can use a single `if err != nil` branch and
// `errors.As` routes the right way (HTTP status → `renderHTTPStatus`,
// everything else → `renderTransportError`). See `routeRequestError`.
type httpResponseError struct {
	statusCode int
	body       []byte
}

func (e *httpResponseError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.statusCode, trimmedBody(e.body))
}

// routeRequestError is the single dispatcher every verb feeds an
// error from `fetchList` / `fetchDetail` / `postDecision` into. The
// error is either an `*httpResponseError` (the backplane responded
// with a non-2xx status) or a transport-layer failure (network,
// refresh-impossible, etc.); we route the former through
// `renderHTTPStatus` and the latter through `renderTransportError`.
func routeRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	var he *httpResponseError
	if errors.As(err, &he) {
		return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, jsonOut)
	}
	return renderTransportError(cmd, backplaneURL, err, jsonOut)
}

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

// newAuthedClient builds an `api.AuthedClient` and surfaces its
// construction-time errors as the right `output.StructuredError`
// category (auth_expired when no token was ever stored, else
// unexpected_response with the underlying error wrapped). Splits the
// boilerplate every verb here used to duplicate inline.
func newAuthedClient(
	ctx context.Context,
	cmd *cobra.Command,
	backplaneURL string,
	jsonOut bool,
) (*api.AuthedClient, error) {
	client, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, renderClientError(cmd, backplaneURL, err, jsonOut)
	}
	return client, nil
}

// renderClientError maps `api.NewAuthedClient` failures onto the
// structured-error envelope. `IsTokenNotFound` is the "operator
// never ran meho login" sentinel and surfaces as auth_expired with a
// `meho login` hint; anything else is a build-time failure of the
// authed transport itself (token store unreadable, etc.) and
// surfaces as unexpected_response so the operator sees the cause.
func renderClientError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unexpected(fmt.Sprintf("build authed client for %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderTransportError maps a generated-client call's transport-layer
// error (network failure, refresh-impossible after a 401) onto the
// right structured-error category. The typed-client surface returns
// `(nil, err)` for these; non-2xx HTTP responses arrive as
// `(*Response, nil)` and are routed through `renderHTTPStatus` instead.
func renderTransportError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
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

// renderHTTPStatus maps a non-2xx HTTP status from the typed-client
// response onto the right structured-error category. Mirrors the
// pre-migration `renderHTTPError` shape (401→AuthExpired,
// 403→InsufficientRole, 404→approval_request_not_found, 409/422→
// Unexpected(body), other non-2xx→Unexpected(HTTP N: body)) but acts
// on the (StatusCode, Body) pair lifted off the generated
// `*Response.HTTPResponse` + `Body` fields rather than a sentinel
// `httpError` value.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	bodyStr := trimmedBody(body)
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
			output.InsufficientRole(bodyStr),
			jsonOut,
		)
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("approval_request_not_found"),
			jsonOut,
		)
	case http.StatusConflict, http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(bodyStr),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d: %s", statusCode, bodyStr)),
			jsonOut,
		)
	}
}

// trimmedBody renders a response body for inclusion in an error
// envelope: trims trailing whitespace, surfaces a placeholder when
// the backend returned an empty body so the operator-facing string
// is never just "HTTP 500:".
func trimmedBody(body []byte) string {
	s := string(body)
	// Strip trailing whitespace only — leading whitespace inside a
	// JSON envelope is legitimate. Same shape as
	// strings.TrimRightFunc(s, unicode.IsSpace) but no extra import.
	for len(s) > 0 {
		last := s[len(s)-1]
		if last == ' ' || last == '\n' || last == '\r' || last == '\t' {
			s = s[:len(s)-1]
			continue
		}
		break
	}
	if s == "" {
		return "(empty body)"
	}
	return s
}
