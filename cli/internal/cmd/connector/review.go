// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// ReviewOperation mirrors the backend ConnectorReviewOp Pydantic
// model verbatim (see operations/ingest/payload.py). The fields cover
// the operator-decision surface: which op is it, what does it do
// (Summary / Description / CustomDescription), is it safe to run, does
// it need approval, is it currently dispatchable? Tags carry the
// LLM-derived group hints so the operator can spot mis-grouped ops at
// review time.
type ReviewOperation struct {
	OpID              string   `json:"op_id"`
	Summary           *string  `json:"summary"`
	Description       *string  `json:"description"`
	CustomDescription *string  `json:"custom_description"`
	SafetyLevel       string   `json:"safety_level"`
	RequiresApproval  bool     `json:"requires_approval"`
	IsEnabled         bool     `json:"is_enabled"`
	Tags              []string `json:"tags"`
}

// ReviewGroup mirrors the backend ConnectorReviewGroup Pydantic model
// verbatim (operations/ingest/payload.py): the LLM-summarised group
// payload plus the operations the LLM assigned to it. The wire field
// is `ops` (not `operations`) — Pydantic's `model_dump()` emits the
// attribute name, and aligning the Go tag is load-bearing for the
// JSON envelope to decode.
type ReviewGroup struct {
	GroupKey     string            `json:"group_key"`
	Name         string            `json:"name"`
	WhenToUse    string            `json:"when_to_use"`
	ReviewStatus string            `json:"review_status"`
	OpCount      int               `json:"op_count"`
	Ops          []ReviewOperation `json:"ops"`
}

// ReviewPayload mirrors the backend ConnectorReviewPayload Pydantic
// model verbatim. Note `review_status` is per-group, not top-level —
// a connector can have a mix of staged / enabled / disabled groups
// (the operator-facing list verb derives a rollup label from the
// per-status counts; see list.go).
//
// `tenant_id` is a UUID for tenant-curated connectors and JSON `null`
// for built-in / global connectors; rendered as the empty string in
// human output and as a pointer in JSON.
type ReviewPayload struct {
	ConnectorID  string        `json:"connector_id"`
	Product      string        `json:"product"`
	Version      string        `json:"version"`
	ImplID       string        `json:"impl_id"`
	TenantID     *string       `json:"tenant_id"`
	Groups       []ReviewGroup `json:"groups"`
	TotalOpCount int           `json:"total_op_count"`
}

// newReviewCmd returns the `meho connector review <connector_id>` command.
func newReviewCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "review <connector_id>",
		Short: "Show the per-group + per-op review payload for one connector",
		Long: "review calls GET /api/v1/connectors/<connector_id>/review and\n" +
			"renders the full review payload — groups (with their LLM-derived\n" +
			"`when_to_use` hints) and per-op flags (safety_level,\n" +
			"requires_approval, is_enabled). Use this before flipping a staged\n" +
			"connector to enabled — operators are expected to verify the\n" +
			"groupings + override per-op safety_level / requires_approval flags\n" +
			"as appropriate via `meho connector edit-group` and\n" +
			"`meho connector edit-op` before running\n" +
			"`meho connector enable <connector_id> --confirm`.\n\n" +
			"--json returns the full machine-readable payload (suitable for\n" +
			"piping to jq / saving for a review checkpoint).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runReview(cmd, reviewOptions{
				ConnectorID:       args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type reviewOptions struct {
	ConnectorID       string
	JSONOut           bool
	BackplaneOverride string
}

func runReview(cmd *cobra.Command, opts reviewOptions) error {
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := getReview(cmd.Context(), backplaneURL, opts.ConnectorID)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printReviewTable(cmd.OutOrStdout(), result)
	return nil
}

func getReview(ctx context.Context, backplaneURL, connectorID string) (*ReviewPayload, error) {
	path := "/api/v1/connectors/" + pathEscapeOpID(connectorID) + "/review"
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	var out ReviewPayload
	if err := decodeJSON(raw, "review", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func printReviewTable(w io.Writer, r *ReviewPayload) {
	// Top-level header carries the rollup label derived from the
	// per-group review_status counts — the canonical payload no
	// longer ships a connector-wide `review_status` field (it's
	// per-group), so we recompute the operator-facing summary here
	// the same way `meho connector list` does (see deriveRollupLabel).
	staged, enabled, disabled := groupStatusCounts(r.Groups)
	rollup := deriveRollupLabel(staged, enabled, disabled)
	fmt.Fprintf(w, "%s (%s/%s/%s) — %s — %d group(s), %d op(s)\n",
		r.ConnectorID, r.Product, r.Version, r.ImplID,
		rollup, len(r.Groups), r.TotalOpCount,
	)
	if len(r.Groups) == 0 {
		fmt.Fprintln(w, "(no groups; the connector has no operations or the grouping pass produced no buckets)")
		return
	}
	for _, g := range r.Groups {
		fmt.Fprintf(w, "\n[%s] %s — review_status=%s — %d op(s)\n",
			g.GroupKey, g.Name, g.ReviewStatus, g.OpCount,
		)
		if g.WhenToUse != "" {
			fmt.Fprintf(w, "  when_to_use: %s\n", g.WhenToUse)
		}
		if len(g.Ops) == 0 {
			continue
		}
		// Compact one-line-per-op render. Operators with a 3000-op
		// vcenter connector cannot scroll through a verbose render;
		// --json carries the full descriptions for the "ok now show
		// me everything" path.
		fmt.Fprintf(w, "  %-42s %-9s %3s %3s  %s\n",
			"op_id", "safety", "req", "en", "summary",
		)
		for _, op := range g.Ops {
			fmt.Fprintf(w, "  %-42s %-9s %3s %3s  %s\n",
				truncate(op.OpID, 42),
				op.SafetyLevel,
				boolFlag(op.RequiresApproval),
				boolFlag(op.IsEnabled),
				truncate(strDeref(op.Summary), 70),
			)
		}
	}
}

// groupStatusCounts buckets the per-group review_status values for a
// connector-wide rollup. Returned in (staged, enabled, disabled) order.
// Unknown values (e.g. a future enum addition) are silently dropped —
// the rollup label falls back to "mixed" when the buckets don't
// reconcile cleanly, which is the right operator-facing answer for
// an unrecognised state anyway.
func groupStatusCounts(groups []ReviewGroup) (staged, enabled, disabled int) {
	for _, g := range groups {
		switch g.ReviewStatus {
		case "staged":
			staged++
		case "enabled":
			enabled++
		case "disabled":
			disabled++
		}
	}
	return staged, enabled, disabled
}

// boolFlag renders a bool as a compact tristate-friendly cell for
// the review table. Y / . is loud-on-true / quiet-on-false so a
// glance picks out the operators-need-approval ops.
func boolFlag(v bool) string {
	if v {
		return " Y "
	}
	return " . "
}

// strDeref returns *s or empty when s is nil. Same shape as the
// operation sibling's helper; duplicated to avoid an import cycle.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
