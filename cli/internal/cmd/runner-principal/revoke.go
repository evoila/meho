// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runnerprincipal

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRevokeCmd returns the `meho runner-principal revoke <name>` command.
func newRevokeCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "revoke <name>",
		Short: "Revoke a runner principal — kill switch (tenant_admin)",
		Long: "revoke calls DELETE /api/v1/runner-principals/{name}/revoke to " +
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
		"emit raw RunnerPrincipalRead JSON instead of the human summary")
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
	resp, err := deleteRevoke(cmd.Context(), backplaneURL, opts.Name)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	entry := resp.JSON200
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "revoked runner principal %q\n", entry.Name)
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

func deleteRevoke(
	ctx context.Context,
	backplaneURL, name string,
) (*api.RevokeRunnerPrincipalApiV1RunnerPrincipalsNameRevokeDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RevokeRunnerPrincipalApiV1RunnerPrincipalsNameRevokeDeleteResponse, error) {
			return authed.RevokeRunnerPrincipalApiV1RunnerPrincipalsNameRevokeDeleteWithResponse(
				ctx,
				name,
				&api.RevokeRunnerPrincipalApiV1RunnerPrincipalsNameRevokeDeleteParams{},
			)
		},
		func(r *api.RevokeRunnerPrincipalApiV1RunnerPrincipalsNameRevokeDeleteResponse) int {
			return r.StatusCode()
		},
	)
}
