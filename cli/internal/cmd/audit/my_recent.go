// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newMyRecentCmd returns the `meho audit my-recent` command.
//
// CLI shape (per issue #467):
//
//	meho audit my-recent [--since DUR] [--limit N] [--json] [--backplane <url>]
//
// Calls GET /api/v1/audit/my-recent. The backend reads the operator's
// `sub` claim from the verified JWT and binds it as the `principal`
// filter — there is no surface that accepts an operator override.
// Cross-operator inspection is `meho audit query --principal P`.
//
// Exit codes mirror `meho audit query`.
func newMyRecentCmd() *cobra.Command {
	var (
		since             string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "my-recent",
		Short: "Show your own recent audit activity",
		Long: "my-recent calls GET /api/v1/audit/my-recent and renders the " +
			"audit rows the calling operator produced. The `principal` " +
			"filter is taken from the JWT's `sub` claim server-side — " +
			"there is no surface that accepts an operator override. " +
			"--since defaults to 24h server-side; pass a different " +
			"shorthand (7d / 30m / 2w) or an ISO-8601 datetime to widen " +
			"the window. --limit caps the page size (1..1000, server " +
			"default 100). --json emits the raw AuditQueryResult.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runMyRecent(cmd, myRecentOptions{
				Since:             since,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&since, "since", "",
		"earliest occurred_at; defaults server-side to 24h when omitted")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max rows (1..1000, server default 100 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AuditQueryResult JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type myRecentOptions struct {
	Since             string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runMyRecent(cmd *cobra.Command, opts myRecentOptions) error {
	if opts.Limit < 0 || opts.Limit > 1000 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 1000; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	rawBody, result, err := getMyRecent(cmd.Context(), client, opts)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against AuditQueryResult"),
			opts.JSONOut,
		)
	}
	printQueryTable(cmd.OutOrStdout(), result)
	return nil
}

// buildMyRecentParams assembles the typed-client params struct from
// the per-call options. The query-param fields are pointer-typed so
// nil means "don't emit on the wire" — the backend then applies its
// own defaults (since=24h, server-side limit).
func buildMyRecentParams(opts myRecentOptions) *api.MyRecentApiV1AuditMyRecentGetParams {
	params := &api.MyRecentApiV1AuditMyRecentGetParams{}
	if opts.Since != "" {
		v := opts.Since
		params.Since = &v
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	return params
}

// getMyRecent drives the typed-client
// `MyRecentApiV1AuditMyRecentGet` endpoint with the same one-shot
// 401-retry shape `postQuery` uses.
func getMyRecent(
	ctx context.Context,
	client *api.AuthedClient,
	opts myRecentOptions,
) ([]byte, *api.AuditQueryResult, error) {
	params := buildMyRecentParams(opts)
	resp, err := client.MyRecentApiV1AuditMyRecentGetWithResponse(ctx, params)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.MyRecentApiV1AuditMyRecentGetWithResponse(ctx, params)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}
