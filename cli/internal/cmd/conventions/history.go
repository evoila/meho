// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newHistoryCmd returns the `meho conventions history` command.
//
//	meho conventions history <slug> [--limit N] [--json] [--backplane <url>]
//
// Role: operator. Fetches the convention's edit trail via
// GET /api/v1/conventions/{slug}/history, newest first (the route's
// ORDER BY `ts DESC` enforces this).
//
// Default output renders unified diffs between consecutive entries:
// for each pair of rows newest→oldest, the diff is between
// `body_before` and `body_after` of that row, showing what changed in
// that single edit. The first history row (CREATE) has no body_before
// and shows only `body_after`; the DELETE row keeps the final body in
// `body_after` for forensic completeness.
//
// `--limit N` bounds the number of history rows fetched + rendered
// (client-side cap; the route returns the full trail). `--json` emits
// the raw history rows for jq pipelines.
//
// Exit codes:
//   - 0   history rendered cleanly
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (includes 404 slug-not-found)
//   - 5   insufficient_role
func newHistoryCmd() *cobra.Command {
	var (
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "history <slug>",
		Short: "Show the edit history of one convention",
		Long: "history calls GET /api/v1/conventions/{slug}/history and " +
			"renders the convention's edit trail newest first. Default " +
			"output renders unified diffs between consecutive entries — " +
			"each row shows what changed in that single edit. The CREATE " +
			"row has no body_before and renders only the initial body; " +
			"the DELETE row keeps the final body in body_after for " +
			"forensic completeness. --limit N caps the number of rows " +
			"rendered (default: all). --json emits the raw history rows.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runHistory(cmd, historyOptions{
				Slug:              args[0],
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 0,
		"cap the number of history rows rendered (default: all)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw history rows as JSON instead of the unified-diff view")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type historyOptions struct {
	Slug              string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runHistory(cmd *cobra.Command, opts historyOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("history requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	if opts.Limit < 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be non-negative; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	entries, err := getHistory(cmd.Context(), backplaneURL, opts.Slug)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.Limit > 0 && len(entries) > opts.Limit {
		entries = entries[:opts.Limit]
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entries)
	}
	printHistoryDiffs(cmd.OutOrStdout(), opts.Slug, entries)
	return nil
}

// buildHistoryPath assembles the GET path. Exposed for unit tests.
func buildHistoryPath(slug string) string {
	return "/api/v1/conventions/" + pathEscape(slug) + "/history"
}

func getHistory(ctx context.Context, backplaneURL, slug string) ([]HistoryEntry, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildHistoryPath(slug), nil)
	if err != nil {
		return nil, err
	}
	var out []HistoryEntry
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode conventions history response: %w", err)
	}
	return out, nil
}

// printHistoryDiffs renders each history row as a header + a unified
// diff of body_before → body_after for that single edit. The CREATE
// row (no body_before) renders the initial body without a diff prefix;
// the DELETE row would surface as a final-body block too. Subsequent
// edits show the per-edit diff so an operator scanning the trail sees
// what each session changed.
//
// Why per-row diffs (not cross-row): each history row already carries
// both before + after for the edit that produced it. Computing diffs
// across consecutive rows (row[i].body_after vs row[i+1].body_after)
// would double-emit the same change and lose the natural
// transaction boundary. The substrate writes one history row per
// transaction, so the row-local before/after pair is already the right
// granularity.
func printHistoryDiffs(w io.Writer, slug string, entries []HistoryEntry) {
	if len(entries) == 0 {
		fmt.Fprintf(w, "no history for convention %q\n", slug)
		return
	}
	for i, e := range entries {
		if i > 0 {
			fmt.Fprintln(w)
		}
		fmt.Fprintf(w, "=== %s  actor=%s  history_id=%s\n", e.Ts, e.ActorSub, e.ID)
		if e.AuditID != nil {
			fmt.Fprintf(w, "    audit_id=%s\n", *e.AuditID)
		} else {
			fmt.Fprintln(w, "    audit_id=<seed>")
		}

		if e.BodyBefore == nil {
			// CREATE row (or any other row with no prior state). Show
			// the initial body as a + block so the trail visibly
			// distinguishes the create from a body-replacing edit.
			fmt.Fprintln(w, "--- /dev/null")
			fmt.Fprintf(w, "+++ %s @ %s\n", slug, e.Ts)
			for _, line := range strings.Split(strings.TrimRight(e.BodyAfter, "\r\n"), "\n") {
				fmt.Fprintf(w, "+ %s\n", line)
			}
			continue
		}

		before := strings.TrimRight(*e.BodyBefore, "\r\n")
		after := strings.TrimRight(e.BodyAfter, "\r\n")
		if before == after {
			// Title- or priority-only edit — body unchanged. The
			// history row still exists (the substrate writes one per
			// PATCH whether or not body changed), but the diff is
			// empty. Surface that explicitly rather than printing a
			// blank section.
			fmt.Fprintln(w, "    (body unchanged — title/priority edit)")
			continue
		}
		fmt.Fprintf(w, "--- %s @ <prev>\n", slug)
		fmt.Fprintf(w, "+++ %s @ %s\n", slug, e.Ts)
		writeUnifiedDiff(w, before, after)
	}
}

// writeUnifiedDiff emits a simple line-oriented diff in unified
// format. This is not a full LCS-quality diff (real `diff -u` quality
// would require pulling in a 3rd-party library or implementing
// Myers); it's a structural diff: lines present in `before` but not
// `after` are `-`, lines present in `after` but not `before` are `+`,
// shared lines are context. For typical convention edits (a small
// number of rule lines edited at a time), the structural diff renders
// cleanly enough for the operator to see "what changed". The `--json`
// path stays as the escape valve for operators who need a precise
// machine-readable diff.
//
// Implementation choice: we deliberately avoid a third-party diff
// library to keep the CLI's dependency surface minimal — the binary
// ships to operators who run it on laptops, in CI, and inside
// containers; every transitive dependency adds maintenance debt and a
// supply-chain edge.
func writeUnifiedDiff(w io.Writer, before, after string) {
	beforeLines := strings.Split(before, "\n")
	afterLines := strings.Split(after, "\n")

	// Build a map of after-lines for O(N) presence checks. We deliberately
	// do NOT use a multiset because operationally-realistic convention
	// bodies don't have many duplicated lines; the simple presence test
	// is good enough.
	afterSet := make(map[string]bool, len(afterLines))
	for _, line := range afterLines {
		afterSet[line] = true
	}
	beforeSet := make(map[string]bool, len(beforeLines))
	for _, line := range beforeLines {
		beforeSet[line] = true
	}

	// Walk before-lines, emitting `-` for lines not in after, ` ` for
	// lines that are. Then walk after-lines for the `+` adds (lines
	// not in before). This is the simplest diff that surfaces the
	// shape of a change without the complexity of a real Myers
	// implementation; for runbook-style Markdown bodies it reads
	// fine. Operators wanting a real diff pipe `--json | jq -r
	// '.[]|.body_after' | diff -u <(meho conventions history ... --json | jq...)`.
	for _, line := range beforeLines {
		if afterSet[line] {
			fmt.Fprintf(w, "  %s\n", line)
		} else {
			fmt.Fprintf(w, "- %s\n", line)
		}
	}
	for _, line := range afterLines {
		if !beforeSet[line] {
			fmt.Fprintf(w, "+ %s\n", line)
		}
	}
}
