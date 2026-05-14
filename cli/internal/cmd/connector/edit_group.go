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

// EditGroupBody mirrors the backend EditGroupBody Pydantic model.
// Both fields are nullable on the wire so the operator can patch
// one field without clobbering the other; we use `omitempty` +
// pointer types so json.Marshal omits unset fields entirely (rather
// than sending explicit null, which T6 might or might not
// distinguish from absent).
type EditGroupBody struct {
	WhenToUse *string `json:"when_to_use,omitempty"`
	Name      *string `json:"name,omitempty"`
}

// EditGroupResponse mirrors the backend response — the updated
// group payload echoed back so the CLI can confirm the change
// landed without a second GET.
type EditGroupResponse struct {
	ConnectorID string `json:"connector_id"`
	GroupKey    string `json:"group_key"`
	Name        string `json:"name"`
	WhenToUse   string `json:"when_to_use"`
}

// newEditGroupCmd returns the `meho connector edit-group` command.
//
// CLI shape:
//
//	meho connector edit-group <connector_id> <group_key> \
//	  [--when-to-use <text>|@<file>] \
//	  [--name <text>] \
//	  [--json] [--backplane <url>]
//
// Hits PATCH /api/v1/connectors/<connector_id>/groups/<group_key>
// with an EditGroupBody. tenant_admin role required.
func newEditGroupCmd() *cobra.Command {
	var (
		whenToUseFlag     string
		nameFlag          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "edit-group <connector_id> <group_key>",
		Short: "Patch a group's when_to_use hint or display name",
		Long: "edit-group calls PATCH /api/v1/connectors/<connector_id>/groups/\n" +
			"<group_key>. The LLM-derived `when_to_use` strings are the\n" +
			"first thing an operator usually overrides at review — the\n" +
			"agent-facing prompt is decisive for search ranking, and the\n" +
			"LLM's first cut sometimes reads more like spec docs than\n" +
			"actionable hints.\n\n" +
			"--when-to-use accepts inline text or `@<path>` to read from a\n" +
			"file (useful for multi-paragraph hints maintained in version\n" +
			"control); the same `@<path>` form works for --name though\n" +
			"single-line display names rarely need it.\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEditGroup(cmd, editGroupOptions{
				ConnectorID:       args[0],
				GroupKey:          args[1],
				WhenToUseRaw:      whenToUseFlag,
				NameRaw:           nameFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&whenToUseFlag, "when-to-use", "",
		"replacement when_to_use text; supports `@<path>` to read from a file")
	cmd.Flags().StringVar(&nameFlag, "name", "",
		"replacement display name; supports `@<path>` to read from a file")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type editGroupOptions struct {
	ConnectorID       string
	GroupKey          string
	WhenToUseRaw      string
	NameRaw           string
	JSONOut           bool
	BackplaneOverride string
}

func runEditGroup(cmd *cobra.Command, opts editGroupOptions) error {
	whenToUse, whenSet, err := loadTextFlag(cmd, "when-to-use")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	name, nameSet, err := loadTextFlag(cmd, "name")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if !whenSet && !nameSet {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit-group needs at least one of --when-to-use or --name"),
			opts.JSONOut,
		)
	}
	body := EditGroupBody{}
	if whenSet {
		body.WhenToUse = &whenToUse
	}
	if nameSet {
		body.Name = &name
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := patchGroup(cmd.Context(), backplaneURL, opts.ConnectorID, opts.GroupKey, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printEditGroupResult(cmd.OutOrStdout(), result)
	return nil
}

func patchGroup(
	ctx context.Context,
	backplaneURL, connectorID, groupKey string,
	body EditGroupBody,
) (*EditGroupResponse, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal edit-group request: %w", err)
	}
	path := fmt.Sprintf("/api/v1/connectors/%s/groups/%s",
		pathEscapeOpID(connectorID), pathEscapeOpID(groupKey),
	)
	respBody, err := doAuthedRequest(ctx, backplaneURL, "PATCH", path, raw)
	if err != nil {
		return nil, err
	}
	var out EditGroupResponse
	if err := decodeJSON(respBody, "edit-group", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func printEditGroupResult(w io.Writer, r *EditGroupResponse) {
	fmt.Fprintf(w, "%s/%s — updated\n", r.ConnectorID, r.GroupKey)
	fmt.Fprintf(w, "  name: %s\n", r.Name)
	if r.WhenToUse != "" {
		fmt.Fprintf(w, "  when_to_use: %s\n", r.WhenToUse)
	}
}
