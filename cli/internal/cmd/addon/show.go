// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package addon

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newShowCmd returns the `meho addon show <name>` command.
func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <name>",
		Short: "Show one add-on pairing by name (operator)",
		Long: "show calls GET /api/v1/addons/pairings/{name} and prints all " +
			"fields of the pairing with that name in your tenant. " +
			"Answers 404 addon_not_paired when no add-on with this name is " +
			"paired. Role: operator.",
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
		"emit raw PairedAddonRead JSON instead of the human summary")
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
		return renderHTTPStatus(cmd, backplaneURL, opts.Name, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	entry := resp.JSON200
	if entry == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("show: empty response body"), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "add-on pairing %q\n", entry.Name)
	printPairingFields(cmd.OutOrStdout(), entry)
	return nil
}

func getShow(
	ctx context.Context,
	backplaneURL, name string,
) (*api.ShowPairingApiV1AddonsPairingsNameGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ShowPairingApiV1AddonsPairingsNameGetResponse, error) {
			return authed.ShowPairingApiV1AddonsPairingsNameGetWithResponse(
				ctx,
				name,
				&api.ShowPairingApiV1AddonsPairingsNameGetParams{},
			)
		},
		func(r *api.ShowPairingApiV1AddonsPairingsNameGetResponse) int { return r.StatusCode() },
	)
}
