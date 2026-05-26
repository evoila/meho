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
// Over-budget warning (G7.1-T7 #1094): the response carries
// `budget_status` -- the preamble budget arithmetic for the tenant.
// When `budget_status.over_budget == true`, this verb writes a prose
// warning to stderr naming the dropped slugs (the conventions that
// will NOT reach an agent session) and exits with code 5
// (insufficient_budget). The table still goes to stdout cleanly so a
// scripted consumer redirecting stdout to a file still gets the data,
// just with a non-zero return code. `--json` mode is the scripting
// surface: it emits the full envelope (entries + budget_status) and
// exits 0 regardless -- JSON consumers parse budget_status
// themselves.
//
// Exit codes:
//   - 0   list returned cleanly (including zero rows, or --json over
//     an over-budget tenant)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
//   - 5   insufficient_role (HTTP 403) OR insufficient_budget (table
//     mode on an over-budget tenant; the error code in the JSON
//     envelope distinguishes the two)
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
		// --json mode is the agent / scripting surface: emit the full
		// envelope (entries + budget_status) and exit 0 regardless of
		// over-budget state. JSON consumers parse budget_status
		// themselves; the human warning + exit-code-5 branch is for
		// the table mode below.
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printListTable(cmd.OutOrStdout(), resp)
	// G7.1-T7 (#1094): when the tenant is over budget, the table is
	// still useful (it shows what's there), so we always print it.
	// But we also need to alert the operator that some conventions
	// will NOT reach agent sessions. The warning goes to stderr (not
	// stdout) so stdout-piping consumers (less, glow, jq via --json)
	// don't see it mid-stream; the exit code (5 = insufficient_budget)
	// makes the failure machine-detectable. T3 #315 deferred this AC
	// because the list endpoint had no dropped_slugs surface; T7 is
	// the plumbing that finally satisfies it.
	if resp != nil && resp.BudgetStatus.OverBudget {
		printOverBudgetWarning(cmd.ErrOrStderr(), &resp.BudgetStatus)
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.InsufficientBudget(formatBudgetDetail(&resp.BudgetStatus)),
			false, // JSON mode exited earlier; this path is human-only
		)
	}
	return nil
}

// printOverBudgetWarning writes the prose warning block to stderr.
// Kept separate from the structured-error envelope so the operator
// sees the full context (what's dropped, why, how to fix) on stderr
// even when scripts route only the StructuredError JSON envelope.
// The exact wording mirrors the issue body's specimen:
//
//	WARNING: tenant operational conventions exceed preamble budget
//	  (max_tokens=N; estimated=M).
//	The following conventions will be DROPPED from the agent
//	session preamble (lowest priority first):
//	  - slug-one
//	  - slug-two
//	Resolve: PATCH the dropped conventions to a higher priority, or
//	shorten / split high-priority entries.
//
// The remediation hint is concrete — operators routinely run `meho
// conventions edit <slug>` after seeing this, so the message points
// them at the right next action.
func printOverBudgetWarning(stderrW io.Writer, bs *BudgetStatus) {
	fmt.Fprintf(stderrW,
		"WARNING: tenant operational conventions exceed preamble budget "+
			"(max_tokens=%d; estimated=%d).\n",
		bs.MaxTokens, bs.EstimatedTokens,
	)
	fmt.Fprintln(stderrW,
		"The following conventions will be DROPPED from the agent session "+
			"preamble (lowest priority first):")
	for _, slug := range bs.DroppedSlugs {
		fmt.Fprintf(stderrW, "  - %s\n", slug)
	}
	fmt.Fprintln(stderrW,
		"Resolve: PATCH the dropped conventions to a higher priority, or "+
			"shorten / split high-priority entries.")
}

// formatBudgetDetail produces the short detail string the JSON error
// envelope carries. The prose warning above is for human eyes; this
// is the structured detail jq-style consumers see in the JSON
// envelope's "detail" field. Compact + machine-friendly: names the
// slug count and the overflow magnitude.
func formatBudgetDetail(bs *BudgetStatus) string {
	return fmt.Sprintf(
		"%d operational convention(s) will be dropped from the agent preamble "+
			"(estimated=%d, max_tokens=%d): %v",
		len(bs.DroppedSlugs), bs.EstimatedTokens, bs.MaxTokens, bs.DroppedSlugs,
	)
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
