// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

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
		var he *httpResponseError
		if errors.As(err, &he) {
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printReviewTable(cmd.OutOrStdout(), result)
	return nil
}

// getReview drives the typed-client review endpoint with a one-shot
// 401-retry. The route declares `response_model=ConnectorReviewPayload`
// so JSON200 carries the typed envelope; non-2xx surfaces as
// *httpResponseError for the caller to route through renderHTTPStatus.
func getReview(ctx context.Context, backplaneURL, connectorID string) (*api.ConnectorReviewPayload, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.GetReviewEndpointApiV1ConnectorsConnectorIdReviewGetResponse, error) {
			return authed.GetReviewEndpointApiV1ConnectorsConnectorIdReviewGetWithResponse(
				ctx,
				connectorID,
				&api.GetReviewEndpointApiV1ConnectorsConnectorIdReviewGetParams{},
			)
		},
		func(r *api.GetReviewEndpointApiV1ConnectorsConnectorIdReviewGetResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		return nil, fmt.Errorf("backplane returned 200 OK but no JSON body decoded against ConnectorReviewPayload")
	}
	return resp.JSON200, nil
}

func printReviewTable(w io.Writer, r *api.ConnectorReviewPayload) {
	// Top-level header carries the rollup label derived from the
	// per-group review_status counts — the canonical payload no
	// longer ships a connector-wide `review_status` field (it's
	// per-group), so we recompute the operator-facing summary here
	// the same way `meho connector list` does (see deriveRollupLabel).
	staged, enabled, disabled := groupStatusCounts(r.Groups)
	rollup := deriveRollupLabel(staged, enabled, disabled)
	// kind / dispatchable carry server-side defaults, so oapi-codegen
	// types them as optional pointers even though the server always
	// sets them; deref defensively (empty kind / not-dispatchable on a
	// nil, the conservative reading).
	kind := ""
	if r.Kind != nil {
		kind = string(*r.Kind)
	}
	dispatchable := r.Dispatchable != nil && *r.Dispatchable
	fmt.Fprintf(w, "%s (%s/%s/%s) — %s — %s — %d group(s), %d op(s)\n",
		r.ConnectorId, r.Product, r.Version, r.ImplId,
		rollup, kindCell(kind, dispatchable),
		len(r.Groups), r.TotalOpCount,
	)
	printProvenance(w, r.Provenance)
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
				truncate(op.OpId, 42),
				op.SafetyLevel,
				boolFlag(op.RequiresApproval),
				boolFlag(op.IsEnabled),
				truncate(strDeref(op.Summary), 70),
			)
		}
	}
}

// printProvenance renders the per-spec ingest provenance (#2291) so an
// operator can tell a vendor artifact from a hand-mutated one before
// enabling reads: the fetched/inline/shipped origin, the audit uri, a
// short sha256 prefix over the raw bytes, who ingested it, and when. A
// connector ingested before the provenance table landed carries no
// rows; we say so explicitly rather than render a blank section.
func printProvenance(w io.Writer, provenance *[]api.ConnectorReviewProvenance) {
	if provenance == nil || len(*provenance) == 0 {
		fmt.Fprintln(w, "  provenance: unknown (pre-provenance) — ingested before provenance was recorded")
		return
	}
	fmt.Fprintf(w, "\nprovenance — %d spec(s):\n", len(*provenance))
	fmt.Fprintf(w, "  %-8s %-19s %-14s %s\n", "origin", "sha256", "operator", "uri")
	for _, p := range *provenance {
		operator := "—"
		if p.OperatorSub != nil && *p.OperatorSub != "" {
			operator = *p.OperatorSub
		}
		fmt.Fprintf(w, "  %-8s %-19s %-14s %s\n",
			p.Origin,
			truncate(p.Sha256, 19),
			truncate(operator, 14),
			p.Uri,
		)
	}
}

// groupStatusCounts buckets the per-group review_status values for a
// connector-wide rollup. Returned in (staged, enabled, disabled) order.
// Unknown values (e.g. a future enum addition) are silently dropped —
// the rollup label falls back to "mixed" when the buckets don't
// reconcile cleanly, which is the right operator-facing answer for
// an unrecognised state anyway.
func groupStatusCounts(groups []api.ConnectorReviewGroup) (staged, enabled, disabled int) {
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
