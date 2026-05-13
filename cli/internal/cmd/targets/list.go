// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

func newListCmd() *cobra.Command {
	var (
		product  string
		jsonOut  bool
		backplane string
	)

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List targets in your tenant",
		Long: "list calls GET /api/v1/targets and renders the results as a " +
			"table.\n\n" +
			"Pass --product to filter by product slug (exact match). " +
			"Pass --json to emit the raw JSON array — useful for jq pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := resolveURL(backplane)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.AuthExpired(err.Error()), jsonOut)
			}
			client, err := api.NewAuthedClient(cmd.Context(), backplaneURL, api.AuthedClientOptions{})
			if err != nil {
				if api.IsTokenNotFound(err) {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("no stored credentials for %s; run `meho login %s`", backplaneURL, backplaneURL)),
						jsonOut)
				}
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("build client: %v", err)),
					jsonOut)
			}

			params := &api.ListTargetsParams{}
			if product != "" {
				params.Product = &product
			}
			targets, status, err := client.ListTargets(cmd.Context(), params)
			if err != nil {
				if api.IsNoRefreshToken(err) {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("token expired; run `meho login %s`", backplaneURL)),
						jsonOut)
				}
				if status == http.StatusUnauthorized {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("backplane rejected stored credentials; run `meho login %s`", backplaneURL)),
						jsonOut)
				}
				if status == http.StatusForbidden {
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unexpected("insufficient role: operator role required for targets list"),
						jsonOut)
				}
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
					jsonOut)
			}

			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), targets)
			}
			return output.PrintTargetsTable(cmd.OutOrStdout(), targets)
		},
	}

	cmd.Flags().StringVarP(&product, "product", "p", "", "filter by product slug (exact match)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a JSON array on stdout instead of the human table")
	cmd.Flags().StringVar(&backplane, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}
