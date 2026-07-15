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

// newShowCmd returns the `meho runner-principal show <name>` command.
func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <name>",
		Short: "Show one runner principal by name (operator)",
		Long: "show calls GET /api/v1/runner-principals/{name} and renders the " +
			"one runner principal with that name in the operator's tenant. " +
			"Returns 404 when no principal with that name exists (a cross-tenant " +
			"name probe also lands here per the no-existence-leak posture). " +
			"Role: operator.",
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
		"emit raw RunnerPrincipalRead JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type showOptions struct {
	Name              string
	JSONOut           bool
	BackplaneOverride string
}

func runShow(cmd *cobra.Command, opts showOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("show requires a non-empty <name> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getShow(cmd.Context(), backplaneURL, opts.Name)
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
	fmt.Fprintf(cmd.OutOrStdout(), "runner principal %q\n", entry.Name)
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

func getShow(
	ctx context.Context,
	backplaneURL, name string,
) (*api.ShowRunnerPrincipalApiV1RunnerPrincipalsNameGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ShowRunnerPrincipalApiV1RunnerPrincipalsNameGetResponse, error) {
			return authed.ShowRunnerPrincipalApiV1RunnerPrincipalsNameGetWithResponse(
				ctx,
				name,
				&api.ShowRunnerPrincipalApiV1RunnerPrincipalsNameGetParams{},
			)
		},
		func(r *api.ShowRunnerPrincipalApiV1RunnerPrincipalsNameGetResponse) int { return r.StatusCode() },
	)
}
