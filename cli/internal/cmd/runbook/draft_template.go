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

// newDraftTemplateCmd returns the `meho runbook draft-template`
// command.
//
// CLI shape (per issue #1318):
//
//	meho runbook draft-template <slug> --from <file.yaml> [--json]
//	  [--backplane URL]
//
// Wraps POST /api/v1/runbooks/templates. Role: tenant_admin —
// operator-role JWT lands as 403 insufficient_role.
//
// Use when: a senior is about to walk you through a procedure (cert
// rotation, host onboarding, vault unseal-after-restart) and wants
// it captured as a governance-graded artifact. The verb creates the
// first draft (version=1, status=draft); if a draft already exists
// for this slug the backend rejects with 409 / draft_already_exists
// and the operator should use `edit-template` instead.
//
// The --from YAML file is parsed locally, pre-flight validated
// (slug regex, step id uniqueness + grammar, step / verify type
// allowlists, substitution allowlist), and only then POSTed. The
// backend re-validates authoritatively at the wire so any drift
// between this CLI's pre-flight and the backend's schemas.py is
// caught by a 422.
//
// Exit codes:
//   - 0   draft created (201)
//   - 1   YAML parse / pre-flight failure (no HTTP call made)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 409 draft_already_exists, 422
//     validation failure)
//   - 5   insufficient_role
func newDraftTemplateCmd() *cobra.Command {
	var (
		fromPath          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "draft-template <slug>",
		Short: "Create the first draft of a new runbook template (tenant_admin)",
		Long: "draft-template calls POST /api/v1/runbooks/templates to " +
			"create the first draft of a new slug. Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"--from <file.yaml> is required: the template body is read " +
			"from the YAML file, pre-flighted locally (slug regex, step " +
			"id uniqueness, step / verify type allowlists, substitution " +
			"allowlist), and only then POSTed. Pre-flight is a UX layer " +
			"— the backend re-validates authoritatively.\n\n" +
			"If a draft already exists for the slug, the backend " +
			"refuses (409). Use `meho runbook edit-template` to mutate " +
			"the existing draft.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDraftTemplate(cmd, draftTemplateOptions{
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
		"emit raw DraftTemplateResponse JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type draftTemplateOptions struct {
	Slug              string
	FromPath          string
	JSONOut           bool
	BackplaneOverride string
}

func runDraftTemplate(cmd *cobra.Command, opts draftTemplateOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("draft-template requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	if opts.FromPath == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("draft-template requires --from <file.yaml>"),
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
	resp, err := postDraftTemplate(cmd.Context(), backplaneURL, opts.Slug, wireBody)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON201 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 201 without a DraftTemplateResponse payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON201)
	}
	printDraftSummary(cmd.OutOrStdout(), resp.JSON201)
	return nil
}

func postDraftTemplate(
	ctx context.Context,
	backplaneURL, slug string,
	body api.RunbookTemplateBody,
) (*api.DraftTemplateApiV1RunbooksTemplatesPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	req := api.DraftTemplateRequest{Slug: slug, Body: body}
	params := &api.DraftTemplateApiV1RunbooksTemplatesPostParams{}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.DraftTemplateApiV1RunbooksTemplatesPostResponse, error) {
			return authed.DraftTemplateApiV1RunbooksTemplatesPostWithResponse(ctx, params, req)
		},
		func(r *api.DraftTemplateApiV1RunbooksTemplatesPostResponse) int { return r.StatusCode() },
	)
}

// printDraftSummary renders the 2-line success summary the issue
// body's AC specifies: `Created draft <slug>@<version>` + a status
// line. The version is always 1 for a fresh draft, but the backend
// is the source of truth so we render whatever it returned (rather
// than hardcoding "1").
func printDraftSummary(w io.Writer, r *api.DraftTemplateResponse) {
	if r == nil {
		return
	}
	fmt.Fprintf(w, "Created draft %s@%d\n", r.Slug, r.Version)
	fmt.Fprintf(w, "Status: %s\n", r.Status)
}
