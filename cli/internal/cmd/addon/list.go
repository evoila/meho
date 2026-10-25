// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package addon

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

// newListCmd returns the `meho addon list` command.
func newListCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List the add-on pairings of your tenant (operator)",
		Long: "list calls GET /api/v1/addons/pairings and prints one row per " +
			"pairing: name, client id, negotiated contract version, paired at, " +
			"and last seen ('never' when the add-on has not reported yet). " +
			"Role: operator.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw PairedAddonListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getList(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, "", operatorRoleAdvice, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printListTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func getList(
	ctx context.Context,
	backplaneURL string,
) (*api.ListPairingsApiV1AddonsPairingsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListPairingsApiV1AddonsPairingsGetResponse, error) {
			return authed.ListPairingsApiV1AddonsPairingsGetWithResponse(
				ctx,
				&api.ListPairingsApiV1AddonsPairingsGetParams{},
			)
		},
		func(r *api.ListPairingsApiV1AddonsPairingsGetResponse) int { return r.StatusCode() },
	)
}

func printListTable(w io.Writer, r *api.PairedAddonListResponse) {
	if r == nil || len(r.Items) == 0 {
		fmt.Fprintln(w, "no add-ons paired in this tenant")
		return
	}
	fmt.Fprintf(w, "%-24s %-30s %-9s %-21s %s\n",
		"NAME", "CLIENT ID", "CONTRACT", "PAIRED AT", "LAST SEEN")
	for _, e := range r.Items {
		fmt.Fprintf(w, "%-24s %-30s %-9d %-21s %s\n",
			e.Name, e.KeycloakClientId, e.ContractVersion,
			formatTime(e.PairedAt), formatLastSeen(e.LastSeenAt))
	}
}
