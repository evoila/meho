// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newShowCmd returns the `meho conventions show` command.
//
//	meho conventions show <slug> [--json] [--backplane <url>]
//
// Role: operator. Fetches one convention via
// GET /api/v1/conventions/{slug} and renders the body as Markdown by
// default (pipe through `glow`, `bat -l md`, etc. for prettified
// rendering); --json wraps the full Convention shape.
//
// A 404 (`convention_not_found`) covers both genuine absence and
// cross-tenant probes — the conflation prevents enumerating other
// tenants via status-code differential.
//
// Exit codes:
//   - 0   convention rendered cleanly
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (includes 404 slug-not-found)
//   - 5   insufficient_role
func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <slug>",
		Short: "Fetch one convention by slug",
		Long: "show calls GET /api/v1/conventions/{slug} and renders the " +
			"convention body as Markdown to stdout. Pipe through a Markdown " +
			"renderer (glow, bat -l md, mdcat, etc.) for prettified output. " +
			"--json wraps the convention in the full Convention envelope " +
			"(id, slug, title, body, kind, priority, timestamps). A 404 " +
			"means the slug doesn't exist in your tenant (the route " +
			"conflates cross-tenant probes with genuine absence so existence " +
			"is never leaked).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShow(cmd, showOptions{
				Slug:              args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw Convention JSON instead of the Markdown body")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type showOptions struct {
	Slug              string
	JSONOut           bool
	BackplaneOverride string
}

func runShow(cmd *cobra.Command, opts showOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("show requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	conv, err := getConvention(cmd.Context(), backplaneURL, opts.Slug)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), conv)
	}
	printConventionBody(cmd.OutOrStdout(), conv)
	return nil
}

// buildShowPath assembles the GET path. Exposed for unit tests so URL
// encoding of slugs stays covered (slugs are constrained to lowercase
// ASCII + digits + hyphen server-side, but the escape is defensive).
func buildShowPath(slug string) string {
	return "/api/v1/conventions/" + pathEscape(slug)
}

func getConvention(ctx context.Context, backplaneURL, slug string) (*Convention, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildShowPath(slug), nil)
	if err != nil {
		return nil, err
	}
	var out Convention
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode conventions show response: %w", err)
	}
	return &out, nil
}

// printConventionBody writes the convention's Markdown body to stdout
// verbatim with exactly one trailing newline. The substrate stores the
// body unmodified, so a body that already ends in `\n` (the
// conventional shape — most editors enforce a trailing LF on save)
// would otherwise produce two newlines when Fprintln adds its own.
// Trimming `\r` + `\n` from the right keeps the single-trailing-
// newline contract regardless of whether the operator stored their
// body with or without a trailing LF/CRLF.
func printConventionBody(w io.Writer, c *Convention) {
	if c == nil {
		return
	}
	fmt.Fprintln(w, strings.TrimRight(c.Body, "\r\n"))
}
