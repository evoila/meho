// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agentprincipal

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRegisterCmd returns the `meho agent-principal register <name>` command.
func newRegisterCmd() *cobra.Command {
	var (
		ownerSub          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "register <name>",
		Short: "Register a new agent principal (tenant_admin)",
		Long: "register calls POST /api/v1/agent-principals to create a new " +
			"agent principal in the operator's tenant. " +
			"Creates a Keycloak client (kind=agent, serviceAccounts=true) and a DB row. " +
			"The Keycloak clientId will be 'agent:<name>'. " +
			"--owner-sub sets the kill-switch owner; defaults to the caller's sub. " +
			"Returns 409 when a principal with the same name already exists. " +
			"Requires tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRegister(cmd, registerOptions{
				Name:              args[0],
				OwnerSub:          ownerSub,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&ownerSub, "owner-sub", "",
		"OIDC sub of the kill-switch owner (defaults to the caller's sub)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AgentPrincipalRead JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type registerOptions struct {
	Name              string
	OwnerSub          string
	JSONOut           bool
	BackplaneOverride string
}

func runRegister(cmd *cobra.Command, opts registerOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("register requires a non-empty <name> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postRegister(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	entry := resp.JSON201
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "registered agent principal %q\n", entry.Name)
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

// buildRegisterBody maps the verb's options onto the generated
// AgentPrincipalCreate body. owner_sub is a *string in the
// generated type (the backend treats null/omitted as "default to
// the caller's sub"); we send the pointer only when the operator
// passed --owner-sub so an unset flag leaves the field absent on
// the wire instead of stamping an explicit empty string.
func buildRegisterBody(opts registerOptions) api.AgentPrincipalCreate {
	body := api.AgentPrincipalCreate{Name: opts.Name}
	if opts.OwnerSub != "" {
		owner := opts.OwnerSub
		body.OwnerSub = &owner
	}
	return body
}

func postRegister(
	ctx context.Context,
	backplaneURL string,
	opts registerOptions,
) (*api.RegisterAgentPrincipalApiV1AgentPrincipalsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := buildRegisterBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RegisterAgentPrincipalApiV1AgentPrincipalsPostResponse, error) {
			return authed.RegisterAgentPrincipalApiV1AgentPrincipalsPostWithResponse(
				ctx,
				&api.RegisterAgentPrincipalApiV1AgentPrincipalsPostParams{},
				body,
			)
		},
		func(r *api.RegisterAgentPrincipalApiV1AgentPrincipalsPostResponse) int { return r.StatusCode() },
	)
}
