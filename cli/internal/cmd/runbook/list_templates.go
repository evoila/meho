// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListTemplatesCmd returns the `meho runbook list-templates`
// command.
//
// CLI shape (per issue #1318):
//
//	meho runbook list-templates \
//	  [--status published|draft|deprecated] \
//	  [--target-kind KIND] \
//	  [--limit N] \
//	  [--json] \
//	  [--backplane URL]
//
// Wraps GET /api/v1/runbooks/templates. Role: operator (the backend
// projects the list to a 6-column summary; full step bodies require
// show-template's tighter role gate).
//
// Default output: 5-column table (SLUG, VERSION, STATUS,
// TARGET_KIND, EDITED_AT). `--json` emits the raw
// RunbookTemplateListResponse envelope for jq pipelines.
//
// Exit codes:
//   - 0   list returned cleanly (including zero rows)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response
//   - 5   insufficient_role
func newListTemplatesCmd() *cobra.Command {
	var (
		statusFilter      string
		targetKind        string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list-templates",
		Short: "List runbook templates in your tenant",
		Long: "list-templates calls GET /api/v1/runbooks/templates and " +
			"renders the runbook templates registered in the operator's " +
			"tenant, slug-sorted. Filters: --status (draft / published / " +
			"deprecated), --target-kind (free-form connector kind like " +
			"`vmware-rest`). --limit caps the page size (server-side " +
			"default 100, max 500). Does NOT return step bodies — use " +
			"`meho runbook show-template` (tenant_admin) to read full " +
			"step content.\n\n" +
			"Operators see this surface to discover available procedures " +
			"before `meho runbook start` (G12.5-T2 #1319). " +
			"Tenant_admins see the same to audit what's in flight.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runListTemplates(cmd, listTemplatesOptions{
				Status:            statusFilter,
				TargetKind:        targetKind,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&statusFilter, "status", "",
		"filter by lifecycle status: draft, published, or deprecated")
	cmd.Flags().StringVar(&targetKind, "target-kind", "",
		"filter by target_kind (free-form connector kind like `vmware-rest`)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max templates per page (1..500, server default 100 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw RunbookTemplateListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listTemplatesOptions struct {
	Status            string
	TargetKind        string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runListTemplates(cmd *cobra.Command, opts listTemplatesOptions) error {
	// Fail fast on out-of-range --limit. The backend's Query(le=500)
	// would otherwise surface a 422 on a negative or oversized value;
	// fast-fail saves the network round-trip.
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	// Validate the status enum locally so a typo (`--status published `
	// with a trailing space, `--status pub`) lands as a CLI error
	// rather than a 422. The three allowlisted values are pinned to
	// the backend's `Literal["draft", "published", "deprecated"]`.
	if opts.Status != "" {
		switch opts.Status {
		case "draft", "published", "deprecated":
			// ok
		default:
			return output.RenderError(
				cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"--status must be one of draft, published, deprecated; got %q",
					opts.Status,
				)),
				opts.JSONOut,
			)
		}
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getTemplateList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a template list payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printTemplateListTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listTemplatesParams maps the CLI flags onto the generated query-param
// shape. Each pointer field is set only when the operator supplied the
// flag so the backplane's own defaults apply for unset values.
func listTemplatesParams(opts listTemplatesOptions) *api.ListTemplatesApiV1RunbooksTemplatesGetParams {
	params := &api.ListTemplatesApiV1RunbooksTemplatesGetParams{}
	if opts.Status != "" {
		s := api.ListTemplatesApiV1RunbooksTemplatesGetParamsStatus(opts.Status)
		params.Status = &s
	}
	if opts.TargetKind != "" {
		tk := opts.TargetKind
		params.TargetKind = &tk
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	return params
}

func getTemplateList(
	ctx context.Context,
	backplaneURL string,
	opts listTemplatesOptions,
) (*api.ListTemplatesApiV1RunbooksTemplatesGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listTemplatesParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListTemplatesApiV1RunbooksTemplatesGetResponse, error) {
			return authed.ListTemplatesApiV1RunbooksTemplatesGetWithResponse(ctx, params)
		},
		func(r *api.ListTemplatesApiV1RunbooksTemplatesGetResponse) int { return r.StatusCode() },
	)
}

// printTemplateListTable renders the list as a compact, scannable
// 5-column table — SLUG, VERSION, STATUS, TARGET_KIND, EDITED_AT.
// Matches the existing CLI table conventions (`meho kb list`,
// `meho conventions list`). The full ISO-8601 timestamp is kept
// verbatim (not truncated) because operators correlating with audit
// rows want the precise edited_at; the column is sized for the
// worst-case Go `time.Time`-formatted shape (RFC3339 with nanos).
func printTemplateListTable(w io.Writer, r *api.RunbookTemplateListResponse) {
	if r == nil || len(r.Templates) == 0 {
		fmt.Fprintln(w, "no runbook templates registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-40s %-7s %-10s %-20s %s\n",
		"SLUG", "VERSION", "STATUS", "TARGET_KIND", "EDITED_AT")
	for _, t := range r.Templates {
		targetKind := "-"
		if t.TargetKind != nil && *t.TargetKind != "" {
			targetKind = *t.TargetKind
		}
		fmt.Fprintf(w, "%-40s %-7d %-10s %-20s %s\n",
			truncate(t.Slug, 40),
			t.Version,
			string(t.Status),
			truncate(targetKind, 20),
			t.EditedAt.UTC().Format("2006-01-02T15:04:05Z"),
		)
	}
}
