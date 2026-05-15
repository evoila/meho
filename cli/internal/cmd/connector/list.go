// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"fmt"
	"io"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// Summary mirrors the backend ConnectorListItem Pydantic model
// (operations/ingest/api_schemas.py) verbatim: one row per ingested
// connector with parsed coordinates + tenant scope + per-status group
// counts + bulk op count for the operator's overview table. The full
// per-op detail comes from `meho connector review <id>`.
//
// Named `Summary` (rather than `ConnectorListItem`) inside the
// `connector` package to satisfy the revive/stutter rule —
// `connector.ConnectorListItem` is the linter's least-favourite
// double; `connector.Summary` reads naturally at the call site.
//
// Note: the canonical payload has no `review_status` field. The
// review state is per-group (some groups can be staged while others
// are enabled — common when an operator partially approves a
// connector). The operator-facing rollup label rendered in the
// human table is derived from the three per-status counts at render
// time; see `deriveRollupLabel`.
//
// `tenant_id` is a UUID for tenant-curated connectors and JSON `null`
// for built-in connectors. Pointer-to-string so we can distinguish
// "field absent" from "empty string" in the rendered table.
type Summary struct {
	ConnectorID        string  `json:"connector_id"`
	Product            string  `json:"product"`
	Version            string  `json:"version"`
	ImplID             string  `json:"impl_id"`
	TenantID           *string `json:"tenant_id"`
	GroupCount         int     `json:"group_count"`
	StagedGroupCount   int     `json:"staged_group_count"`
	EnabledGroupCount  int     `json:"enabled_group_count"`
	DisabledGroupCount int     `json:"disabled_group_count"`
	OperationCount     int     `json:"operation_count"`
}

// ListResponse is the envelope for GET /api/v1/connectors.
type ListResponse struct {
	Connectors []Summary `json:"connectors"`
}

// validStatuses pins the allowed --status values. `all` is the
// no-filter default; the wire shape uses an absent status query
// param for that case (T6's `Literal | None` semantic).
var validStatuses = map[string]struct{}{
	"staged":   {},
	"enabled":  {},
	"disabled": {},
	"all":      {},
}

// newListCmd returns the `meho connector list` command.
//
// CLI shape:
//
//	meho connector list [--status staged|enabled|disabled|all] [--json] [--backplane <url>]
//
// Hits GET /api/v1/connectors?status=... and renders the result as
// a one-line-per-connector table. operator role suffices (read-only).
func newListCmd() *cobra.Command {
	var (
		status            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List ingested connectors filtered by review status",
		Long: "list calls GET /api/v1/connectors against the configured\n" +
			"backplane and renders one row per ingested connector with its\n" +
			"review state + group/op counts. Default `--status all` lists\n" +
			"every connector visible to the operator's tenant (incl. built-in\n" +
			"connectors with tenant_id IS NULL).\n\n" +
			"Operator-role tokens can list; tenant_admin is not required for\n" +
			"this read-only verb.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Status:            status,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&status, "status", "all",
		"filter by review status: staged | enabled | disabled | all (default all)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Status            string
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if _, ok := validStatuses[opts.Status]; !ok {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--status %q invalid; expected one of: staged | enabled | disabled | all",
				opts.Status,
			)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := getList(cmd.Context(), backplaneURL, opts.Status)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printListTable(cmd.OutOrStdout(), opts.Status, result)
	return nil
}

func getList(ctx context.Context, backplaneURL, status string) (*ListResponse, error) {
	path := "/api/v1/connectors"
	if status != "" && status != "all" {
		q := url.Values{}
		q.Set("status", status)
		path += "?" + q.Encode()
	}
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	var out ListResponse
	if err := decodeJSON(raw, "connector list", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func printListTable(w io.Writer, status string, r *ListResponse) {
	if len(r.Connectors) == 0 {
		fmt.Fprintf(w, "0 connector(s) with status=%s\n", status)
		return
	}
	fmt.Fprintf(w, "%d connector(s) with status=%s\n", len(r.Connectors), status)
	fmt.Fprintf(w, "%-32s %-10s %-10s %5s %5s\n",
		"connector_id", "status", "tenant", "grps", "ops",
	)
	for _, c := range r.Connectors {
		tenant := "(built-in)"
		if c.TenantID != nil && *c.TenantID != "" {
			tenant = *c.TenantID
		}
		fmt.Fprintf(w, "%-32s %-10s %-10s %5d %5d\n",
			truncate(c.ConnectorID, 32),
			deriveRollupLabel(c.StagedGroupCount, c.EnabledGroupCount, c.DisabledGroupCount),
			truncate(tenant, 10),
			c.GroupCount,
			c.OperationCount,
		)
	}
}

// deriveRollupLabel computes a connector-wide status rollup from the
// three per-group review_status counts the backend ships in
// ConnectorListItem. The canonical payload has no top-level
// review_status (a connector can hold a mix of staged / enabled /
// disabled groups), so the operator-facing label is derived here.
//
// Rules (load-bearing; mirrored in the `review` verb's header):
//
//   - "(empty)"  — connector has zero groups (post-ingest before
//     grouping, or every group was orphan-only and got pruned).
//   - "staged"   — at least one group is still awaiting review and
//     none have been enabled yet. The operator-facing question is
//     "is there anything left to review" and the answer is yes.
//   - "enabled"  — every group is enabled. The connector is fully
//     live; all `is_enabled=true` operations are dispatchable.
//   - "disabled" — every group is disabled. The connector was
//     rolled back; no operations are dispatchable. Per-op overrides
//     are preserved (a future re-enable picks them back up).
//   - "mixed"    — partial enable / partial review state, or an
//     unknown enum value the CLI doesn't recognise. The CLI bias
//     here is conservative: surface the heterogeneity rather than
//     hide it under a single label.
func deriveRollupLabel(staged, enabled, disabled int) string {
	total := staged + enabled + disabled
	if total == 0 {
		return "(empty)"
	}
	if staged == total {
		return "staged"
	}
	if enabled == total {
		return "enabled"
	}
	if disabled == total {
		return "disabled"
	}
	// At least two of {staged, enabled, disabled} are non-zero, or
	// the per-status counts don't sum to group_count (server shipped
	// a status the CLI doesn't recognise). "mixed" is the right
	// operator-facing answer in both cases — surface the
	// heterogeneity rather than hide it under a single label.
	return "mixed"
}
