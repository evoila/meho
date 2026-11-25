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

// newRegisterCmd returns the `meho runner-principal register <name>` command.
func newRegisterCmd() *cobra.Command {
	var (
		ownerSub          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "register <name>",
		Short: "Register a new runner principal (a person with tenant_admin)",
		Long: "register calls POST /api/v1/runner-principals to create a new " +
			"satellite-runner principal in the operator's tenant. " +
			"Creates a Keycloak client (kind=runner, serviceAccounts=true) and a DB row. " +
			"The Keycloak clientId will be 'runner:<name>'. The client's token " +
			"is minted with a hardcoded principal_kind=runner mapper and a " +
			"read-only tenant_role, so the runner is caged to the gateway path " +
			"prefixes (/api/v1/gateway/, /api/v1/checks/) and rejected 403 " +
			"everywhere else. " +
			"It also prints a one-time enrollment code (shown once, short expiry): " +
			"the runner sends it with its own public key on its first start, so it " +
			"can renew its own login. " +
			"--owner-sub sets the kill-switch owner; defaults to the caller's sub. " +
			"Returns 409 when the name is taken, also by a revoked runner. " +
			"Only a person with tenant_admin can register: a service login or an " +
			"agent login gets 403 human_principal_required.",
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
		"emit raw JSON instead of the human summary; it holds the runner and its one-time enrollment code")
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
	if entry == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("register: empty response body"), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "registered runner principal %q\n", entry.Name)
	printRegisteredSummary(cmd.OutOrStdout(), entry)
	return nil
}

// printRegisteredSummary prints the principal and its one-time enrollment code.
// The code is shown only here; the runner sends it with its own public key to
// enroll, and it works once.
func printRegisteredSummary(w io.Writer, e *api.RunnerPrincipalRegistered) {
	printEntrySummary(w, &api.RunnerPrincipalRead{
		CreatedAt:        e.CreatedAt,
		CreatedBySub:     e.CreatedBySub,
		Id:               e.Id,
		KeycloakClientId: e.KeycloakClientId,
		Name:             e.Name,
		OwnerSub:         e.OwnerSub,
		Revoked:          e.Revoked,
	})
	fmt.Fprintf(w, "  enrollment_code:     %s\n", e.EnrollmentCode)
	fmt.Fprintf(w, "  code_expires_at:     %s\n", e.EnrollmentCodeExpiresAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintln(w, "The code is shown once and works once. Set it as MEHO_RUNNER_ENROLLMENT_CODE on the runner's first start.")
}

// buildRegisterBody maps the verb's options onto the generated
// RunnerPrincipalCreate body. owner_sub is sent only when the operator
// passed --owner-sub so an unset flag leaves the field absent on the wire
// (the backend defaults it to the caller's sub).
func buildRegisterBody(opts registerOptions) api.RunnerPrincipalCreate {
	body := api.RunnerPrincipalCreate{Name: opts.Name}
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
) (*api.RegisterRunnerPrincipalApiV1RunnerPrincipalsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := buildRegisterBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RegisterRunnerPrincipalApiV1RunnerPrincipalsPostResponse, error) {
			return authed.RegisterRunnerPrincipalApiV1RunnerPrincipalsPostWithResponse(
				ctx,
				&api.RegisterRunnerPrincipalApiV1RunnerPrincipalsPostParams{},
				body,
			)
		},
		func(r *api.RegisterRunnerPrincipalApiV1RunnerPrincipalsPostResponse) int { return r.StatusCode() },
	)
}
