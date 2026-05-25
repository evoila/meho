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
	"os"
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

// doAuthedRequest performs an authenticated HTTP request against the backplane
// and returns the raw response body on 2xx. Non-2xx responses are returned as
// a *requestError so callers can map them to user-facing messages.
func doAuthedRequest(ctx context.Context, backplaneURL, method, path string, body []byte) ([]byte, error) {
	token, err := api.LoadToken()
	if err != nil {
		return nil, &requestError{kind: "no_token", detail: err.Error()}
	}
	url := strings.TrimRight(backplaneURL, "/") + path
	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, &requestError{kind: "network_error", detail: err.Error()}
	}
	defer resp.Body.Close() //nolint:errcheck
	rawBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response body: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &requestError{
			status: resp.StatusCode,
			body:   string(rawBody),
		}
	}
	return rawBody, nil
}

type requestError struct {
	kind   string
	detail string
	status int
	body   string
}

func (e *requestError) Error() string {
	if e.status != 0 {
		return fmt.Sprintf("HTTP %d: %s", e.status, e.body)
	}
	return fmt.Sprintf("%s: %s", e.kind, e.detail)
}

func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	var re *requestError
	if errors.As(err, &re) {
		switch {
		case re.kind == "no_token":
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unauthenticated("run `meho login` first"), jsonOut)
		case re.status == 401:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unauthenticated("token expired or invalid; run `meho login`"), jsonOut)
		case re.status == 403:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Forbidden("insufficient_role: tenant_admin required"), jsonOut)
		case re.status == 404:
			return output.RenderError(cmd.ErrOrStderr(),
				output.NotFound("agent_principal_not_found"), jsonOut)
		case re.status == 409:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Conflict("agent_principal_already_exists"), jsonOut)
		case re.status == 503:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("keycloak_admin_not_configured: contact your MEHO administrator"), jsonOut)
		case re.kind == "network_error":
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unreachable(backplaneURL), jsonOut)
		}
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unexpected(err.Error()), jsonOut)
}

func printEntrySummary(w io.Writer, e *Entry) {
	fmt.Fprintf(w, "  id:                  %s\n", e.ID)
	fmt.Fprintf(w, "  keycloak_client_id:  %s\n", e.KeycloakClientID)
	fmt.Fprintf(w, "  owner_sub:           %s\n", e.OwnerSub)
	fmt.Fprintf(w, "  revoked:             %v\n", e.Revoked)
	fmt.Fprintf(w, "  created_at:          %s\n", e.CreatedAt)
}

// loadJSONObjectArg parses a raw string as a JSON object (no @file support
// needed for this surface).
func loadJSONObjectArg(raw string) (map[string]any, error) {
	if raw == "" {
		return nil, nil
	}
	if strings.HasPrefix(raw, "@") {
		path := raw[1:]
		var r io.Reader
		if path == "-" {
			r = os.Stdin
		} else {
			f, err := os.Open(path)
			if err != nil {
				return nil, fmt.Errorf("open %s: %w", path, err)
			}
			defer f.Close() //nolint:errcheck
			r = f
		}
		data, err := io.ReadAll(r)
		if err != nil {
			return nil, fmt.Errorf("read @-file: %w", err)
		}
		raw = string(data)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		return nil, fmt.Errorf("parse JSON: %w", err)
	}
	return m, nil
}
