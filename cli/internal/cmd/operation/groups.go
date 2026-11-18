// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// GroupSummary mirrors the backend OperationGroupSummary Pydantic
// model. Kept hand-written (rather than oapi-codegen-generated)
// because the FastAPI surface types this route's response as
// `dict[str, Any]` — the generator therefore emits no
// `OperationGroupSummary` model worth using, and the typed shape is
// locked by the backend test suite. Promoting the response to a
// typed FastAPI return so the generator picks it up is a separate
// backend Task explicitly out of scope for G0.12-T2 #1260 (the CLI
// hygiene Initiative #1118 is consumer-side only).
type GroupSummary struct {
	GroupKey       string `json:"group_key"`
	Name           string `json:"name"`
	WhenToUse      string `json:"when_to_use"`
	OperationCount int    `json:"operation_count"`
	// Operations is a pointer so --json keeps the backplane's answer: no
	// field while the switch is off, and `[]` for a group without typed
	// operations while it is on.
	Operations     *[]GroupOperation `json:"operations,omitempty"`
	OperationsMore int               `json:"operations_more,omitempty"`
}

// GroupOperation is one typed or multi-step operation of a group. The
// backplane sends the list only while its GROUP_LISTING_OPERATIONS switch
// is on.
type GroupOperation struct {
	OpID             string  `json:"op_id"`
	Summary          *string `json:"summary"`
	SafetyLevel      string  `json:"safety_level"`
	RequiresApproval bool    `json:"requires_approval"`
}

// GroupsResponse is the JSON envelope returned by
// GET /api/v1/operations/groups. Hand-typed for the same reason as
// GroupSummary above.
type GroupsResponse struct {
	ConnectorID   string         `json:"connector_id"`
	Groups        []GroupSummary `json:"groups"`
	NextCursor    *string        `json:"next_cursor"`
	RawOperations *RawOperations `json:"raw_operations,omitempty"`
	Note          string         `json:"note,omitempty"`
}

// groupsQuery holds the optional query parameters of the groups call.
type groupsQuery struct {
	includeRaw bool
	group      string
	cursor     string
}

// RawOperations is the `raw_operations` part of a search or group
// answer. The backplane sends it only while its SEARCH_TYPED_FIRST
// switch is on: then raw operations (made from the vendor API
// description) are hidden unless --include-raw is set.
type RawOperations struct {
	Included      bool    `json:"included"`
	HiddenMatches int     `json:"hidden_matches,omitempty"`
	HiddenGroups  int     `json:"hidden_groups,omitempty"`
	Note          *string `json:"note"`
}

// printRawNote prints the note of raw_operations, when there is one.
func printRawNote(w io.Writer, raw *RawOperations) {
	if raw != nil && raw.Note != nil && *raw.Note != "" {
		fmt.Fprintf(w, "note: %s\n", *raw.Note)
	}
}

// newGroupsCmd returns the `meho operation groups <connector_id>` command.
//
// CLI shape (matches issue #481 spec):
//
//	meho operation groups <connector_id> \
//	  [--include-raw]                         # also count raw operations
//	  [--group <group_key>]                   # list one group only
//	  [--cursor <group_key>]                  # the next page
//	  [--json]                                # machine-readable output
//	  [--backplane <url>]                     # override the backplane URL
//
// Exit codes:
//   - 0   groups listed cleanly (including the "0 enabled groups" empty case)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
func newGroupsCmd() *cobra.Command {
	var (
		query             groupsQuery
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
			return runGroups(cmd, args[0], query, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&query.includeRaw, "include-raw", false,
		"also count raw operations made from the vendor API description "+
			"(matters only while the backplane switch SEARCH_TYPED_FIRST is on)")
	cmd.Flags().StringVar(&query.group, "group", "",
		"list only this group (matters only while the backplane switch "+
			"GROUP_LISTING_OPERATIONS is on)")
	cmd.Flags().StringVar(&query.cursor, "cursor", "",
		"the next_cursor of the previous page, to list the next page")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runGroups(cmd *cobra.Command, connectorID string, query groupsQuery, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	result, err := getGroups(cmd.Context(), client, connectorID, query)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printGroupsTable(cmd.OutOrStdout(), result)
	return nil
}

// getGroups issues the typed GET via the generated client, runs the
// one-shot 401-refresh dance via the AuthedClient's Refresh hook
// (mirroring api.AuthedClient.GetHealth), and unmarshals the 200
// body into the hand-typed GroupsResponse. Non-2xx outcomes wrap as
// *apiResponseError for renderRequestError to classify.
func getGroups(ctx context.Context, client operationsAPI, connectorID string, query groupsQuery) (*GroupsResponse, error) {
	params := &api.GetGroupsApiV1OperationsGroupsGetParams{
		ConnectorId: connectorID,
	}
	if query.includeRaw {
		params.IncludeRaw = &query.includeRaw
	}
	if query.group != "" {
		params.Group = &query.group
	}
	if query.cursor != "" {
		params.Cursor = &query.cursor
	}
	resp, err := client.GetGroupsApiV1OperationsGroupsGetWithResponse(ctx, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.GetGroupsApiV1OperationsGroupsGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out GroupsResponse
	if err := json.Unmarshal(resp.Body, &out); err != nil {
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
	defer printRawNote(w, r.RawOperations)
	if r.Note != "" {
		defer fmt.Fprintf(w, "note: %s\n", r.Note)
	}
	if r.NextCursor != nil && *r.NextCursor != "" {
		defer fmt.Fprintf(w, "more groups: run again with --cursor %s\n", *r.NextCursor)
	}
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
		printGroupOperations(w, g)
	}
}

// printGroupOperations prints the operation lines of one group, one per
// line, under the group's row.
func printGroupOperations(w io.Writer, g GroupSummary) {
	var operations []GroupOperation
	if g.Operations != nil {
		operations = *g.Operations
	}
	for _, op := range operations {
		approval := ""
		if op.RequiresApproval {
			approval = ", needs approval"
		}
		summary := ""
		if op.Summary != nil {
			summary = *op.Summary
		}
		fmt.Fprintf(w, "    %s (%s%s) %s\n", op.OpID, op.SafetyLevel, approval, summary)
	}
	if g.OperationsMore > 0 {
		fmt.Fprintf(w, "    … %d more: use meho operation search with --group %s\n", g.OperationsMore, g.GroupKey)
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
