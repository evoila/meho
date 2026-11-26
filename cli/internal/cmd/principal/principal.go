// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package principal hosts the commands of `meho principal ...`: the admin
// block list. A blocked login is refused by MEHO on every request, within
// a few seconds. The commands wrap /api/v1/principals/blocked.
package principal

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
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/cmd/approvals"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho principal` parent command.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "principal",
		Short: "Block and unblock people in MEHO (tenant_admin)",
		Long: "Block a person in MEHO, so that every request of that login is " +
			"refused within a few seconds. A platform admin blocks in every " +
			"tenant. A tenant admin blocks in their own tenant. Nobody blocks " +
			"their own login. Only a person can use these commands; an agent " +
			"cannot.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newBlockCmd(), newUnblockCmd(), newBlockedCmd())
	return cmd
}

type commonFlags struct {
	jsonOut           bool
	backplaneOverride string
}

func (f *commonFlags) add(cmd *cobra.Command) {
	cmd.Flags().BoolVar(&f.jsonOut, "json", false, "emit raw JSON instead of the human summary")
	cmd.Flags().StringVar(&f.backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
}

func newBlockCmd() *cobra.Command {
	var flags commonFlags
	var reason string
	cmd := &cobra.Command{
		Use:   "block <sub> --reason <text>",
		Short: "Block a login or an agent in MEHO (tenant_admin)",
		Long: "block calls POST /api/v1/principals/blocked. A <sub> that starts with " +
			"agent: names an agent: copy it from the agent list (agent:<name>). It " +
			"stops every token of that agent. MEHO refuses an agent name that names " +
			"no agent in your reach, and stores no block. Every other <sub> is a " +
			"login id: MEHO stores its block, and it holds from the next request " +
			"of that login. The reason is kept in the audit log and in the block " +
			"list. The blocked login sees no reason.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			if strings.TrimSpace(reason) == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("block needs --reason <text>"), flags.jsonOut)
			}
			url, err := backplane.Resolve(flags.backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), flags.jsonOut)
			}
			resp, err := postBlock(cmd.Context(), url, api.BlockCreate{Sub: args[0], Reason: reason})
			if err != nil {
				return renderRequestError(cmd, url, err, flags.jsonOut)
			}
			if resp.StatusCode() != http.StatusCreated {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, flags.jsonOut)
			}
			if flags.jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON201)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "blocked %s (scope %s)\n", resp.JSON201.Sub, scopeText(resp.JSON201.Scope))
			fmt.Fprintln(cmd.OutOrStdout(), resp.JSON201.Message)
			return nil
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "why the login is blocked (required)")
	flags.add(cmd)
	return cmd
}

func newUnblockCmd() *cobra.Command {
	var flags commonFlags
	var reason string
	cmd := &cobra.Command{
		Use:   "unblock <sub>",
		Short: "Unblock a person in MEHO (tenant_admin; a platform admin for their own block)",
		Long: "unblock calls DELETE /api/v1/principals/blocked/{sub}. A tenant admin " +
			"removes the block of their own tenant. A platform admin with tenant_admin " +
			"removes every block of another person's login. A platform admin whom a " +
			"tenant admin blocked removes that block on their own login, in the tenant " +
			"of the block. This needs a reason (--reason) and a fresh sign-in in the " +
			"browser: unblock opens your browser and asks you to sign in again. Your " +
			"`meho login` token is not enough for it. MEHO writes the reason and the " +
			"sign-in to the audit log.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			url, err := backplane.Resolve(flags.backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), flags.jsonOut)
			}
			authed, err := newAuthedClient(cmd.Context(), url)
			if err != nil {
				return renderRequestError(cmd, url, err, flags.jsonOut)
			}
			var why *string
			if cmd.Flags().Changed("reason") {
				why = &reason
			}
			return runUnblock(cmd, authed, url, args[0], why, flags.jsonOut)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "",
		"why the block is removed (required on your own login; kept in the audit log)")
	flags.add(cmd)
	return cmd
}

