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

// Summary mirrors the backend ConnectorSummary Pydantic model: one
// row per ingested connector with high-level review state + bulk op
// count for the operator's overview table. The full per-op detail
// comes from `meho connector review <id>`.
//
// Named `Summary` (rather than `ConnectorSummary`) inside the
// `connector` package to satisfy the revive/stutter rule —
// `connector.ConnectorSummary` is the linter's least-favourite
// double; `connector.Summary` reads naturally at the call site.
type Summary struct {
	ConnectorID    string `json:"connector_id"`
	Product        string `json:"product"`
	Version        string `json:"version"`
	ImplID         string `json:"impl_id"`
	ReviewStatus   string `json:"review_status"`
	GroupCount     int    `json:"group_count"`
	OperationCount int    `json:"operation_count"`
	TenantID       string `json:"tenant_id,omitempty"`
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
		tenant := c.TenantID
		if tenant == "" {
			tenant = "(built-in)"
		}
		fmt.Fprintf(w, "%-32s %-10s %-10s %5d %5d\n",
			truncate(c.ConnectorID, 32),
			c.ReviewStatus,
			truncate(tenant, 10),
			c.GroupCount,
			c.OperationCount,
		)
	}
}
