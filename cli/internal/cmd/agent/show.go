// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"

	"github.com/spf13/cobra"

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
			"definition as a key-value summary (or the full Entry JSON " +
			"with --json). A 404 means the name doesn't exist in your " +
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
		"emit raw Entry JSON instead of the human summary")
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
	entry, err := getEntry(cmd.Context(), backplaneURL, opts.Name)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

// buildShowPath assembles the GET path. Exposed for unit tests so URL
// encoding of names with dots / hyphens stays covered.
func buildShowPath(name string) string {
	return "/api/v1/agents/" + url.PathEscape(name)
}

func getEntry(ctx context.Context, backplaneURL, name string) (*Entry, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildShowPath(name), nil)
	if err != nil {
		return nil, err
	}
	var out Entry
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode agent show response: %w", err)
	}
	return &out, nil
}
