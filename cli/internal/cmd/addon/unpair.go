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

// newUnpairCmd returns the `meho addon unpair <name>` command.
func newUnpairCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "unpair <name>",
		Short: "Remove an add-on pairing (tenant_admin)",
		Long: "unpair calls DELETE /api/v1/addons/pairings/{name}. The server " +
			"deletes the add-on's login first, then the pairing. The add-on's " +
			"login stops working at once, and the surfaces the add-on declared " +
			"are gone. There is no prompt. Answers 404 addon_not_paired when no " +
			"add-on with this name is paired. Requires tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runUnpair(cmd, unpairOptions{
				Name:              args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a {\"name\",\"unpaired\"} JSON object instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type unpairOptions struct {
	Name              string
	JSONOut           bool
	BackplaneOverride string
}

// unpairResult is the --json answer. The server answers 204 with no body.
type unpairResult struct {
	Name     string `json:"name"`
	Unpaired bool   `json:"unpaired"`
}

func runUnpair(cmd *cobra.Command, opts unpairOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("unpair requires a non-empty <name> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := deleteUnpair(cmd.Context(), backplaneURL, opts.Name)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusNoContent {
		return renderHTTPStatus(cmd, backplaneURL, opts.Name, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), unpairResult{Name: opts.Name, Unpaired: true})
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "unpaired add-on %q: the pairing is removed.\n", opts.Name)
	fmt.Fprintln(w, "The add-on's login no longer works.")
	fmt.Fprintln(w, "The surfaces the add-on declared are gone.")
	return nil
}

func deleteUnpair(
	ctx context.Context,
	backplaneURL, name string,
) (*api.UnpairAddonApiV1AddonsPairingsNameDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.UnpairAddonApiV1AddonsPairingsNameDeleteResponse, error) {
			return authed.UnpairAddonApiV1AddonsPairingsNameDeleteWithResponse(
				ctx,
				name,
				&api.UnpairAddonApiV1AddonsPairingsNameDeleteParams{},
			)
		},
		func(r *api.UnpairAddonApiV1AddonsPairingsNameDeleteResponse) int { return r.StatusCode() },
	)
}