// runUnblock sends the unblock and writes the answer.
func runUnblock(cmd *cobra.Command, authed *api.AuthedClient, url, sub string, reason *string, jsonOut bool) error {
	resp, err := deleteBlock(cmd.Context(), authed, sub, reason)
	var noBrowser *auth.NoBrowserError
	if errors.As(err, &noBrowser) {
		return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(
			"this unblock needs a fresh sign-in, and no browser could be opened. "+
				"Run this command on a computer where a browser can open. "+
				"Sign-in link: "+noBrowser.URL), jsonOut)
	}
	if err != nil {
		return renderRequestError(cmd, url, err, jsonOut)
	}
	if resp.StatusCode() != http.StatusOK {
		detail := decodeDetailString(strings.TrimSpace(string(resp.Body)))
		switch {
		case resp.StatusCode() == http.StatusUnprocessableEntity && strings.HasPrefix(detail, reasonRequired):
			return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(
				"Removing a block on your own login needs a reason: add --reason <text>."), jsonOut)
		case resp.StatusCode() == statusTokenRejected && strings.HasPrefix(detail, freshSignInRequired):
			return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(detail), jsonOut)
		}
		return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "unblocked %s\n", resp.JSON200.Sub)
	return nil
}

func newBlockedCmd() *cobra.Command {
	var flags commonFlags
	cmd := &cobra.Command{
		Use:   "blocked",
		Short: "List the blocked people (tenant_admin)",
		Long: "blocked calls GET /api/v1/principals/blocked. A platform admin " +
			"sees every block. A tenant admin sees the blocks of their tenant.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			url, err := backplane.Resolve(flags.backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), flags.jsonOut)
			}
			resp, err := getBlocked(cmd.Context(), url)
			if err != nil {
				return renderRequestError(cmd, url, err, flags.jsonOut)
			}
			if resp.StatusCode() != http.StatusOK {
				return renderHTTPStatus(cmd, url, resp.StatusCode(), resp.Body, flags.jsonOut)
			}
			if flags.jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
			}
			printBlocked(cmd.OutOrStdout(), resp.JSON200.Blocked)
			return nil
		},
	}
	flags.add(cmd)
	return cmd
}

func scopeText(scope string) string {
	if scope == "*" {
		return "all tenants"
	}
	return "tenant " + scope
}

func printBlocked(w io.Writer, rows []api.BlockRead) {
	if len(rows) == 0 {
		fmt.Fprintln(w, "nobody is blocked")
		return
	}
	fmt.Fprintf(w, "%-40s %-24s %-14s %-20s %s\n", "SUB", "AGENT", "SCOPE", "BLOCKED AT", "REASON")
	for _, r := range rows {
		scope := "all"
		if r.Scope != "*" {
			scope = "tenant"
		}
		agent := "-"
		if r.Agent != nil && *r.Agent != "" {
			agent = *r.Agent
		}
		fmt.Fprintf(w, "%-40s %-24s %-14s %-20s %s\n", r.Sub, agent, scope,
			r.BlockedAt.UTC().Format("2006-01-02T15:04:05Z"), r.Reason)
	}
}

func getBlocked(ctx context.Context, url string) (*api.ListBlockedPrincipalsApiV1PrincipalsBlockedGetResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListBlockedPrincipalsApiV1PrincipalsBlockedGetResponse, error) {
			return authed.ListBlockedPrincipalsApiV1PrincipalsBlockedGetWithResponse(
				ctx, &api.ListBlockedPrincipalsApiV1PrincipalsBlockedGetParams{})
		},
		func(r *api.ListBlockedPrincipalsApiV1PrincipalsBlockedGetResponse) int { return r.StatusCode() },
	)
}

func postBlock(ctx context.Context, url string, body api.BlockCreate) (*api.BlockPrincipalRouteApiV1PrincipalsBlockedPostResponse, error) {
	authed, err := newAuthedClient(ctx, url)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.BlockPrincipalRouteApiV1PrincipalsBlockedPostResponse, error) {
			return authed.BlockPrincipalRouteApiV1PrincipalsBlockedPostWithResponse(
				ctx, &api.BlockPrincipalRouteApiV1PrincipalsBlockedPostParams{}, body)
		},
		func(r *api.BlockPrincipalRouteApiV1PrincipalsBlockedPostResponse) int { return r.StatusCode() },
	)
}

