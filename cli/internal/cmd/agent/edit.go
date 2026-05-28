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

// newEditCmd returns the `meho agent edit` command.
//
//	meho agent edit <name> \
//	  [--identity-ref R] [--model-tier T] [--system-prompt P]
//	  [--turn-budget N] [--toolset @file] [--output-schema @file]
//	  [--enabled|--disabled] [--json] [--backplane <url>]
//
// Role: tenant_admin. Only the flags the operator actually set are sent
// in the PATCH body (mirroring the backend's exclude_unset semantics),
// so a single-field edit leaves the rest untouched. `name` itself is
// not renamable. A 404 (`agent_not_found`) covers absence /
// cross-tenant.
func newEditCmd() *cobra.Command {
	var (
		identityRef       string
		modelTier         string
		systemPrompt      string
		turnBudget        int
		toolset           string
		outputSchema      string
		enabled           bool
		disabled          bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "edit <name>",
		Short: "Apply a partial update to one agent definition (tenant_admin)",
		Long: "edit calls PATCH /api/v1/agents/{name}. Tenant_admin " +
			"only. Only the flags you set are applied — a single-field " +
			"edit leaves the rest of the definition untouched. The agent " +
			"name is not renamable (renaming is delete + recreate). " +
			"--enabled / --disabled toggle the parked state (pass at most " +
			"one). --toolset and --output-schema accept inline JSON, " +
			"@<path>, or @-; each must be a JSON object. A 404 means the " +
			"name doesn't exist in your tenant.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEdit(cmd, editOptions{
				Name:              args[0],
				IdentityRef:       identityRef,
				ModelTier:         modelTier,
				SystemPrompt:      systemPrompt,
				TurnBudget:        turnBudget,
				ToolsetArg:        toolset,
				OutputSchemaArg:   outputSchema,
				Enabled:           enabled,
				Disabled:          disabled,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
				identityRefSet:    cmd.Flags().Changed("identity-ref"),
				modelTierSet:      cmd.Flags().Changed("model-tier"),
				systemPromptSet:   cmd.Flags().Changed("system-prompt"),
				turnBudgetSet:     cmd.Flags().Changed("turn-budget"),
				toolsetSet:        cmd.Flags().Changed("toolset"),
				outputSchemaSet:   cmd.Flags().Changed("output-schema"),
				enabledSet:        cmd.Flags().Changed("enabled"),
				disabledSet:       cmd.Flags().Changed("disabled"),
			})
		},
	}
	cmd.Flags().StringVar(&identityRef, "identity-ref", "", "new identity reference")
	cmd.Flags().StringVar(&modelTier, "model-tier", "", "new model tier: standard | fast | deep")
	cmd.Flags().StringVar(&systemPrompt, "system-prompt", "", "new system prompt")
	cmd.Flags().IntVar(&turnBudget, "turn-budget", 0, "new turn budget (1..1000)")
	cmd.Flags().StringVar(&toolset, "toolset", "",
		"new toolset spec as a JSON object: inline JSON, @<path>, or @-")
	cmd.Flags().StringVar(&outputSchema, "output-schema", "",
		"new output schema as a JSON object: inline JSON, @<path>, or @-")
	cmd.Flags().BoolVar(&enabled, "enabled", false, "enable the definition")
	cmd.Flags().BoolVar(&disabled, "disabled", false, "disable (park) the definition")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AgentDefinitionRead JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type editOptions struct {
	Name              string
	IdentityRef       string
	ModelTier         string
	SystemPrompt      string
	TurnBudget        int
	ToolsetArg        string
	OutputSchemaArg   string
	Enabled           bool
	Disabled          bool
	JSONOut           bool
	BackplaneOverride string

	identityRefSet  bool
	modelTierSet    bool
	systemPromptSet bool
	turnBudgetSet   bool
	toolsetSet      bool
	outputSchemaSet bool
	enabledSet      bool
	disabledSet     bool
}

