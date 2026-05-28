// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCreateCmd returns the `meho agent create` command.
//
//	meho agent create <name> \
//	  --identity-ref R --model-tier T --system-prompt P --turn-budget N \
//	  [--toolset @file|@-|<json>] [--output-schema @file|@-|<json>] \
//	  [--disabled] [--json] [--backplane <url>]
//
// Role: tenant_admin. A duplicate (tenant, name) returns 409.
func newCreateCmd() *cobra.Command {
	var (
		identityRef       string
		modelTier         string
		systemPrompt      string
		turnBudget        int
		toolset           string
		outputSchema      string
		disabled          bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "create <name>",
		Short: "Create one agent definition (tenant_admin)",
		Long: "create calls POST /api/v1/agents to create one agent " +
			"definition under the operator's tenant. Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"--model-tier is one of standard|fast|deep. --turn-budget is " +
			"the max model turns (1..1000). --toolset and --output-schema " +
			"accept inline JSON, @<path> to read a file, or @- for stdin; " +
			"each must be a JSON object. --disabled creates the definition " +
			"parked (enabled defaults to true).\n\n" +
			"A duplicate (same tenant + name) returns 409 with detail " +
			"agent_already_exists.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCreate(cmd, createOptions{
				Name:              args[0],
				IdentityRef:       identityRef,
				ModelTier:         modelTier,
				SystemPrompt:      systemPrompt,
				TurnBudget:        turnBudget,
				ToolsetArg:        toolset,
				OutputSchemaArg:   outputSchema,
				Disabled:          disabled,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&identityRef, "identity-ref", "",
		"reference to the agent principal whose permissions bound the toolset (G11.2)")
	cmd.Flags().StringVar(&modelTier, "model-tier", "",
		"logical model tier: standard | fast | deep")
	cmd.Flags().StringVar(&systemPrompt, "system-prompt", "",
		"the agent's system prompt")
	cmd.Flags().IntVar(&turnBudget, "turn-budget", 0,
		"max model turns the runtime allows (1..1000)")
	cmd.Flags().StringVar(&toolset, "toolset", "",
		"allowed-tools spec as a JSON object: inline JSON, @<path>, or @- (default {})")
	cmd.Flags().StringVar(&outputSchema, "output-schema", "",
		"optional structured-output JSON Schema as a JSON object: inline JSON, @<path>, or @-")
	cmd.Flags().BoolVar(&disabled, "disabled", false,
		"create the definition parked (enabled=false; default is enabled)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AgentDefinitionRead JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("identity-ref")
	_ = cmd.MarkFlagRequired("model-tier")
	_ = cmd.MarkFlagRequired("system-prompt")
	_ = cmd.MarkFlagRequired("turn-budget")
	return cmd
}

type createOptions struct {
	Name              string
	IdentityRef       string
	ModelTier         string
	SystemPrompt      string
	TurnBudget        int
	ToolsetArg        string
	OutputSchemaArg   string
	Disabled          bool
	JSONOut           bool
	BackplaneOverride string
}

func runCreate(cmd *cobra.Command, opts createOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("create requires a non-empty <name> argument"), opts.JSONOut)
	}
	// CLI-side model-tier validation mirrors the backend's enum so the
	// operator gets an immediate rejection rather than a remote 422.
	if !validModelTiers[opts.ModelTier] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--model-tier must be one of: standard, fast, deep"), opts.JSONOut)
	}
	if opts.TurnBudget < 1 || opts.TurnBudget > 1000 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--turn-budget must be between 1 and 1000; got %d", opts.TurnBudget)),
			opts.JSONOut)
	}
	toolset, err := loadJSONObjectFlag(cmd, opts.ToolsetArg, "--toolset")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	outputSchema, err := loadJSONObjectFlag(cmd, opts.OutputSchemaArg, "--output-schema")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	body := api.AgentDefinitionCreate{
		Name:         opts.Name,
		IdentityRef:  opts.IdentityRef,
		ModelTier:    api.AgentModelTier(opts.ModelTier),
		SystemPrompt: opts.SystemPrompt,
		TurnBudget:   opts.TurnBudget,
	}
	// Toolset is `*map[string]interface{}` with `omitempty` — leaving it
	// nil tells the backend to apply its documented default ({}).
	if toolset != nil {
		body.Toolset = &toolset
	}
	// OutputSchema is `*map[string]interface{}` without `omitempty`, so a
	// nil pointer round-trips to wire `null`, matching the backend's
	// "no structured-output schema" semantics.
	if outputSchema != nil {
		body.OutputSchema = &outputSchema
	}
	if opts.Disabled {
		disabled := false
		body.Enabled = &disabled
	}
	resp, err := postCreate(cmd.Context(), backplaneURL, body)
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
	fmt.Fprintf(cmd.OutOrStdout(), "created agent definition %q\n", entry.Name)
	printDefinitionSummary(cmd.OutOrStdout(), entry)
	return nil
}

func postCreate(ctx context.Context, backplaneURL string, body api.AgentDefinitionCreate) (*api.CreateAgentApiV1AgentsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.CreateAgentApiV1AgentsPostResponse, error) {
			return authed.CreateAgentApiV1AgentsPostWithResponse(ctx, nil, body)
		},
		func(r *api.CreateAgentApiV1AgentsPostResponse) int { return r.StatusCode() },
	)
}
