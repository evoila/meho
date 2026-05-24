// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho conventions list` command.
//
//	meho conventions list [--kind K] [--json] [--backplane <url>]
//
// Role: operator. Lists the operator's tenant's conventions, sorted
// `priority DESC, created_at ASC` (the same key the T4 preamble
// assembler uses for packing — so the list view surfaces conventions
// in the order T4 would consider them).
//
// --kind narrows by kind (operational | workflow | reference); when
// omitted, all kinds are returned. The backend route's only filter is
// --kind; for paginated browsing of a large tenant, the operator
// scopes via the kind flag, not via limit/offset (the substrate does
// not paginate; v0.2 list returns the full set, ordered).
//
// Acceptance criterion note (issue body): "lowest-priority slugs that
// will be dropped when the tenant's operational set exceeds the
// preamble budget" — that signal lives in the T4 preamble assembler
// (sibling task #316), not in the T2 list endpoint. The list verb here
// renders the priority column verbatim so an operator can spot
// over-budget candidates manually; the dropped-slugs warning wires in
// once T4 exposes that information. See `printOverBudgetWarning`
// below for the structural placeholder.
//
// Exit codes:
//   - 0   list returned cleanly (including zero rows)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
//   - 5   insufficient_role
func newListCmd() *cobra.Command {
	var (
		kind              string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List tenant conventions, optionally filtered by kind",
		Long: "list calls GET /api/v1/conventions and renders the " +
			"conventions registered in the operator's tenant, sorted " +
			"priority DESC, created_at ASC — the same order T4's preamble " +
			"assembler uses. --kind filters by operational | workflow | " +
			"reference. --json emits the raw ListResponse envelope for jq " +
			"pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Kind:              kind,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&kind, "kind", "",
		"narrow entries by kind: operational | workflow | reference")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Kind              string
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	// Validate --kind locally so an operator typo surfaces immediately
	// rather than as a remote 422 the user has to decode.
	if opts.Kind != "" && !validKinds[opts.Kind] {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--kind must be one of: operational, workflow, reference; got %q",
				opts.Kind,
			)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printListTable(cmd.OutOrStdout(), resp)
	return nil
}

// buildListPath assembles the GET /api/v1/conventions query string from
// the per-call options. Exposed for unit tests so the URL construction
// stays unit-checkable without standing up an httptest.Server.
func buildListPath(opts listOptions) string {
	q := url.Values{}
	if opts.Kind != "" {
		q.Set("kind", opts.Kind)
	}
	path := "/api/v1/conventions"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getList(ctx context.Context, backplaneURL string, opts listOptions) (*ListResponse, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out ListResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode conventions list response: %w", err)
	}
	return &out, nil
}

// printListTable renders the list as a compact, scannable table.
// Columns: SLUG, KIND, PRIORITY, UPDATED, TITLE. The full ISO-8601
// timestamp is kept verbatim (not truncated) because operators
// correlating with audit-log rows want the precise updated_at.
func printListTable(w io.Writer, r *ListResponse) {
	if r == nil || len(r.Entries) == 0 {
		fmt.Fprintln(w, "no conventions registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-32s %-11s %-8s %-32s %s\n", "SLUG", "KIND", "PRIORITY", "UPDATED", "TITLE")
	for _, e := range r.Entries {
		fmt.Fprintf(w, "%-32s %-11s %-8d %-32s %s\n",
			truncate(e.Slug, 32),
			e.Kind,
			e.Priority,
			e.UpdatedAt,
			truncate(e.Title, 60),
		)
	}
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Rune-aware so multi-byte UTF-8 survives the
// cut. Same shape as the sibling-package helpers.
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