func runEdit(cmd *cobra.Command, opts editOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit requires a non-empty <name> argument"), opts.JSONOut)
	}
	if opts.enabledSet && opts.disabledSet {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("pass at most one of --enabled / --disabled"), opts.JSONOut)
	}
	body, anySet, err := buildEditBody(cmd, opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if !anySet {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit requires at least one field flag to change"), opts.JSONOut)
	}

	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := patchEdit(cmd.Context(), backplaneURL, opts.Name, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	entry := resp.JSON200
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "updated agent definition %q\n", entry.Name)
	printDefinitionSummary(cmd.OutOrStdout(), entry)
	return nil
}

// buildEditBody assembles the PATCH request body from only the flags
// the operator set. Returns the typed AgentDefinitionUpdate and a
// boolean indicating whether any field was actually populated (so the
// caller can reject a no-op edit before issuing the request). Kept
// separate from runEdit so the field-selection logic stays
// unit-testable without a live HTTP transport.
//
// The backend's PATCH semantics use Pydantic's exclude_unset, so an
// `omitempty` pointer field left nil is "field omitted" and the
// existing value is preserved. Toolset / OutputSchema in the
// generated AgentDefinitionUpdate are NOT tagged `omitempty` (they
// carry the wire `null` distinction the backend's v0.2 limitation
// documents), so this helper leaves them nil when the operator
// didn't pass the flag; they only become non-nil when --toolset /
// --output-schema is actually set.
func buildEditBody(cmd *cobra.Command, opts editOptions) (api.AgentDefinitionUpdate, bool, error) {
	body := api.AgentDefinitionUpdate{}
	anySet := false
	if opts.identityRefSet {
		identityRef := opts.IdentityRef
		body.IdentityRef = &identityRef
		anySet = true
	}
	if opts.modelTierSet {
		if !validModelTiers[opts.ModelTier] {
			return api.AgentDefinitionUpdate{}, false, fmt.Errorf("--model-tier must be one of: standard, fast, deep")
		}
		tier := api.AgentModelTier(opts.ModelTier)
		body.ModelTier = &tier
		anySet = true
	}
	if opts.systemPromptSet {
		systemPrompt := opts.SystemPrompt
		body.SystemPrompt = &systemPrompt
		anySet = true
	}
	if opts.turnBudgetSet {
		if opts.TurnBudget < 1 || opts.TurnBudget > 1000 {
			return api.AgentDefinitionUpdate{}, false, fmt.Errorf("--turn-budget must be between 1 and 1000; got %d", opts.TurnBudget)
		}
		turnBudget := opts.TurnBudget
		body.TurnBudget = &turnBudget
		anySet = true
	}
	if opts.toolsetSet {
		toolset, err := loadJSONObjectFlag(cmd, opts.ToolsetArg, "--toolset")
		if err != nil {
			return api.AgentDefinitionUpdate{}, false, err
		}
		body.Toolset = &toolset
		anySet = true
	}
	if opts.outputSchemaSet {
		schema, err := loadJSONObjectFlag(cmd, opts.OutputSchemaArg, "--output-schema")
		if err != nil {
			return api.AgentDefinitionUpdate{}, false, err
		}
		body.OutputSchema = &schema
		anySet = true
	}
	if opts.enabledSet {
		enabled := true
		body.Enabled = &enabled
		anySet = true
	}
	if opts.disabledSet {
		disabled := false
		body.Enabled = &disabled
		anySet = true
	}
	return body, anySet, nil
}

func patchEdit(
	ctx context.Context,
	backplaneURL, name string,
	body api.AgentDefinitionUpdate,
) (*api.EditAgentApiV1AgentsNamePatchResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EditAgentApiV1AgentsNamePatchResponse, error) {
			return authed.EditAgentApiV1AgentsNamePatchWithResponse(ctx, name, nil, body)
		},
		func(r *api.EditAgentApiV1AgentsNamePatchResponse) int { return r.StatusCode() },
	)
}
