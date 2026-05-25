// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agentprincipal

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRevokeCmd returns the `meho agent-principal revoke <name>` command.
func newRevokeCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "revoke <name>",
		Short: "Revoke an agent principal — kill switch (tenant_admin)",
		Long: "revoke calls DELETE /api/v1/agent-principals/{name}/revoke to " +
			"immediately disable the Keycloak client (no new token grants) " +
			"and mark the DB row revoked. " +
			"In-flight tokens remain valid until their exp. " +
			"Returns 404 when no active principal with that name exists. " +
			"Requires tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRevoke(cmd, revokeOptions{
				Name:              args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw Entry JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type revokeOptions struct {
	Name              string
	JSONOut           bool
	BackplaneOverride string
}

func runRevoke(cmd *cobra.Command, opts revokeOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("revoke requires a non-empty <name> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	entry, err := deleteRevoke(cmd.Context(), backplaneURL, opts.Name)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "revoked agent principal %q\n", entry.Name)
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

func deleteRevoke(ctx context.Context, backplaneURL, name string) (*Entry, error) {
	path := "/api/v1/agent-principals/" + url.PathEscape(name) + "/revoke"
	raw, err := doAuthedRequest(ctx, backplaneURL, "DELETE", path, nil)
	if err != nil {
		return nil, err
	}
	var out Entry
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode revoke response: %w", err)
	}
	return &out, nil
}
