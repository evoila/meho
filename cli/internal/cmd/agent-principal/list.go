// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agentprincipal

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho agent-principal list` command.
func newListCmd() *cobra.Command {
	var (
		includeRevoked    bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List agent principals in your tenant",
		Long: "list calls GET /api/v1/agent-principals and renders the " +
			"agent principals registered in the operator's tenant, name-sorted. " +
			"Revoked principals are excluded by default; pass --include-revoked " +
			"to show them too. Role: operator.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				IncludeRevoked:    includeRevoked,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&includeRevoked, "include-revoked", false,
		"include revoked principals in the listing (default false)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	IncludeRevoked    bool
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := fetchList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printListTable(cmd.OutOrStdout(), resp)
	return nil
}

func buildListPath(opts listOptions) string {
	q := url.Values{}
	if opts.IncludeRevoked {
		q.Set("include_revoked", "true")
	}
	path := "/api/v1/agent-principals"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func fetchList(ctx context.Context, backplaneURL string, opts listOptions) (*ListResponse, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out ListResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode agent-principal list response: %w", err)
	}
	return &out, nil
}

func printListTable(w io.Writer, r *ListResponse) {
	if r == nil || len(r.Principals) == 0 {
		fmt.Fprintln(w, "no agent principals registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-32s %-8s %-20s %s\n", "NAME", "REVOKED", "OWNER", "KEYCLOAK CLIENT ID")
	for _, e := range r.Principals {
		fmt.Fprintf(w, "%-32s %-8v %-20s %s\n",
			e.Name, e.Revoked, e.OwnerSub, e.KeycloakClientID)
	}
}
