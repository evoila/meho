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
	"github.com/evoila/meho/cli/internal/startinggrants"
)

// newCreateCmd returns the `meho agent create` command.
//
//	meho agent create <name> \
//	  --identity-ref R --model-tier T --system-prompt P --turn-budget N \
//	  [--toolset @file|@-|<json>] [--output-schema @file|@-|<json>] \
//	  [--grants none|read-everything|read-targets [--target <name> ...]] \
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
		grants            startinggrants.Flags
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
			"agent_already_exists.\n\n" +
			"--grants picks the first grants of the agent that --identity-ref " +
			"names: none, read-everything, or read-targets with one --target " +
			"<name> per target. Only safe operations run at once; everything " +
			"else waits for a person's approval. MEHO needs the pick when that " +
			"agent has no grant yet. The CLI never picks for you: when MEHO " +
			"needs a pick and --grants is missing, it prints the three choices " +
			"and creates nothing. With --grants, the CLI shows the agent's " +
			"grants after creation (--json adds them as \"grants\"). " +
			"Without --grants, it shows the agent's live grants when it has " +
			"some: then no pick is needed (--json has them as \"live_grants\").",
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
				Grants:            grants,
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
		"the agent's standing rules, the same for every run; the task for one run goes in the --input flag of meho agent run (guide: https://evoila.github.io/meho/latest/guides/hosted-agents/)")
	cmd.Flags().IntVar(&turnBudget, "turn-budget", 0,
		"max model turns the runtime allows (1..1000)")
	cmd.Flags().StringVar(&toolset, "toolset", "",
		"allowed-tools spec as a JSON object: inline JSON, @<path>, or @- (default {}); fields and tool names: https://evoila.github.io/meho/latest/guides/hosted-agents/")
	cmd.Flags().StringVar(&outputSchema, "output-schema", "",
		"optional structured-output JSON Schema as a JSON object: inline JSON, @<path>, or @-")
	cmd.Flags().BoolVar(&disabled, "disabled", false,
		"create the definition parked (enabled=false; default is enabled)")
	startinggrants.AddFlags(cmd, &grants)
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
	Grants            startinggrants.Flags
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
	pick, err := opts.Grants.Body()
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Usage(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	body := api.AgentDefinitionCreate{
		Name:           opts.Name,
		IdentityRef:    opts.IdentityRef,
		ModelTier:      api.AgentModelTier(opts.ModelTier),
		SystemPrompt:   opts.SystemPrompt,
		TurnBudget:     opts.TurnBudget,
		StartingGrants: pick,
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
		if pickErr := startinggrants.RequestError(resp.StatusCode(), resp.Body); pickErr != nil {
			return output.RenderError(cmd.ErrOrStderr(), pickErr, opts.JSONOut)
		}
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	return printSaved(cmd, backplaneURL, "created", resp.JSON201, pick != nil, opts.JSONOut)
}

// printSaved prints the created or edited definition. After a starting
// grant pick it also reads and shows the grants of the definition's
// agent, so the person sees what the pick wrote (--json adds them as
// "grants"). Without a pick it shows the agent's live grants from the
// answer: then MEHO asked for no pick, and these grants say what the
// agent may do (--json has them as "live_grants").
func printSaved(
	cmd *cobra.Command, backplaneURL, verb string, entry *api.AgentDefinitionSaved, picked, jsonOut bool,
) error {
	if !picked {
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), entry)
		}
		fmt.Fprintf(cmd.OutOrStdout(), "%s agent definition %q\n", verb, entry.Name)
		printDefinitionSummary(cmd.OutOrStdout(), savedDefinition(entry))
		startinggrants.PrintLiveGrants(cmd.OutOrStdout(), entry.IdentityRef, entry.LiveGrants, entry.LiveGrantsTruncated)
		return nil
	}
	grants, readErr := readAgentGrants(cmd.Context(), backplaneURL, entry.IdentityRef)
	if jsonOut {
		out, err := startinggrants.WithGrants(entry, grants, readErr)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
		}
		return output.PrintJSON(cmd.OutOrStdout(), out)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "%s agent definition %q\n", verb, entry.Name)
	printDefinitionSummary(cmd.OutOrStdout(), savedDefinition(entry))
	outcome := "created"
	if verb != "created" {
		outcome = "changed"
	}
	startinggrants.PrintGrants(cmd.OutOrStdout(), outcome, entry.IdentityRef, grants, readErr)
	return nil
}

// savedDefinition returns the definition part of a create or edit answer.
func savedDefinition(s *api.AgentDefinitionSaved) *api.AgentDefinitionRead {
	if s == nil {
		return nil
	}
	return &api.AgentDefinitionRead{
		CreatedAt:    s.CreatedAt,
		CreatedBySub: s.CreatedBySub,
		Enabled:      s.Enabled,
		Id:           s.Id,
		IdentityRef:  s.IdentityRef,
		ModelTier:    s.ModelTier,
		Name:         s.Name,
		OutputSchema: s.OutputSchema,
		SystemPrompt: s.SystemPrompt,
		TenantId:     s.TenantId,
		Toolset:      s.Toolset,
		TurnBudget:   s.TurnBudget,
		UpdatedAt:    s.UpdatedAt,
	}
}

// readAgentGrants reads the grants of the definition's agent after the
// create or edit, so the person sees what the pick wrote.
func readAgentGrants(ctx context.Context, backplaneURL, principalSub string) ([]api.AgentGrantRead, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return startinggrants.ReadGrants(ctx, authed, principalSub)
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
