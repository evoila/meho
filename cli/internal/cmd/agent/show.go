// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newShowCmd returns the `meho agent show` command.
//
//	meho agent show <name> [--json] [--backplane <url>]
//
// Role: operator. Fetches one definition via GET /api/v1/agents/{name}.
// A 404 (`agent_not_found`) covers both genuine absence and
// cross-tenant probes — existence is never leaked across tenants.
func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <name>",
		Short: "Fetch one agent definition by name",
		Long: "show calls GET /api/v1/agents/{name} and renders the " +
			"definition as a key-value summary (or the full AgentDefinitionRead " +
			"JSON with --json). A 404 means the name doesn't exist in your " +
			"tenant — the route conflates cross-tenant probes with " +
			"genuine absence so existence is never leaked.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShow(cmd, showOptions{
				Name:              args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AgentDefinitionRead JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type showOptions struct {
	Name              string
	JSONOut           bool
	BackplaneOverride string
}

func runShow(cmd *cobra.Command, opts showOptions) error {
	if opts.Name == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("show requires a non-empty <name> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getDefinition(cmd.Context(), backplaneURL, opts.Name)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printDefinitionSummary(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func getDefinition(ctx context.Context, backplaneURL, name string) (*api.ShowAgentApiV1AgentsNameGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ShowAgentApiV1AgentsNameGetResponse, error) {
			return authed.ShowAgentApiV1AgentsNameGetWithResponse(ctx, name, nil)
		},
		func(r *api.ShowAgentApiV1AgentsNameGetResponse) int { return r.StatusCode() },
	)
}
