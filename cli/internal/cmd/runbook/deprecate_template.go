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

// newDeprecateTemplateCmd returns the `meho runbook
// deprecate-template` command.
//
// CLI shape (per issue #1318):
//
//	meho runbook deprecate-template <slug> --version N [--json]
//	  [--backplane URL]
//
// Wraps POST /api/v1/runbooks/templates/{slug}/deprecate. Role:
// tenant_admin.
//
// Mark a published version as deprecated. In-flight runs continue
// to advance (they're pinned), but new `runbook start` calls
// against this version are refused — the backend's runbook_start
// falls back to the latest non-deprecated published version of the
// slug.
//
// Exit codes:
//   - 0   deprecated successfully (200)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404 slug_not_found, 400 on
//     attempting to deprecate a draft / already-deprecated version)
//   - 5   insufficient_role
func newDeprecateTemplateCmd() *cobra.Command {
	var (
		version           int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "deprecate-template <slug>",
		Short: "Mark a published version as deprecated (tenant_admin)",
		Long: "deprecate-template calls POST " +
			"/api/v1/runbooks/templates/{slug}/deprecate. Tenant_admin only.\n\n" +
			"In-flight runs continue to advance (they're pinned), but " +
			"new `meho runbook start` calls against this version are " +
			"refused — the backend falls back to the latest non-deprecated " +
			"published version of the slug.\n\n" +
			"Use when: a procedure is no longer current (cert validity " +
			"period changed, a backend was upgraded) and you want to stop " +
			"new operators starting against it.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDeprecateTemplate(cmd, deprecateTemplateOptions{
				Slug:              args[0],
				Version:           version,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&version, "version", 0,
		"template version to deprecate (required; positive integer)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw DeprecateTemplateResponse JSON instead of the human confirmation")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type deprecateTemplateOptions struct {
	Slug              string
	Version           int
	JSONOut           bool
	BackplaneOverride string
}

func runDeprecateTemplate(cmd *cobra.Command, opts deprecateTemplateOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("deprecate-template requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	if opts.Version <= 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("deprecate-template requires --version N (positive integer)"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postDeprecateTemplate(cmd.Context(), backplaneURL, opts.Slug, opts.Version)
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
				"call %s: HTTP 200 without a DeprecateTemplateResponse payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printDeprecateSummary(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func postDeprecateTemplate(
	ctx context.Context,
	backplaneURL, slug string,
	version int,
) (*api.DeprecateTemplateApiV1RunbooksTemplatesSlugDeprecatePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := api.UnderscoreVersionBody{Version: version}
	params := &api.DeprecateTemplateApiV1RunbooksTemplatesSlugDeprecatePostParams{}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.DeprecateTemplateApiV1RunbooksTemplatesSlugDeprecatePostResponse, error) {
			return authed.DeprecateTemplateApiV1RunbooksTemplatesSlugDeprecatePostWithResponse(
				ctx, slug, params, body,
			)
		},
		func(r *api.DeprecateTemplateApiV1RunbooksTemplatesSlugDeprecatePostResponse) int {
			return r.StatusCode()
		},
	)
}

func printDeprecateSummary(w io.Writer, r *api.DeprecateTemplateResponse) {
	if r == nil {
		return
	}
	fmt.Fprintf(w, "Deprecated %s@%d (status=%s)\n", r.Slug, r.Version, r.Status)
}
