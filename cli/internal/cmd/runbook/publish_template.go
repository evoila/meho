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

// newPublishTemplateCmd returns the `meho runbook publish-template`
// command.
//
// CLI shape (per issue #1318):
//
//	meho runbook publish-template <slug> --version N [--json]
//	  [--backplane URL]
//
// Wraps POST /api/v1/runbooks/templates/{slug}/publish. Role:
// tenant_admin.
//
// Flip a draft template to published. Idempotent on
// already-published versions. After publish, the template becomes
// the latest start target for `meho runbook start` (G12.5-T2 #1319).
// Previous published versions stay addressable for in-flight runs
// (pinned at start time) and for `show-template`.
//
// Exit codes:
//   - 0   published successfully (200) or already-published (no-op)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404 slug_not_found, 400 on
//     attempting to publish a deprecated version)
//   - 5   insufficient_role
func newPublishTemplateCmd() *cobra.Command {
	var (
		version           int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "publish-template <slug>",
		Short: "Flip a draft template to published (tenant_admin)",
		Long: "publish-template calls POST " +
			"/api/v1/runbooks/templates/{slug}/publish. Tenant_admin only.\n\n" +
			"--version pins the version to publish (the version returned " +
			"by the draft-template / edit-template call you want to flip). " +
			"Idempotent on already-published versions: a second publish " +
			"against the same (slug, version) returns 200 with no state " +
			"change.\n\n" +
			"After publish, the template becomes the latest start target " +
			"for `meho runbook start` (G12.5-T2). Previous published " +
			"versions stay addressable for in-flight runs (pinned at " +
			"start time) and for `show-template`.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPublishTemplate(cmd, publishTemplateOptions{
				Slug:              args[0],
				Version:           version,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&version, "version", 0,
		"template version to publish (required; the value returned by draft-template / edit-template)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw PublishTemplateResponse JSON instead of the human confirmation")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type publishTemplateOptions struct {
	Slug              string
	Version           int
	JSONOut           bool
	BackplaneOverride string
}

func runPublishTemplate(cmd *cobra.Command, opts publishTemplateOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("publish-template requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	if opts.Version <= 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("publish-template requires --version N (positive integer)"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := postPublishTemplate(cmd.Context(), backplaneURL, opts.Slug, opts.Version)
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
				"call %s: HTTP 200 without a PublishTemplateResponse payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printPublishSummary(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func postPublishTemplate(
	ctx context.Context,
	backplaneURL, slug string,
	version int,
) (*api.PublishTemplateApiV1RunbooksTemplatesSlugPublishPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := api.UnderscoreVersionBody{Version: version}
	params := &api.PublishTemplateApiV1RunbooksTemplatesSlugPublishPostParams{}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.PublishTemplateApiV1RunbooksTemplatesSlugPublishPostResponse, error) {
			return authed.PublishTemplateApiV1RunbooksTemplatesSlugPublishPostWithResponse(
				ctx, slug, params, body,
			)
		},
		func(r *api.PublishTemplateApiV1RunbooksTemplatesSlugPublishPostResponse) int {
			return r.StatusCode()
		},
	)
}

func printPublishSummary(w io.Writer, r *api.PublishTemplateResponse) {
	if r == nil {
		return
	}
	fmt.Fprintf(w, "Published %s@%d (status=%s)\n", r.Slug, r.Version, r.Status)
}
