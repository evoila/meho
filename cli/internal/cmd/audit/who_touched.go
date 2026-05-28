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

// newWhoTouchedCmd returns the `meho audit who-touched <target>`
// command.
//
// CLI shape (per issue #467):
//
//	meho audit who-touched <target> \
//	  [--since DUR]    # 24h | 7d | ISO-8601, default 24h
//	  [--limit N]      # 1..1000, server default 100
//	  [--json]
//	  [--backplane <url>]
//
// Calls GET /api/v1/audit/who-touched/{target}. The backend resolves
// the target name against the same-tenant `targets` table; an
// unmatched name returns an empty result (not an error).
//
// Exit codes mirror `meho audit query`.
func newWhoTouchedCmd() *cobra.Command {
	var (
		since             string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "who-touched <target>",
		Short: "Show every audit row that touched a specific target",
		Long: "who-touched calls GET /api/v1/audit/who-touched/{target} " +
			"and renders the audit rows where target_name matches the " +
			"argument inside the operator's tenant. --since defaults to " +
			"24h; pass a different shorthand (7d / 30m / 2w) or an " +
			"ISO-8601 datetime to widen the window. --limit caps the " +
			"page size (1..1000, server default 100). --json emits the " +
			"raw AuditQueryResult.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runWhoTouched(cmd, whoTouchedOptions{
				Target:            args[0],
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

type whoTouchedOptions struct {
	Target            string
	Since             string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runWhoTouched(cmd *cobra.Command, opts whoTouchedOptions) error {
	if opts.Target == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("who-touched requires a non-empty <target> argument"),
			opts.JSONOut,
		)
	}
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
	rawBody, result, err := getWhoTouched(cmd.Context(), client, opts)
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

// buildWhoTouchedParams assembles the typed-client params struct
// from the per-call options. The target name passes as the typed
// path parameter on the call site; only the query-string params
// `since` / `limit` land on this struct.
func buildWhoTouchedParams(opts whoTouchedOptions) *api.WhoTouchedApiV1AuditWhoTouchedTargetGetParams {
	params := &api.WhoTouchedApiV1AuditWhoTouchedTargetGetParams{}
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

// getWhoTouched drives the typed-client
// `WhoTouchedApiV1AuditWhoTouchedTargetGet` endpoint with the same
// one-shot 401-retry shape `postQuery` uses. The `target` argument
// passes as the typed path parameter — the generated request
// builder URL-encodes the segment.
func getWhoTouched(
	ctx context.Context,
	client *api.AuthedClient,
	opts whoTouchedOptions,
) ([]byte, *api.AuditQueryResult, error) {
	params := buildWhoTouchedParams(opts)
	resp, err := client.WhoTouchedApiV1AuditWhoTouchedTargetGetWithResponse(ctx, opts.Target, params)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.WhoTouchedApiV1AuditWhoTouchedTargetGetWithResponse(ctx, opts.Target, params)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}
