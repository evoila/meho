// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
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
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	entries, err := listOverrides(cmd.Context(), client, opts.OpIDPattern)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entries)
	}
	printOverridesTable(cmd.OutOrStdout(), entries)
	return nil
}

// listOverrides drives the typed-client
// `ListOverridesApiV1BroadcastOverridesGet` endpoint with a one-shot
// 401-retry around the underlying AuthedClient's refresh path
// (mirrors `AuthedClient.GetHealth`'s pattern in client.go). Non-2xx
// responses are returned as `*httpResponseError` so the caller can
// route them through `renderHTTPStatus`; transport-layer errors
// return verbatim.
func listOverrides(
	ctx context.Context,
	client *api.AuthedClient,
	opIDPattern string,
) ([]api.BroadcastOverrideRead, error) {
	params := &api.ListOverridesApiV1BroadcastOverridesGetParams{}
	if opIDPattern != "" {
		p := opIDPattern
		params.OpIdPattern = &p
	}
	resp, err := client.ListOverridesApiV1BroadcastOverridesGetWithResponse(ctx, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.ListOverridesApiV1BroadcastOverridesGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		// 2xx without a JSON200 means the response body didn't
		// decode against the list envelope -- treat as the empty
		// list rather than NPE'ing.
		return nil, nil
	}
	return resp.JSON200.Items, nil
}

func printOverridesTable(w io.Writer, entries []api.BroadcastOverrideRead) {
	if len(entries) == 0 {
		fmt.Fprintln(w, "(no broadcast-detail overrides in this tenant)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-30s  %-12s  %-20s  %-9s  %s\n",
		"id", "op_id_pattern", "scope_field", "scope_value", "detail", "created_by")
	fmt.Fprintln(w, "  ---")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-30s  %-12s  %-20s  %-9s  %s\n",
			e.Id.String(),
			e.OpIdPattern,
			strDerefOrDash(e.ScopeField),
			strDerefOrDash(e.ScopeValue),
			e.Detail,
			e.CreatedBySub,
		)
	}
}

// strDerefOrDash renders an optional string field as "-" when nil
// or empty, otherwise as the underlying value. Used by both the
// list table and the set summary so an op-wide rule (scope_field /
// scope_value both null) renders consistently across verbs.
func strDerefOrDash(s *string) string {
	if s == nil || *s == "" {
		return "-"
	}
	return *s
}
