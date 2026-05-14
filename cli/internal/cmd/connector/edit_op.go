// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// validSafetyLevels pins the safety_level enum the backend accepts.
// Matches the safety_level column on endpoint_descriptor (G0.6-T1
// #392 schema). Fail fast in the CLI rather than letting the
// backplane 422 surface an unfamiliar value.
var validSafetyLevels = map[string]struct{}{
	"safe":      {},
	"caution":   {},
	"dangerous": {},
}

// EditOpBody mirrors the backend EditOpBody Pydantic model. Every
// field is optional + nullable so partial patches work. Pointer
// types let json.Marshal omit unset fields cleanly via `omitempty`.
type EditOpBody struct {
	CustomDescription *string `json:"custom_description,omitempty"`
	SafetyLevel       *string `json:"safety_level,omitempty"`
	RequiresApproval  *bool   `json:"requires_approval,omitempty"`
	IsEnabled         *bool   `json:"is_enabled,omitempty"`
}

// EditOpResponse mirrors the backend response — the updated op row
// echoed back so the CLI can confirm without re-running review.
type EditOpResponse struct {
	ConnectorID       string  `json:"connector_id"`
	OpID              string  `json:"op_id"`
	CustomDescription *string `json:"custom_description"`
	SafetyLevel       string  `json:"safety_level"`
	RequiresApproval  bool    `json:"requires_approval"`
	IsEnabled         bool    `json:"is_enabled"`
}

// newEditOpCmd returns the `meho connector edit-op` command.
//
// CLI shape:
//
//	meho connector edit-op <connector_id> <op_id> \
//	  [--custom-description <text>|@<file>] \
//	  [--safety safe|caution|dangerous] \
//	  [--requires-approval | --no-requires-approval] \
//	  [--enable | --disable] \
//	  [--json] [--backplane <url>]
//
// Hits PATCH /api/v1/connectors/<connector_id>/operations/<op_id>
// with an EditOpBody. tenant_admin role required.
func newEditOpCmd() *cobra.Command {
	var (
		customDesc        string
		safetyFlag        string
		requiresApproval  bool
		clearApproval     bool
		enableOp          bool
		disableOp         bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "edit-op <connector_id> <op_id>",
		Short: "Patch a per-op override (custom_description, safety, approval, enabled)",
		Long: "edit-op calls PATCH /api/v1/connectors/<id>/operations/<op_id>\n" +
			"to override the per-op flags an operator wants to differ from\n" +
			"the parser-derived defaults. Common patterns:\n\n" +
			"  --safety dangerous            # parser tagged a destructive\n" +
			"                                # POST as 'caution'; bump it.\n" +
			"  --requires-approval           # gate this op on the approval\n" +
			"                                # workflow regardless of safety.\n" +
			"  --no-requires-approval        # clear the requires_approval flag.\n" +
			"  --custom-description @path.md # operator-authored agent prompt\n" +
			"                                # supersedes vendor docs.\n" +
			"  --disable                     # exclude this op from\n" +
			"                                # search_operations + dispatch\n" +
			"                                # even when the connector is enabled.\n" +
			"  --enable                      # re-include a previously disabled op.\n\n" +
			"op_id is the canonical form `METHOD:/path` for generic\n" +
			"connectors (e.g. `GET:/api/vcenter/cluster`). The CLI URL-escapes\n" +
			"the op_id segment before placing it in the path so the colons\n" +
			"and slashes survive the routing layer.\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEditOp(cmd, editOpOptions{
				ConnectorID:          args[0],
				OpID:                 args[1],
				CustomDescriptionRaw: customDesc,
				SafetyFlag:           safetyFlag,
				RequiresApproval:     requiresApproval,
				ClearApproval:        clearApproval,
				EnableOp:             enableOp,
				DisableOp:            disableOp,
				JSONOut:              jsonOut,
				BackplaneOverride:    backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&customDesc, "custom-description", "",
		"replacement custom_description; supports `@<path>` to read from a file")
	cmd.Flags().StringVar(&safetyFlag, "safety", "",
		"replacement safety_level: safe | caution | dangerous")
	cmd.Flags().BoolVar(&requiresApproval, "requires-approval", false,
		"mark the op as requiring an approval workflow")
	cmd.Flags().BoolVar(&clearApproval, "no-requires-approval", false,
		"clear the requires_approval flag")
	cmd.Flags().BoolVar(&enableOp, "enable", false,
		"set is_enabled=true on this op")
	cmd.Flags().BoolVar(&disableOp, "disable", false,
		"set is_enabled=false on this op")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.MarkFlagsMutuallyExclusive("requires-approval", "no-requires-approval")
	cmd.MarkFlagsMutuallyExclusive("enable", "disable")
	return cmd
}

