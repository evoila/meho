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

// ReviewOperation mirrors the backend per-op review entry. The
// fields cover the operator-decision surface: which op is it, what
// does it do (Summary / CustomDescription), is it safe to run, does
// it need approval, is it currently dispatchable?
type ReviewOperation struct {
	OpID              string  `json:"op_id"`
	Summary           *string `json:"summary"`
	CustomDescription *string `json:"custom_description"`
	SafetyLevel       string  `json:"safety_level"`
	RequiresApproval  bool    `json:"requires_approval"`
	IsEnabled         bool    `json:"is_enabled"`
}

// ReviewGroup mirrors the backend per-group review entry: the
// LLM-summarised group payload plus the operations the LLM assigned
// to it.
type ReviewGroup struct {
	GroupKey   string            `json:"group_key"`
	Name       string            `json:"name"`
	WhenToUse  string            `json:"when_to_use"`
	Operations []ReviewOperation `json:"operations"`
}

// ReviewPayload is the GET /api/v1/connectors/{id}/review envelope.
type ReviewPayload struct {
	ConnectorID  string        `json:"connector_id"`
	Product      string        `json:"product"`
	Version      string        `json:"version"`
	ImplID       string        `json:"impl_id"`
	ReviewStatus string        `json:"review_status"`
	Groups       []ReviewGroup `json:"groups"`
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
	fmt.Fprintf(w, "%s (%s/%s/%s) — review_status=%s — %d group(s)\n",
		r.ConnectorID, r.Product, r.Version, r.ImplID,
		r.ReviewStatus, len(r.Groups),
	)
	if len(r.Groups) == 0 {
		fmt.Fprintln(w, "(no groups; the connector has no operations or the grouping pass produced no buckets)")
		return
	}
	for _, g := range r.Groups {
		fmt.Fprintf(w, "\n[%s] %s — %d op(s)\n", g.GroupKey, g.Name, len(g.Operations))
		if g.WhenToUse != "" {
			fmt.Fprintf(w, "  when_to_use: %s\n", g.WhenToUse)
		}
		if len(g.Operations) == 0 {
			continue
		}
		// Compact one-line-per-op render. Operators with a 3000-op
		// vcenter connector cannot scroll through a verbose render;
		// --json carries the full descriptions for the "ok now show
		// me everything" path.
		fmt.Fprintf(w, "  %-42s %-9s %3s %3s  %s\n",
			"op_id", "safety", "req", "en", "summary",
		)
		for _, op := range g.Operations {
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
