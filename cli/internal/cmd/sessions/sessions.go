// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package sessions hosts `meho sessions ...`: the admin commands of the
// agent session store (#4901).
//
//   - `meho sessions list [--project P] [--state S] [--since T] [--until T]
//     [--limit N] [--all]` — GET /api/v1/sessions. Role operator and up.
//
//   - `meho sessions show <session-id>` — GET /api/v1/sessions/{id}: the
//     index row, the file hashes and the uploads. Role operator and up.
//
//   - `meho sessions get <session-id> <folder>` — downloads every upload
//     and writes the whole (checked) files under <folder>/<session-id>/.
//     Role operator and up; a quarantined upload needs tenant_admin.
//
//   - `meho sessions delete <session-id> [--confirm]` — DELETE
//     /api/v1/sessions/{id}: the session and its files. tenant_admin only.
//
//   - `meho sessions push [--dry-run] [--outbox DIR]` — POST
//     /api/v1/sessions: upload the sessions of the local outbox, with
//     the person's own login. Any tenant role.
//
// Every call writes an audit row on the server.
package sessions

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho sessions` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "sessions",
		Short: "Upload, list, show, download and delete stored agent sessions",
		Long: "The agent session store keeps a copy of agent sessions that people " +
			"uploaded. push uploads the sessions of your local outbox. The other " +
			"commands list the sessions of your tenant, show one session, download " +
			"one as whole files, and delete one. Listing, showing and downloading " +
			"need the operator role; " +
			"deleting needs tenant_admin. Every call is audited.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newPushCmd())
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newGetCmd())
	cmd.AddCommand(newDeleteCmd())
	return cmd
}

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

// retryOn401 runs call once and, on a 401, refreshes the bearer and runs it again.
func retryOn401[R any](
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*R, error),
	statusOf func(*R) int,
) (*R, error) {
	resp, err := call(ctx)
	if err != nil || resp == nil || statusOf(resp) != http.StatusUnauthorized {
		return resp, err
	}
	if rerr := authed.Refresh(ctx); rerr != nil {
		return resp, rerr
	}
	return call(ctx)
}

func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	switch {
	case errors.Is(err, errMissingAccessToken), api.IsTokenNotFound(err), api.IsNoRefreshToken(err):
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("no usable stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL)),
			jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)), jsonOut)
	}
}

func renderHTTPStatus(cmd *cobra.Command, backplaneURL string, statusCode int, body []byte, jsonOut bool) error {
	detail := decodeDetail(body)
	switch statusCode {
	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("backplane rejected the stored token; run `meho login %s`",
				backplaneURL)),
			jsonOut)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(), output.InsufficientRole(detail), jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d: %s", statusCode, detail)), jsonOut)
	}
}

// decodeDetail returns the plain-string `detail` of a FastAPI error body, or the body.
func decodeDetail(body []byte) string {
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err == nil {
		var s string
		if jerr := json.Unmarshal(env.Detail, &s); jerr == nil && s != "" {
			return s
		}
	}
	return strings.TrimSpace(string(body))
}

func orNone(p *string) string {
	if p == nil || *p == "" {
		return "-"
	}
	return *p
}