type editOpOptions struct {
	ConnectorID          string
	OpID                 string
	CustomDescriptionRaw string
	SafetyFlag           string
	RequiresApproval     bool
	ClearApproval        bool
	EnableOp             bool
	DisableOp            bool
	JSONOut              bool
	BackplaneOverride    string
}

func runEditOp(cmd *cobra.Command, opts editOpOptions) error {
	body := EditOpBody{}

	descText, descSet, err := loadTextFlag(cmd, "custom-description")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if descSet {
		body.CustomDescription = &descText
	}

	if opts.SafetyFlag != "" {
		if _, ok := validSafetyLevels[opts.SafetyFlag]; !ok {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"--safety %q invalid; expected one of: safe | caution | dangerous",
					opts.SafetyFlag,
				)),
				opts.JSONOut,
			)
		}
		body.SafetyLevel = &opts.SafetyFlag
	}

	if opts.RequiresApproval {
		t := true
		body.RequiresApproval = &t
	} else if opts.ClearApproval {
		f := false
		body.RequiresApproval = &f
	}

	if opts.EnableOp {
		t := true
		body.IsEnabled = &t
	} else if opts.DisableOp {
		f := false
		body.IsEnabled = &f
	}

	if isEmptyEditOpBody(body) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit-op needs at least one of --custom-description / --safety / --requires-approval / --no-requires-approval / --enable / --disable"),
			opts.JSONOut,
		)
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := patchOp(cmd.Context(), backplaneURL, opts.ConnectorID, opts.OpID, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printEditOpResult(cmd.OutOrStdout(), result)
	return nil
}

// isEmptyEditOpBody returns true when no field is set — the operator
// invoked edit-op without any actual change. Without this guard the
// CLI would ship an empty PATCH that succeeds at the route layer
// but does nothing, surprising the operator.
func isEmptyEditOpBody(b EditOpBody) bool {
	return b.CustomDescription == nil &&
		b.SafetyLevel == nil &&
		b.RequiresApproval == nil &&
		b.IsEnabled == nil
}

func patchOp(
	ctx context.Context,
	backplaneURL, connectorID, opID string,
	body EditOpBody,
) (*EditOpResponse, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal edit-op request: %w", err)
	}
	path := fmt.Sprintf("/api/v1/connectors/%s/operations/%s",
		pathEscapeOpID(connectorID), pathEscapeOpID(opID),
	)
	respBody, err := doAuthedRequest(ctx, backplaneURL, "PATCH", path, raw)
	if err != nil {
		return nil, err
	}
	var out EditOpResponse
	if err := decodeJSON(respBody, "edit-op", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func printEditOpResult(w io.Writer, r *EditOpResponse) {
	fmt.Fprintf(w, "%s/%s — updated\n", r.ConnectorID, r.OpID)
	fmt.Fprintf(w, "  safety: %s\n", r.SafetyLevel)
	fmt.Fprintf(w, "  requires_approval: %t\n", r.RequiresApproval)
	fmt.Fprintf(w, "  is_enabled: %t\n", r.IsEnabled)
	if r.CustomDescription != nil && *r.CustomDescription != "" {
		fmt.Fprintf(w, "  custom_description: %s\n", *r.CustomDescription)
	}
}
