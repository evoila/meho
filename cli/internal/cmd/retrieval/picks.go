// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"context"
	"fmt"
	"io"
	"net/http"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newPicksCmd returns the `meho retrieval picks` subcommand. It wraps
// GET /api/v1/retrieve/operation-picks: per connector and day, how often
// a call used an operation that the search before it returned, and
// whether that call worked.
func newPicksCmd() *cobra.Command {
	var (
		since             string
		connector         string
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)

	cmd := &cobra.Command{
		Use:   "picks",
		Short: "Show if calls used an operation the search returned (per connector and day)",
		Long: "picks calls GET /api/v1/retrieve/operation-picks. For each " +
			"connector and day it shows how many operation searches ran, how " +
			"many calls used an operation from the search results (at rank 1, " +
			"in the top 3, anywhere), how many of those returned ok, and how " +
			"many calls had no search before them.\n\n" +
			"A call is linked to the newest search of the same caller, session " +
			"and connector in the 5 minutes before it.\n\n" +
			"MEHO records searches and calls only when the backplane sets " +
			"REQUEST_USAGE_RETENTION_DAYS. When it is not set, this report is " +
			"empty.\n\n" +
			"--since accepts `30d`, `7d`, `24h` or an ISO-8601 date. --connector " +
			"shows one connector. --tenant shows another tenant (platform admin " +
			"only; others get HTTP 403, exit code 5). --json prints the raw report.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runPicks(cmd, picksOptions{
				Since:             since,
				Connector:         connector,
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}

	cmd.Flags().StringVar(&since, "since", "30d",
		"window start; accepts relative (`30d`, `7d`, `24h`) or ISO-8601 date (`2026-04-01`)")
	cmd.Flags().StringVar(&connector, "connector", "",
		"show only this connector id (for example `k8s-1.x`)")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"tenant UUID (platform admin only; others get a 403)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"print the raw OperationPickReport instead of the table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")

	return cmd
}

type picksOptions struct {
	Since             string
	Connector         string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runPicks(cmd *cobra.Command, opts picksOptions) error {
	params := &api.OperationPicksEndpointApiV1RetrieveOperationPicksGetParams{}
	since := opts.Since
	params.Since = &since
	if opts.Connector != "" {
		connector := opts.Connector
		params.ConnectorId = &connector
	}
	if opts.Tenant != "" {
		var tenantUUID openapi_types.UUID
		if err := tenantUUID.UnmarshalText([]byte(opts.Tenant)); err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("--tenant %q is not a UUID", opts.Tenant)),
				opts.JSONOut,
			)
		}
		params.TenantFilter = &tenantUUID
	}

	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			backplane.ClassifyError(err), opts.JSONOut)
	}

	resp, err := getPicks(cmd.Context(), backplaneURL, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// A 200 without a parsed body must not read as "no records".
	if resp.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without an operation picks report", backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printPicksTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func getPicks(
	ctx context.Context,
	backplaneURL string,
	params *api.OperationPicksEndpointApiV1RetrieveOperationPicksGetParams,
) (*api.OperationPicksEndpointApiV1RetrieveOperationPicksGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.OperationPicksEndpointApiV1RetrieveOperationPicksGetResponse, error) {
			return authed.OperationPicksEndpointApiV1RetrieveOperationPicksGetWithResponse(ctx, params)
		},
		func(r *api.OperationPicksEndpointApiV1RetrieveOperationPicksGetResponse) int {
			return r.StatusCode()
		},
	)
}

// printPicksTable prints one row per day and connector, in the order the
// backplane sends them (by date, then connector).
func printPicksTable(w io.Writer, r *api.OperationPickReport) {
	fmt.Fprintf(w, "Operation picks — tenant: %s\n", r.TenantId.String())
	fmt.Fprintf(w, "window: %s → %s\n",
		r.Since.Format("2006-01-02T15:04:05Z07:00"),
		r.Until.Format("2006-01-02T15:04:05Z07:00"))
	if len(r.Buckets) == 0 {
		fmt.Fprintln(w, "(no records in the window)")
		return
	}
	fmt.Fprintf(w, "%-12s %-24s %8s %8s %6s %7s %6s %7s %7s %7s %7s %9s\n",
		"date", "connector", "searches", "followed", "calls", "rank1", "top3",
		"in_res", "res_ok", "not_res", "unknown", "no_search")
	for _, b := range r.Buckets {
		fmt.Fprintf(w, "%-12s %-24s %8d %8d %6d %7d %6d %7d %7d %7d %7d %9d\n",
			b.Date.String(), b.ConnectorId, b.Searches, b.SearchesFollowedByCall,
			b.Calls, b.CallsAtRank1, b.CallsInTop3, b.CallsInResults,
			b.CallsInResultsOk, b.CallsNotInResults, b.CallsUnknownOp,
			b.CallsWithoutSearch)
	}
}
