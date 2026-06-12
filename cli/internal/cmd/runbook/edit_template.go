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

// newEditTemplateCmd returns the `meho runbook edit-template`
// command.
//
// CLI shape (per issue #1318):
//
//	meho runbook edit-template <slug> --from <file.yaml> [--json]
//	  [--backplane URL]
//
// Wraps PATCH /api/v1/runbooks/templates/{slug}. Role: tenant_admin.
//
// Edit semantics depend on the template's current status (backend
// decides; the CLI just surfaces the response):
//
//   - If a draft exists for this slug → edit IN PLACE; no version
//     bump; forked_from is nil in the response.
//   - If only published/deprecated versions exist → FORK to a new
//     draft at version=max+1; forked_from carries the source
//     coordinates plus the in-flight run count pinned to the
//     version being forked from.
//
// The --from YAML file is parsed locally and pre-flighted with the
// same allowlist as draft-template before the PATCH is issued.
//
// Exit codes:
//   - 0   draft edited (200)
//   - 1   YAML parse / pre-flight failure (no HTTP call made)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404 slug_not_found, 422
//     validation failure)
//   - 5   insufficient_role
func newEditTemplateCmd() *cobra.Command {
	var (
		fromPath          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "edit-template <slug>",
		Short: "Edit a draft template — in-place or fork-on-publish (tenant_admin)",
		Long: "edit-template calls PATCH /api/v1/runbooks/templates/{slug}. " +
			"Tenant_admin only.\n\n" +
			"If a draft exists for this slug, the edit lands in place " +
			"(no version bump). If only published/deprecated versions " +
			"exist, the edit FORKS to a new draft at version=max+1; the " +
			"response carries `forked_from` so you can see how many " +
			"in-flight runs are still pinned to the version you're " +
			"forking from (a non-zero count means juniors are mid-procedure " +
			"on the older version — they stay pinned).\n\n" +
			"MULTI-SESSION DRAFTING: a senior+agent walk-through of a " +
			"real cert-rotation runbook is rarely a single session. " +
			"Start with draft-template, edit-template repeatedly across " +
			"sessions, then publish-template once the senior signs off. " +
			"Drafts are mutable across sessions; published versions are " +
			"pinned for in-flight runs.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEditTemplate(cmd, editTemplateOptions{
				Slug:              args[0],
				FromPath:          fromPath,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&fromPath, "from", "",
		"path to the YAML file describing the template body (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw EditTemplateResponse JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type editTemplateOptions struct {
	Slug              string
	FromPath          string
	JSONOut           bool
	BackplaneOverride string
}

func runEditTemplate(cmd *cobra.Command, opts editTemplateOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("edit-template requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	if opts.FromPath == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("edit-template requires --from <file.yaml>"),
			opts.JSONOut,
		)
	}
	yamlBody, err := loadYAMLTemplate(opts.FromPath)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(err.Error()),
			opts.JSONOut,
		)
	}
	if err := validateYAMLTemplate(opts.Slug, yamlBody); err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(err.Error()),
			opts.JSONOut,
		)
	}
	wireBody, err := buildRunbookTemplateBody(yamlBody)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(err.Error()),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := patchEditTemplate(cmd.Context(), backplaneURL, opts.Slug, wireBody)
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
				"call %s: HTTP 200 without an EditTemplateResponse payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printEditSummary(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func patchEditTemplate(
	ctx context.Context,
	backplaneURL, slug string,
	body api.RunbookTemplateBody,
) (*api.EditTemplateApiV1RunbooksTemplatesSlugPatchResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.EditTemplateApiV1RunbooksTemplatesSlugPatchParams{}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EditTemplateApiV1RunbooksTemplatesSlugPatchResponse, error) {
			return authed.EditTemplateApiV1RunbooksTemplatesSlugPatchWithResponse(ctx, slug, params, body)
		},
		func(r *api.EditTemplateApiV1RunbooksTemplatesSlugPatchResponse) int { return r.StatusCode() },
	)
}

// printEditSummary renders the human summary line(s) for an edit
// response. The shape depends on whether the edit was an in-place
// mutation of an existing draft or a fork from a published version:
//
//   - draft-edit (forked_from == nil): one line —
//     `Edited slug@version (status=draft)`.
//   - fork-on-edit (forked_from != nil): two lines —
//     `Edited slug@version (forked from slug@prev, N in-flight runs
//     on previous version)`. Surfaces in_flight_run_count so the
//     senior sees how many juniors are still pinned to the older
//     version (the fork warning Initiative #1200 calls out).
func printEditSummary(w io.Writer, r *api.EditTemplateResponse) {
	if r == nil {
		return
	}
	if r.ForkedFrom == nil {
		fmt.Fprintf(w, "Edited %s@%d (status=%s)\n", r.Slug, r.Version, r.Status)
		return
	}
	fmt.Fprintf(w,
		"Edited %s@%d (forked from %s@%d, %d in-flight run(s) on previous version, status=%s)\n",
		r.Slug, r.Version,
		r.ForkedFrom.Slug, r.ForkedFrom.Version,
		r.ForkedFrom.InFlightRunCount,
		r.Status,
	)
}
