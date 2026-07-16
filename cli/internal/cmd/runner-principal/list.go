// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runnerprincipal

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

// newListCmd returns the `meho runner-principal list` command.
func newListCmd() *cobra.Command {
	var (
		includeRevoked    bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List runner principals in your tenant",
		Long: "list calls GET /api/v1/runner-principals and renders the " +
			"runner principals registered in the operator's tenant, name-sorted. " +
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

// listQueryParams maps the CLI flags onto the generated query-param shape.
// include_revoked is omitted from the wire when the operator didn't ask
// for it so the backplane's own default (excluding revoked rows) applies.
func listQueryParams(opts listOptions) *api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetParams {
	params := &api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetParams{}
	if opts.IncludeRevoked {
		flag := true
		params.IncludeRevoked = &flag
	}
	return params
}

func getList(
	ctx context.Context,
	backplaneURL string,
	opts listOptions,
) (*api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetResponse, error) {
			return authed.ListRunnerPrincipalsApiV1RunnerPrincipalsGetWithResponse(ctx, params)
		},
		func(r *api.ListRunnerPrincipalsApiV1RunnerPrincipalsGetResponse) int { return r.StatusCode() },
	)
}

func printListTable(w io.Writer, r *api.RunnerPrincipalListResponse) {
	if r == nil || len(r.Principals) == 0 {
		fmt.Fprintln(w, "no runner principals registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-32s %-8s %-20s %s\n", "NAME", "REVOKED", "OWNER", "KEYCLOAK CLIENT ID")
	for _, e := range r.Principals {
		fmt.Fprintf(w, "%-32s %-8v %-20s %s\n",
			e.Name, e.Revoked, e.OwnerSub, e.KeycloakClientId)
	}
}
