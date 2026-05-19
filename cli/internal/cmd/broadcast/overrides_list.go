// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

func newOverridesListCmd() *cobra.Command {
	var (
		opIDPattern       string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List broadcast-detail override rules for the operator's tenant",
		Long: "list calls GET /api/v1/broadcast/overrides and renders the " +
			"operator's tenant's rules as a human-readable table. " +
			"--op-id-pattern filters by exact pattern (not glob match). " +
			"--json emits the raw JSON array.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runOverridesList(cmd, overridesListOptions{
				OpIDPattern:       opIDPattern,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&opIDPattern, "op-id-pattern", "",
		"exact-match filter on op_id_pattern (the rule's stored pattern, not a glob match)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw JSON array instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type overridesListOptions struct {
	OpIDPattern       string
	JSONOut           bool
	BackplaneOverride string
}

func runOverridesList(cmd *cobra.Command, opts overridesListOptions) error {
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	entries, err := listOverrides(cmd.Context(), backplaneURL, opts.OpIDPattern)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entries)
	}
	printOverridesTable(cmd.OutOrStdout(), entries)
	return nil
}

// buildListPath assembles the GET path including the optional
// query parameter. Exposed for unit tests so URL encoding stays
// covered when the pattern contains special characters.
func buildListPath(opIDPattern string) string {
	if opIDPattern == "" {
		return "/api/v1/broadcast/overrides"
	}
	q := url.Values{}
	q.Set("op_id_pattern", opIDPattern)
	return "/api/v1/broadcast/overrides?" + q.Encode()
}

func listOverrides(ctx context.Context, backplaneURL, opIDPattern string) ([]Entry, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListPath(opIDPattern), nil)
	if err != nil {
		return nil, err
	}
	var out []Entry
	if jerr := json.Unmarshal(raw, &out); jerr != nil {
		return nil, fmt.Errorf("decode broadcast overrides list: %w", jerr)
	}
	return out, nil
}

func printOverridesTable(w io.Writer, entries []Entry) {
	if len(entries) == 0 {
		fmt.Fprintln(w, "(no broadcast-detail overrides in this tenant)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-30s  %-12s  %-20s  %-9s  %s\n",
		"id", "op_id_pattern", "scope_field", "scope_value", "detail", "created_by")
	fmt.Fprintln(w, "  ---")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-30s  %-12s  %-20s  %-9s  %s\n",
			e.ID,
			e.OpIDPattern,
			strDerefOrDash(e.ScopeField),
			strDerefOrDash(e.ScopeValue),
			e.Detail,
			e.CreatedBySub,
		)
	}
}