// deleteBlock sends the unblock with the stored login. When MEHO asks
// for a fresh sign-in (the unblock of the person's own login), it opens
// the browser approval sign-in, as `meho approvals approve` does, and
// sends the call once more with that token. The token stays in memory.
func deleteBlock(ctx context.Context, authed *api.AuthedClient, sub string, reason *string) (*api.UnblockPrincipalRouteApiV1PrincipalsBlockedSubDeleteResponse, error) {
	call := func(ctx context.Context, editors ...api.RequestEditorFn) (*api.UnblockPrincipalRouteApiV1PrincipalsBlockedSubDeleteResponse, error) {
		return authed.UnblockPrincipalRouteApiV1PrincipalsBlockedSubDeleteWithResponse(
			ctx, sub, &api.UnblockPrincipalRouteApiV1PrincipalsBlockedSubDeleteParams{},
			api.UnblockBody{Reason: reason}, editors...)
	}
	statusOf := func(r *api.UnblockPrincipalRouteApiV1PrincipalsBlockedSubDeleteResponse) int { return r.StatusCode() }
	resp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != statusTokenRejected {
		return resp, nil
	}
	if !approvals.NeedsFreshSignIn(resp.HTTPResponse) {
		return retryOn401(ctx, authed, func(ctx context.Context) (*api.UnblockPrincipalRouteApiV1PrincipalsBlockedSubDeleteResponse, error) {
			return call(ctx)
		}, statusOf)
	}
	src, err := startFreshSignIn(ctx, authed)
	if err != nil {
		return nil, err
	}
	tok, err := src.Token()
	if err != nil {
		return nil, err
	}
	if tok.AccessToken == "" {
		return nil, errors.New("meho: the browser sign-in returned no access token")
	}
	withSignIn := func(_ context.Context, req *http.Request) error {
		req.Header.Set("Authorization", "Bearer "+tok.AccessToken)
		return nil
	}
	return call(ctx, withSignIn)
}

// startFreshSignIn opens the browser approval sign-in. A package variable
// so tests can stand in for the browser and the identity provider.
var startFreshSignIn = approvals.BrowserApprovalSignIn

// The machine-readable prefixes of the unblock refusals the command explains.
const (
	reasonRequired      = "unblock_reason_required"
	freshSignInRequired = "fresh_sign_in_required"
)

// statusTokenRejected is HTTP 401: the backplane did not accept the stored token.
const statusTokenRejected = 401

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
	if err != nil || resp == nil || statusOf(resp) != statusTokenRejected {
		return resp, err
	}
	if err := authed.Refresh(ctx); err != nil {
		return resp, err
	}
	return call(ctx)
}

func renderRequestError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	if errors.Is(err, errMissingAccessToken) || api.IsTokenNotFound(err) || api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(fmt.Sprintf(
			"credentials for %s are unavailable or expired; run `meho login %s`", backplaneURL, backplaneURL)), jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(), output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)), jsonOut)
}

func renderHTTPStatus(cmd *cobra.Command, backplaneURL string, statusCode int, body []byte, jsonOut bool) error {
	detail := decodeDetailString(strings.TrimSpace(string(body)))
	switch statusCode {
	case statusTokenRejected:
		return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(fmt.Sprintf(
			"backplane rejected the stored token; run `meho login %s`", backplaneURL)), jsonOut)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(), output.InsufficientRole(detail), jsonOut)
	case http.StatusNotFound, http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(detail), jsonOut)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("invalid request: "+detail), jsonOut)
	default:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf(
			"call %s: HTTP %d: %s", backplaneURL, statusCode, detail)), jsonOut)
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
		var errs []struct {
			Type string `json:"type"`
			Msg  string `json:"msg"`
		}
		if err := json.Unmarshal(envelope.Detail, &errs); err == nil && len(errs) > 0 {
			parts := make([]string, 0, len(errs))
			for _, e := range errs {
				if e.Type != "" && e.Msg != "" {
					parts = append(parts, e.Type+": "+e.Msg)
				}
			}
			if len(parts) > 0 {
				return strings.Join(parts, "; ")
			}
		}
	}
	return body
}
