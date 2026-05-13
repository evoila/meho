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

func newDescribeCmd() *cobra.Command {
	var (
		jsonOut   bool
		backplane string
	)

	cmd := &cobra.Command{
		Use:   "describe <name|alias>",
		Short: "Describe a target by name or alias",
		Long: "describe calls GET /api/v1/targets/{name} using alias-aware " +
			"resolution.\n\n" +
			"Pass the canonical name or any registered alias. On 404, " +
			"near-miss suggestions are printed to stderr.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
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

			target, status, detail, err := client.DescribeTarget(cmd.Context(), name)
			if err != nil {
				if api.IsNoRefreshToken(err) {
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("token expired; run `meho login %s`", backplaneURL)),
						jsonOut)
				}
				switch status {
				case http.StatusUnauthorized:
					return output.RenderError(cmd.ErrOrStderr(),
						output.AuthExpired(fmt.Sprintf("backplane rejected stored credentials; run `meho login %s`", backplaneURL)),
						jsonOut)
				case http.StatusForbidden:
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unexpected("insufficient role: operator role required"),
						jsonOut)
				case http.StatusNotFound:
					if detail != nil {
						output.PrintTargetNearMisses(cmd.ErrOrStderr(), name, detail.Matches)
					} else {
						fmt.Fprintf(cmd.ErrOrStderr(), "Target %q not found.\n", name)
					}
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unexpected(fmt.Sprintf("target %q not found", name)),
						jsonOut)
				case http.StatusConflict:
					if detail != nil {
						output.PrintAmbiguousTarget(cmd.ErrOrStderr(), name, detail.Matches)
					}
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unexpected(fmt.Sprintf("ambiguous query %q: use the canonical name", name)),
						jsonOut)
				default:
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
						jsonOut)
				}
			}

			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), target)
			}
			return output.PrintTarget(cmd.OutOrStdout(), target)
		},
	}

	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw JSON on stdout instead of the human summary")
	cmd.Flags().StringVar(&backplane, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}
