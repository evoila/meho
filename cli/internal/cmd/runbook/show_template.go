// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newShowTemplateCmd returns the `meho runbook show-template`
// command.
//
// CLI shape (per issue #1318):
//
//	meho runbook show-template <slug> [--version N] [--json]
//	  [--backplane URL]
//
// Wraps GET /api/v1/runbooks/templates/{slug}. Role: tenant_admin
// unconditionally; OPERATOR role passes only when the operator has a
// completed or abandoned run against (slug, version) — the
// post-completion carve-out (G12.3-T4 #1309) is implemented
// backend-side, the CLI just surfaces whatever the backend returns.
//
// Default output: heading-formatted block — title + metadata, then
// description, then a numbered list of steps with verify summary.
// `--json` emits the raw ShowTemplateResponse for round-trip use
// (the body shape mirrors the YAML import format closely enough that
// a senior can copy the JSON, edit, and pipe back through `meho
// runbook edit-template --from /dev/stdin` after YAML conversion).
//
// Exit codes:
//   - 0   template rendered cleanly
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404 slug_not_found)
//   - 5   insufficient_role (incl. opacity_floor — runbook in flight,
//     post-completion carve-out not yet satisfied)
func newShowTemplateCmd() *cobra.Command {
	var (
		version           int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show-template <slug>",
		Short: "Read the full body of a runbook template, including step contents",
		Long: "show-template calls GET /api/v1/runbooks/templates/{slug} " +
			"and writes the full template (title, description, ordered " +
			"steps with verify summary) to stdout. --version pins to a " +
			"specific version; omitted means the latest non-deprecated " +
			"version the operator can see.\n\n" +
			"ROLE GATE: tenant_admin unconditionally; operator only with " +
			"the post-completion carve-out — once the operator has a " +
			"completed or abandoned run against (slug, version), they " +
			"can read the template for post-mortem / learning. While a " +
			"run is in flight against this slug the 403 still holds " +
			"(opacity_floor; the operator stays scoped to `meho runbook " +
			"next` step-by-step rendering at run time).\n\n" +
			"--json emits the raw ShowTemplateResponse envelope so a " +
			"senior can round-trip the template through their editor.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShowTemplate(cmd, showTemplateOptions{
				Slug:              args[0],
				Version:           version,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&version, "version", 0,
		"pin to a specific template version (default: latest non-deprecated)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ShowTemplateResponse JSON instead of the human-readable block")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type showTemplateOptions struct {
	Slug              string
	Version           int
	JSONOut           bool
	BackplaneOverride string
}

func runShowTemplate(cmd *cobra.Command, opts showTemplateOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("show-template requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	if opts.Version < 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--version must be non-negative; got %d", opts.Version)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getTemplate(cmd.Context(), backplaneURL, opts)
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
				"call %s: HTTP 200 without a template payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	if err := printTemplateBlock(cmd.OutOrStdout(), resp.JSON200); err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("render template: %v", err)),
			opts.JSONOut,
		)
	}
	return nil
}

func getTemplate(
	ctx context.Context,
	backplaneURL string,
	opts showTemplateOptions,
) (*api.ShowTemplateApiV1RunbooksTemplatesSlugGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.ShowTemplateApiV1RunbooksTemplatesSlugGetParams{}
	if opts.Version > 0 {
		v := opts.Version
		params.Version = &v
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ShowTemplateApiV1RunbooksTemplatesSlugGetResponse, error) {
			return authed.ShowTemplateApiV1RunbooksTemplatesSlugGetWithResponse(ctx, opts.Slug, params)
		},
		func(r *api.ShowTemplateApiV1RunbooksTemplatesSlugGetResponse) int { return r.StatusCode() },
	)
}

