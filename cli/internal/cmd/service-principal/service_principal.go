// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package serviceprincipal hosts service-principal operator commands.
package serviceprincipal

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

// NewRootCmd returns the `meho service-principals` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "service-principals",
		Short:        "Manage service-principal operator surfaces",
		SilenceUsage: true,
	}
	cmd.AddCommand(NewGrantsCmd())
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

func retryOn401[R any](ctx context.Context, authed *api.AuthedClient, call func(context.Context) (*R, error), statusOf func(*R) int) (*R, error) {
	resp, err := call(ctx)
	if err != nil || resp == nil || statusOf(resp) != http.StatusUnauthorized {
		return resp, err
	}
	if err := authed.Refresh(ctx); err != nil {
		return resp, err
	}
	return call(ctx)
}

func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	if errors.Is(err, errMissingAccessToken) || api.IsTokenNotFound(err) || api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(fmt.Sprintf("credentials for %s are unavailable or expired; run `meho login %s`", backplaneURL, backplaneURL)), jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(), output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)), jsonOut)
}

func renderHTTPStatus(cmd *cobra.Command, backplaneURL string, statusCode int, body []byte, jsonOut bool) error {
	detail := decodeDetailString(strings.TrimSpace(string(body)))
	switch statusCode {
	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(fmt.Sprintf("backplane rejected the stored token; run `meho login %s`", backplaneURL)), jsonOut)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(), output.InsufficientRole(detail), jsonOut)
	case http.StatusNotFound, http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(detail), jsonOut)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("invalid request: "+detail), jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s", backplaneURL, statusCode, detail)), jsonOut)
	}
}

func decodeDetailString(body string) string {
	var envelope struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal([]byte(body), &envelope); err == nil {
		var detail string
		if err := json.Unmarshal(envelope.Detail, &detail); err == nil && detail != "" {
			return detail
		}
	}
	return body
}
