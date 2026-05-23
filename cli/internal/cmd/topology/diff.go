// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDiffCmd returns the `meho topology diff` command.
//
//	meho topology diff <ts1> <ts2> \
//	  [--kind <node-or-edge-kind>]   # narrow to one domain kind
//	  [--changed-only]               # suppress last_seen-only updates
//	  [--json]                       # raw TopologyDiffResult JSON
//	  [--backplane <url>]            # override the configured backplane
//
// Calls GET /api/v1/topology/diff. The two timestamp positional args
// accept either duration shorthand (24h / 7d / 30m / 2w) resolved
// client-side or an ISO-8601 absolute timestamp.
//
// Output: structured summary by default ("N nodes created, M edges
// removed, K updated; total P"); --json carries the full
// TopologyDiffResult with per-entry detail.
//
// 1000-row hard cap: when the server caps the result, the CLI surfaces
// the truncation marker plus the canonical "narrow the time window"
// hint so the operator sees the remediation path inline (--json carries
// the same truncated flag for scripted consumers).
//
// Exit codes (shared with sibling topology verbs):
//   - 0   query returned cleanly (including a truncated result).
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. 400 invalid_window for an
//     inverted ts1/ts2 pair).
//   - 5   insufficient_role
func newDiffCmd() *cobra.Command {
	var (
		kind              string
		changedOnly       bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "diff <ts1> <ts2>",
		Short: "Diff the topology graph between two timestamps (G9.3 history)",
		Long: "diff calls GET /api/v1/topology/diff and renders the net " +
			"per-resource delta between ts1 (exclusive) and ts2 " +
			"(inclusive). ts1 / ts2 accept either duration shorthand " +
			"(24h / 7d / 30m / 2w) resolved client-side to an absolute " +
			"ISO-8601 timestamp, or an ISO-8601 datetime directly. " +
			"--kind narrows the result to one domain kind (a node kind " +
			"like `vm` or an edge kind like `runs-on`); --changed-only " +
			"suppresses `updated` entries whose only mutation was a " +
			"`last_seen` bump (refresh-service heartbeats). --json " +
			"emits the raw TopologyDiffResult so operators can pipe " +
			"into jq. Output is hard-capped at 1000 entries; when the " +
			"cap fires, narrow the time window with closer ts1/ts2 " +
			"values or filter by --kind.",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDiff(cmd, diffOptions{
				TS1:               args[0],
				TS2:               args[1],
				Kind:              kind,
				ChangedOnly:       changedOnly,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&kind, "kind", "",
		"narrow to one resource kind (node kind like `vm` or edge kind like `runs-on`)")
	cmd.Flags().BoolVar(&changedOnly, "changed-only", false,
		"suppress `updated` entries whose only mutation was a `last_seen` bump")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw TopologyDiffResult JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// diffOptions is the per-call option bag for runDiff.
type diffOptions struct {
	TS1               string
	TS2               string
	Kind              string
	ChangedOnly       bool
	JSONOut           bool
	BackplaneOverride string
}

func runDiff(cmd *cobra.Command, opts diffOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, err := getDiff(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printDiffSummary(cmd.OutOrStdout(), result)
	return nil
}

// buildDiffPath assembles the GET path + query string for a diff call.
// ts1 / ts2 are both resolved client-side from duration shorthand to
// absolute ISO-8601 to match the REST router's absolute-only contract
// (mirrors the timeline verb).
func buildDiffPath(opts diffOptions, now time.Time) (string, error) {
	q := url.Values{}
	ts1ISO, err := resolveDurationOrISO(opts.TS1, now)
	if err != nil {
		return "", fmt.Errorf("ts1 %q: %w", opts.TS1, err)
	}
	q.Set("ts1", ts1ISO)
	ts2ISO, err := resolveDurationOrISO(opts.TS2, now)
	if err != nil {
		return "", fmt.Errorf("ts2 %q: %w", opts.TS2, err)
	}
	q.Set("ts2", ts2ISO)
	if opts.Kind != "" {
		q.Set("kind", opts.Kind)
	}
	if opts.ChangedOnly {
		q.Set("changed_only", strconv.FormatBool(true))
	}
	return "/api/v1/topology/diff?" + q.Encode(), nil
}

func getDiff(ctx context.Context, backplaneURL string, opts diffOptions) (*DiffResult, error) {
	path, err := buildDiffPath(opts, time.Now().UTC())
	if err != nil {
		return nil, err
	}
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	var out DiffResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode diff response: %w", err)
	}
	return &out, nil
}

// DiffEntry mirrors the backend `TopologyDiffEntry` Pydantic model
// (`backend/src/meho_backplane/topology/schemas.py`). Hand-written for
// the same reason `TimelineEntry` is: keep the topology package
// decoupled from oapi-codegen churn.
type DiffEntry struct {
	ChangeKind string  `json:"change_kind"`
	Source     string  `json:"source"`
	ResourceID *string `json:"resource_id"`
	Kind       string  `json:"kind"`
	Name       *string `json:"name"`
	Summary    string  `json:"summary"`
}

// DiffResult mirrors the backend `TopologyDiffResult`. `TruncationHint`
// keeps the JSON key as `null` (no `omitempty`) so a re-marshal
// preserves the Pydantic wire shape.
type DiffResult struct {
	Entries        []DiffEntry `json:"entries"`
	Truncated      bool        `json:"truncated"`
	TruncationHint *string     `json:"truncation_hint"`
}

// printDiffSummary renders the diff result as a structured summary:
// counts by (source, change_kind) plus a truncation banner when the
// server cap fired. The JSON path carries the same data with per-entry
// detail for callers who want the full list.
func printDiffSummary(w io.Writer, r *DiffResult) {
	if r == nil {
		fmt.Fprintln(w, "no diff result")
		return
	}
	if len(r.Entries) == 0 {
		fmt.Fprintln(w, "no graph changes in the requested window")
		if r.Truncated && r.TruncationHint != nil {
			fmt.Fprintln(w, *r.TruncationHint)
		}
		return
	}
	// Counts per (source, change_kind). A small fixed grid is more
	// scannable than a free-form histogram for the four kinds we
	// actually surface.
	type key struct{ source, kind string }
	counts := map[key]int{}
	for _, e := range r.Entries {
		counts[key{e.Source, e.ChangeKind}]++
	}
	render := func(src string) {
		created := counts[key{src, "created"}]
		updated := counts[key{src, "updated"}]
		removed := counts[key{src, "removed"}]
		fmt.Fprintf(w, "%-6s  created=%-4d  updated=%-4d  removed=%-4d\n",
			src, created, updated, removed)
	}
	fmt.Fprintln(w, "diff summary:")
	render("node")
	render("edge")
	fmt.Fprintf(w, "total entries: %d\n", len(r.Entries))
	if r.Truncated {
		hint := ""
		if r.TruncationHint != nil {
			hint = *r.TruncationHint
		}
		fmt.Fprintln(w, "TRUNCATED:", hint)
	}
}
