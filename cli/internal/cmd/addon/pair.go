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

// secretNote is printed after the client secret. The server cannot show
// the secret a second time.
const secretNote = "Shown only once. MEHO cannot show it again. " +
	"Give the client id and the secret to the add-on now."

// newPairCmd returns the `meho addon pair <name>` command.
func newPairCmd() *cobra.Command {
	var (
		contractVersion     int
		minBackplaneVersion int
		ownerSub            string
		jsonOut             bool
		backplaneOverride   string
	)
	cmd := &cobra.Command{
		Use:   "pair <name>",
		Short: "Pair an add-on with MEHO (tenant_admin)",
		Long: "pair calls POST /api/v1/addons/pairings. It creates a login for " +
			"the add-on (client id 'addon:<name>') and prints the client id and " +
			"the client secret once. MEHO cannot show the secret again. " +
			"The contract version is the version of the rules MEHO and the " +
			"add-on use to talk to each other. Both numbers come from the " +
			"add-on: --contract-version is the contract version it speaks, and " +
			"--min-backplane-version is the oldest MEHO contract version it " +
			"accepts. Today MEHO speaks contract version 1. " +
			"The secret goes to stdout only. Use --json to pass the whole " +
			"answer to a secret store. Requires tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := checkVersionFlags(cmd, jsonOut); err != nil {
				return err
			}
			return runPair(cmd, pairOptions{
				Name:                args[0],
				ContractVersion:     contractVersion,
				MinBackplaneVersion: minBackplaneVersion,
				OwnerSub:            ownerSub,
				JSONOut:             jsonOut,
				BackplaneOverride:   backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&contractVersion, "contract-version", 0,
		"contract version the add-on speaks (required; MEHO speaks version 1 today)")
	cmd.Flags().IntVar(&minBackplaneVersion, "min-backplane-version", 0,
		"oldest MEHO contract version the add-on accepts (required)")
	cmd.Flags().StringVar(&ownerSub, "owner-sub", "",
		"OIDC sub of the pairing's owner (defaults to the caller)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the whole PairAddonResult JSON, with client_secret, instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// checkVersionFlags rejects a missing or non-positive version flag before
// any network call. The check lives here and not in cobra's required-flag
// marker so the error is printed in the normal error format.
func checkVersionFlags(cmd *cobra.Command, jsonOut bool) error {
	for _, name := range []string{"contract-version", "min-backplane-version"} {
		flag := cmd.Flags().Lookup(name)
		if flag == nil || !flag.Changed {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Usage(fmt.Sprintf("--%s is required (run '%s --help' for usage)",
					name, cmd.CommandPath())),
				jsonOut)
		}
		value, err := cmd.Flags().GetInt(name)
		if err != nil || value < 1 {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Usage(fmt.Sprintf("--%s must be 1 or higher", name)),
				jsonOut)
		}
	}
	return nil
}

type pairOptions struct {
	Name                string
	ContractVersion     int
	MinBackplaneVersion int
	OwnerSub            string
	JSONOut             bool
	BackplaneOverride   string
}

func runPair(cmd *cobra.Command, opts pairOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("pair requires a non-empty <name> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postPair(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		// A 2xx answer other than 201 could carry credentials. Never echo
		// its body into an error message.
		body := resp.Body
		if resp.StatusCode() >= 200 && resp.StatusCode() < 300 {
			body = nil
		}
		return renderHTTPStatus(cmd, backplaneURL, opts.Name, adminRoleAdvice, resp.StatusCode(), body, opts.JSONOut)
	}
	result := resp.JSON201
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("pair: empty response body"), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printPairResult(cmd, result)
	return nil
}

// printPairResult writes the one-time credentials to stdout. The secret is
// printed exactly once, on its own line, followed by the note.
func printPairResult(cmd *cobra.Command, result *api.PairAddonResult) {
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "paired add-on %q\n", result.Pairing.Name)
	fmt.Fprintf(w, "  client_id:                    %s\n", result.ClientId)
	fmt.Fprintf(w, "  negotiated_contract_version:  %d\n", result.NegotiatedContractVersion)
	fmt.Fprintf(w, "  client_secret:                %s\n", result.ClientSecret)
	fmt.Fprintln(w, secretNote)
}

// buildPairBody maps the flags onto the generated request body. owner_sub
// is sent only when --owner-sub was given; otherwise the server uses the
// caller.
func buildPairBody(opts pairOptions) api.PairAddonRequest {
	body := api.PairAddonRequest{
		Name:                     opts.Name,
		AddonContractVersion:     opts.ContractVersion,
		AddonMinBackplaneVersion: opts.MinBackplaneVersion,
	}
	if opts.OwnerSub != "" {
		owner := opts.OwnerSub
		body.OwnerSub = &owner
	}
	return body
}

func postPair(
	ctx context.Context,
	backplaneURL string,
	opts pairOptions,
) (*api.PairAddonApiV1AddonsPairingsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := buildPairBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.PairAddonApiV1AddonsPairingsPostResponse, error) {
			return authed.PairAddonApiV1AddonsPairingsPostWithResponse(
				ctx,
				&api.PairAddonApiV1AddonsPairingsPostParams{},
				body,
			)
		},
		func(r *api.PairAddonApiV1AddonsPairingsPostResponse) int { return r.StatusCode() },
	)
}
