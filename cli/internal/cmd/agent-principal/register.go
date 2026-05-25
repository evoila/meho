// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agentprincipal

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// registerRequest mirrors the backend AgentPrincipalCreate pydantic model.
type registerRequest struct {
	Name     string `json:"name"`
	OwnerSub string `json:"owner_sub,omitempty"`
}

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
		"emit raw Entry JSON instead of the human summary")
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
	entry, err := postRegister(cmd.Context(), backplaneURL, registerRequest{
		Name:     opts.Name,
		OwnerSub: opts.OwnerSub,
	})
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "registered agent principal %q\n", entry.Name)
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

func postRegister(ctx context.Context, backplaneURL string, req registerRequest) (*Entry, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal register request: %w", err)
	}
	raw, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/agent-principals", body)
	if err != nil {
		return nil, err
	}
	var out Entry
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode register response: %w", err)
	}
	return &out, nil
}
