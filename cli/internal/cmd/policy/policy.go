// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package policy holds the `meho policy ...` commands.
//
// The policy file (`policy: v1`) holds the categories (taxonomy), the
// classifications of connectors, operation groups and operations, and the
// routing of approvals (who may approve which changes). It is a
// file of its own, not a section of the settings file. The commands call
// the `/api/v1/policy` routes; see file.go.
package policy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"unicode"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho policy` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "policy",
		Short: "Manage the policy file (validate / diff / import / export / history)",
		Long: "Manage the policy file (policy: v1): the categories, the " +
			"classifications of connectors, operation groups and operations, " +
			"and the routing of approvals (who may approve which changes). " +
			"A tenant admin works on their own tenant; a platform admin can " +
			"work on the whole install with --scope install. An import " +
			"replaces the whole policy and waits until a second admin " +
			"approves it.",
		SilenceUsage: true,
	}
	addFileCommands(cmd)
	return cmd
}

var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the answer the CLI reads. A policy file is at most
// 1 MiB, and a history holds up to 20 files with their diffs.
const responseBodyCap int64 = 64 << 20

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

// rawResponse is the status and body of one answer.
type rawResponse struct {
	StatusCode int
	Body       []byte
}

// doRequest calls once and, on a 401, renews the token and calls again.
// call must build a fresh request body each time.
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
		if rsp, err = call(ctx); err != nil {
			return nil, err
		}
	}
	defer rsp.Body.Close() //nolint:errcheck
	raw, err := io.ReadAll(io.LimitReader(rsp.Body, responseBodyCap+1))
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}
	if int64(len(raw)) > responseBodyCap {
		return nil, fmt.Errorf("response body exceeds %d-byte cap", responseBodyCap)
	}
	return &rawResponse{StatusCode: rsp.StatusCode, Body: raw}, nil
}

func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	switch {
	case errors.Is(err, errMissingAccessToken), api.IsTokenNotFound(err):
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL)), jsonOut)
	case api.IsNoRefreshToken(err):
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL)), jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)), jsonOut)
}

func renderHTTPStatus(cmd *cobra.Command, backplaneURL string, resp *rawResponse, jsonOut bool) error {
	detail := decodeDetail(resp.Body)
	switch resp.StatusCode {
	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf("backplane rejected the stored token; run `meho login %s`",
				backplaneURL)), jsonOut)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(), output.InsufficientRole(detail), jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("HTTP %d: %s", resp.StatusCode, detail)), jsonOut)
	}
}

// decodeDetail returns the `detail` text of a FastAPI error, else the body.
func decodeDetail(body []byte) string {
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err == nil {
		var s string
		if json.Unmarshal(env.Detail, &s) == nil && s != "" {
			return s
		}
	}
	return strings.TrimSpace(string(body))
}

// sanitize replaces control characters, so text from the server cannot
// move the cursor or change the terminal.
func sanitize(s string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return '�'
		}
		return r
	}, s)
}
