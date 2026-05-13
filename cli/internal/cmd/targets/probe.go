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

func newProbeCmd() *cobra.Command {
	var (
		jsonOut   bool
		backplane string
	)

	cmd := &cobra.Command{
		Use:   "probe <name|alias>",
		Short: "Invoke the connector probe for a target",
		Long: "probe calls POST /api/v1/targets/{name}/probe, which invokes " +
			"the registered connector's probe method.\n\n" +
			"A 501 response means no connector is registered yet for the " +
			"target's product — this is expected before the G3 connector " +
			"for that product lands.",
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

			pr, status, err := client.ProbeTarget(cmd.Context(), name)
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
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unexpected(fmt.Sprintf("target %q not found", name)),
						jsonOut)
				case http.StatusNotImplemented:
					// 501: no connector registered yet for this product — friendly message.
					fmt.Fprintf(cmd.ErrOrStderr(),
						"No connector registered yet for target %q.\n"+
							"The connector for this product lands in G3 — check the initiative tracker.\n",
						name)
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unexpected(fmt.Sprintf("no connector registered for target %q", name)),
						jsonOut)
				default:
					return output.RenderError(cmd.ErrOrStderr(),
						output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
						jsonOut)
				}
			}

			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), pr)
			}
			return output.PrintProbeResult(cmd.OutOrStdout(), pr)
		},
	}

	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw JSON on stdout instead of the human summary")
	cmd.Flags().StringVar(&backplane, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}