// printTemplateBlock renders the template as a heading-formatted
// human-readable block. The format is operator-readable (not a
// substitute for the JSON round-trip): heading line, metadata
// key-value pairs, then a numbered step list with verify summary.
//
// Returns an error only when the union-typed step body fails to
// decode (a malformed wire response — the generated client would
// already have rejected this at parse time, so practically
// unreachable). Splitting the error path out keeps the renderer
// honest about edge cases instead of silently falling back to a
// blank line.
func printTemplateBlock(w io.Writer, r *api.ShowTemplateResponse) error {
	if r == nil {
		return nil
	}
	fmt.Fprintf(w, "Template: %s@%d\n", r.Slug, r.Version)
	fmt.Fprintf(w, "Title:       %s\n", r.Title)
	fmt.Fprintf(w, "Status:      %s\n", string(r.Status))
	targetKind := "-"
	if r.TargetKind != nil && *r.TargetKind != "" {
		targetKind = *r.TargetKind
	}
	fmt.Fprintf(w, "Target kind: %s\n", targetKind)
	fmt.Fprintf(w, "Created by:  %s (%s)\n",
		r.CreatedBy, r.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintf(w, "Edited by:   %s (%s)\n",
		r.EditedBy, r.EditedAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintln(w)
	fmt.Fprintln(w, "Description:")
	for _, line := range strings.Split(strings.TrimRight(r.Description, "\n"), "\n") {
		fmt.Fprintf(w, "  %s\n", line)
	}
	fmt.Fprintln(w)
	fmt.Fprintf(w, "Steps (%d):\n", len(r.Steps))
	for i, item := range r.Steps {
		if err := printStep(w, i+1, item); err != nil {
			return err
		}
	}
	return nil
}

// printStep renders a single step's title, type, body, and verify
// summary. The step body is rendered as a Markdown-style indented
// block; the verify summary is a one-liner showing the verify type
// plus its discriminating field (prompt for confirm, op_id for
// operation_call).
//
// Uses the generated union helpers (Discriminator + As*Step) to route
// on the step type. An unknown discriminator returns an error rather
// than rendering an empty step — practically unreachable given the
// typed parser would have already failed, but worth surfacing rather
// than silently dropping a step.
func printStep(w io.Writer, n int, item api.ShowTemplateResponse_Steps_Item) error {
	discriminator, err := item.Discriminator()
	if err != nil {
		return fmt.Errorf("read step %d discriminator: %w", n, err)
	}
	switch discriminator {
	case "manual":
		step, err := item.AsManualStep()
		if err != nil {
			return fmt.Errorf("step %d: decode manual step: %w", n, err)
		}
		fmt.Fprintf(w, "  %d. [manual] %s (id: %s)\n", n, step.Title, step.Id)
		printIndentedBody(w, step.Body)
		printManualVerify(w, step.Verify)
	case "operation_call":
		step, err := item.AsOperationCallStep()
		if err != nil {
			return fmt.Errorf("step %d: decode operation_call step: %w", n, err)
		}
		fmt.Fprintf(w, "  %d. [operation_call] %s (id: %s, op_id: %s)\n",
			n, step.Title, step.Id, step.OpId)
		printIndentedBody(w, step.Body)
		printOpCallVerify(w, step.Verify)
	default:
		return fmt.Errorf("step %d: unknown step type %q", n, discriminator)
	}
	return nil
}

// printIndentedBody renders the step body indented by 6 spaces (4 for
// step number indent, 2 for body indent), preserving embedded
// newlines. Trailing newlines are stripped.
func printIndentedBody(w io.Writer, body string) {
	if body == "" {
		return
	}
	for _, line := range strings.Split(strings.TrimRight(body, "\n"), "\n") {
		fmt.Fprintf(w, "      %s\n", line)
	}
}

// printManualVerify renders a manual step's verify gate as a
// one-liner. confirm verify shows the prompt; operation_call verify
// shows the op_id.
func printManualVerify(w io.Writer, v api.ManualStep_Verify) {
	discriminator, err := v.Discriminator()
	if err != nil {
		fmt.Fprintf(w, "      verify: (unreadable: %v)\n", err)
		return
	}
	switch discriminator {
	case "confirm":
		c, _ := v.AsConfirmVerify()
		fmt.Fprintf(w, "      verify: confirm — %s\n", truncate(c.Prompt, 80))
	case "operation_call":
		c, _ := v.AsOperationCallVerify()
		fmt.Fprintf(w, "      verify: operation_call op_id=%s\n", c.OpId)
	default:
		fmt.Fprintf(w, "      verify: (unknown type %q)\n", discriminator)
	}
}

// printOpCallVerify is the operation_call-step-flavour analogue.
// Identical to printManualVerify except for the input type — the
// generated client defines `ManualStep_Verify` and
// `OperationCallStep_Verify` as distinct types even though they hold
// the same union shape.
func printOpCallVerify(w io.Writer, v api.OperationCallStep_Verify) {
	discriminator, err := v.Discriminator()
	if err != nil {
		fmt.Fprintf(w, "      verify: (unreadable: %v)\n", err)
		return
	}
	switch discriminator {
	case "confirm":
		c, _ := v.AsConfirmVerify()
		fmt.Fprintf(w, "      verify: confirm — %s\n", truncate(c.Prompt, 80))
	case "operation_call":
		c, _ := v.AsOperationCallVerify()
		fmt.Fprintf(w, "      verify: operation_call op_id=%s\n", c.OpId)
	default:
		fmt.Fprintf(w, "      verify: (unknown type %q)\n", discriminator)
	}
}
