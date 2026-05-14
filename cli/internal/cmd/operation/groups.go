// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// GroupSummary mirrors the backend OperationGroupSummary Pydantic
// model. Kept hand-written (rather than oapi-codegen-generated)
// because the openapi spec types these routes' responses as
// dict[str, Any] — the typed shape is locked by the backend test
// suite, and a minor drift on either side surfaces on the matching
// CI gate.
type GroupSummary struct {
	GroupKey       string `json:"group_key"`
	Name           string `json:"name"`
	WhenToUse      string `json:"when_to_use"`
	OperationCount int    `json:"operation_count"`
}

// GroupsResponse is the JSON envelope returned by
// GET /api/v1/operations/groups.
type GroupsResponse struct {
	ConnectorID string         `json:"connector_id"`
	Groups      []GroupSummary `json:"groups"`
}

// newGroupsCmd returns the `meho operation groups <connector_id>` command.
//
// CLI shape (matches issue #481 spec):
//
//	meho operation groups <connector_id> \
//	  [--json]                                # machine-readable output
//	  [--backplane <url>]                     # override the backplane URL
//
// Exit codes mirror `meho retrieval eval`:
//   - 0   groups listed cleanly (including the "0 enabled groups" empty case)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
func newGroupsCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "groups <connector_id>",
		Short: "List enabled operation groups for a connector",
		Long: "groups calls GET /api/v1/operations/groups against the named " +
			"connector_id (e.g. `vault-1.x`, `vmware-rest-9.0`) and renders " +
			"the enabled groups as a human-readable table with --json for the " +
			"raw envelope. Unknown connector_id returns an empty groups list " +
			"(operationally meaningful — the connector exists but has no " +
			"enabled groups yet, or the connector_id is unknown).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runGroups(cmd, args[0], jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runGroups(cmd *cobra.Command, connectorID string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	result, err := getGroups(cmd.Context(), backplaneURL, connectorID)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printGroupsTable(cmd.OutOrStdout(), result)
	return nil
}

func getGroups(ctx context.Context, backplaneURL, connectorID string) (*GroupsResponse, error) {
	q := url.Values{}
	q.Set("connector_id", connectorID)
	path := "/api/v1/operations/groups?" + q.Encode()
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	var out GroupsResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode groups response: %w", err)
	}
	return &out, nil
}

// printGroupsTable renders a GroupsResponse as a human-readable
// table. Compact one-line-per-group format keeps `meho operation
// groups vault-1.x` scannable when the connector has 20+ groups.
// when_to_use is truncated to 80 chars; full text comes back via
// --json.
func printGroupsTable(w io.Writer, r *GroupsResponse) {
	if len(r.Groups) == 0 {
		fmt.Fprintf(w, "%s — 0 enabled groups\n", r.ConnectorID)
		return
	}
	fmt.Fprintf(w, "%s — %d enabled group(s)\n", r.ConnectorID, len(r.Groups))
	fmt.Fprintf(w, "%-24s %4s  %-30s %s\n", "group_key", "ops", "name", "when_to_use")
	for _, g := range r.Groups {
		fmt.Fprintf(w, "%-24s %4d  %-30s %s\n",
			truncate(g.GroupKey, 24),
			g.OperationCount,
			truncate(g.Name, 30),
			truncate(g.WhenToUse, 80),
		)
	}
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in group names / when_to_use strings survives without
// producing an invalid UTF-8 cut.
func truncate(s string, maxLen int) string {
	if maxLen < 1 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	return string(runes[:maxLen-1]) + "…"
}
