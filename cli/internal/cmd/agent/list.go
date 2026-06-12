// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho agent list` command.
//
//	meho agent list [--limit N] [--offset N] [--json] [--backplane <url>]
//
// Role: operator. Lists the operator's tenant's definitions,
// name-sorted, via GET /api/v1/agents.
func newListCmd() *cobra.Command {
	var (
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List agent definitions in your tenant",
		Long: "list calls GET /api/v1/agents and renders the agent " +
			"definitions in the operator's tenant, name-sorted. --limit " +
			"caps the page size (1..500, server default 100). --offset " +
			"advances the page window (default 0). --json emits the raw " +
			"AgentDefinitionListResponse envelope for jq pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max definitions per page (1..500, server default 100 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"offset into the name-sorted result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AgentDefinitionListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	if opts.Offset < 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--offset must be non-negative; got %d", opts.Offset)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printListTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listQueryParams maps the CLI flags onto the generated query-param
// shape. limit / offset are *int because oapi-codegen emits `omitempty`
// pointer fields when the OpenAPI parameter is optional.
func listQueryParams(opts listOptions) *api.ListAgentsApiV1AgentsGetParams {
	params := &api.ListAgentsApiV1AgentsGetParams{}
	if opts.Limit > 0 {
		limit := opts.Limit
		params.Limit = &limit
	}
	if opts.Offset > 0 {
		offset := opts.Offset
		params.Offset = &offset
	}
	return params
}

func getList(ctx context.Context, backplaneURL string, opts listOptions) (*api.ListAgentsApiV1AgentsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListAgentsApiV1AgentsGetResponse, error) {
			return authed.ListAgentsApiV1AgentsGetWithResponse(ctx, params)
		},
		func(r *api.ListAgentsApiV1AgentsGetResponse) int { return r.StatusCode() },
	)
}

// printListTable renders the definitions as a compact table:
// NAME, TIER, BUDGET, ENABLED, IDENTITY.
func printListTable(w io.Writer, r *api.AgentDefinitionListResponse) {
	if r == nil || len(r.Agents) == 0 {
		fmt.Fprintln(w, "no agent definitions registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-32s %-10s %-8s %-8s %s\n", "NAME", "TIER", "BUDGET", "ENABLED", "IDENTITY")
	for _, e := range r.Agents {
		fmt.Fprintf(w, "%-32s %-10s %-8d %-8t %s\n",
			e.Name, e.ModelTier, e.TurnBudget, e.Enabled, e.IdentityRef)
	}
}
