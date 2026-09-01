// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package tenants hosts the cobra commands under `meho tenants ...` for the
// operator-plane per-tenant policy surface (#3272).
//
// v0.2 ships one policy family — the flight-recorder capture policy:
//
//   - `meho tenants flight-recorder-policy set [--enabled] [--agent-readable]
//     [--retention-days N | --clear-retention]` — PATCH
//     /api/v1/tenants/flight-recorder-policy. Tenant-scoped (the caller's own
//     tenant, from the JWT — no tenant id is accepted, so there is no
//     cross-tenant write). tenant_admin only; operator / read_only land as 403
//     insufficient_role.
//
// The verb builds a **sparse** JSON body (only the fields the operator set)
// rather than the generated `TenantFlightRecorderPolicyUpdate` struct: that
// struct's pointer fields carry no `omitempty`, so a nil pointer would marshal
// to an explicit JSON `null` — which the backend reads as "clear to inherit /
// default", not "leave unchanged". The sparse-map approach preserves the
// tri-state null-vs-absent distinction the flight-recorder policy needs (same
// reason `meho targets import` builds sparse PATCH bodies). See
// flight_recorder.go.
//
// Auth + error-classification plumbing mirrors the `meho conventions` package:
// the bearer `meho login` wrote, a one-shot 401-refresh-retry, and the
// structured-error categories (auth_expired / unreachable / insufficient_role
// / unexpected). CLI and agent exercise the same backplane dispatch path.
package tenants

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

// NewRootCmd returns the `meho tenants` parent command, grafted onto the
// top-level meho command tree by cmd/root.go.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "tenants",
		Short:        "Operate per-tenant policy (flight-recorder capture policy)",
		Long:         "Manage the operator's own tenant policy. v0.2 ships the flight-recorder capture policy.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFlightRecorderPolicyCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns when the stored
// token row exists but its access_token is empty. Mirrors the sibling verb
// trees so an operator sees the same `meho login` hint everywhere.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient and verifies a non-empty bearer.
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

// responseBodyCap bounds the response body the CLI will read (the resolved
// policy is a handful of fields; 1 MiB is comfortable adversarial headroom).
const responseBodyCap int64 = 1 << 20

// rawResponse is the (status, body) pair the verbs render after a round-trip.
type rawResponse struct {
	StatusCode int
	Body       []byte
}

func readAllBody(rsp *http.Response) ([]byte, error) {
	defer rsp.Body.Close() //nolint:errcheck
	raw, err := io.ReadAll(io.LimitReader(rsp.Body, responseBodyCap+1))
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}
	if int64(len(raw)) > responseBodyCap {
		return nil, fmt.Errorf("response body exceeds %d-byte cap", responseBodyCap)
	}
	return raw, nil
}

// doRequest invokes call once and, on a 401, runs a one-shot bearer refresh +
// re-issues. Returns the drained (status, body) pair on the final response.
func doRequest(
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*http.Response, error),
) (*rawResponse, error) {
	rsp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if rsp.StatusCode == http.StatusUnauthorized {
		_, _ = io.Copy(io.Discard, rsp.Body)
		rsp.Body.Close() //nolint:errcheck
		if rerr := authed.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		rsp, err = call(ctx)
		if err != nil {
			return nil, err
		}
	}
	raw, err := readAllBody(rsp)
	if err != nil {
		return nil, err
	}
	return &rawResponse{StatusCode: rsp.StatusCode, Body: raw}, nil
}

// renderRequestError maps a transport-layer request error to a structured-error
// category (auth_expired / unreachable).
func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	switch {
	case errors.Is(err, errMissingAccessToken):
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored credentials for %s are incomplete; run `meho login %s`",
				backplaneURL, backplaneURL)),
			jsonOut)
	case api.IsTokenNotFound(err):
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL)),
			jsonOut)
	case api.IsNoRefreshToken(err):
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL)),
			jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
			jsonOut)
	}
}

// renderHTTPStatus classifies a non-2xx response into the right structured
// category. 403 -> insufficient_role (the operator sees the required role);
// 404 -> tenant_not_found / route absent; 422 -> invalid request; else raw.
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
				"backplane rejected the stored token; run `meho login %s`", backplaneURL)),
			jsonOut)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(bodyStr)), jsonOut)
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)), jsonOut)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("invalid request: "+decodeDetailString(bodyStr)), jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s", backplaneURL, statusCode, bodyStr)),
			jsonOut)
	}
}

// detailEnvelope models FastAPI's HTTPException JSON shape ({"detail": ...}),
// where detail is either a plain string or the pydantic validation list.
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// decodeDetailString pulls a plain-string `detail` out of a FastAPI error body,
// falling back to the raw body when the shape doesn't match.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var s string
		if jerr := json.Unmarshal(env.Detail, &s); jerr == nil && s != "" {
			return s
		}
	}
	return strings.TrimSpace(body)
}
