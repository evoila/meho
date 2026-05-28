// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

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

// newShowCmd returns the `meho kb show` command.
//
// CLI shape (per issue #418):
//
//	meho kb show <slug> [--json] [--backplane <url>]
//
// Default output: writes the entry's body to stdout verbatim — the
// body is already Markdown by the substrate's contract, so an
// operator can pipe through `glow`, `bat -l md`, etc. for rendering.
// `--json` wraps the full KbEntry shape (id, tenant_id, slug, body,
// metadata, created_at, updated_at).
//
// A 404 from the backend surfaces as "slug_not_found". The
// cross-tenant probe always reads as 404 — the substrate's tenant
// WHERE clause yields zero rows for a slug outside the operator's
// tenant, and the route returns 404 (not 403) so the existence of
// a slug in another tenant never leaks through status-code
// discrimination.
//
// Exit codes:
//   - 0   entry rendered cleanly
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
		Short: "Fetch a kb entry by slug",
		Long: "show calls GET /api/v1/kb/{slug} and writes the full " +
			"entry body to stdout. The body is Markdown by the " +
			"substrate's contract; pipe through a Markdown renderer " +
			"(glow, bat -l md, mdcat, etc.) for prettified output. " +
			"--json wraps the entry in the full KbEntry envelope " +
			"(id, slug, body, metadata, timestamps). A 404 means the " +
			"slug doesn't exist in your tenant (the route deliberately " +
			"conflates cross-tenant probes with genuine absence so " +
			"existence is never leaked across tenant boundaries).",
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
		"emit raw KbEntry JSON instead of the Markdown body")
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
	resp, err := getEntry(cmd.Context(), backplaneURL, opts.Slug)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil
	// (printEntryBody silently no-ops, so the operator would see an
	// empty stdout with exit 0 — phantom success). Mirrors the
	// convention in `cli/internal/cmd/status.go:142`.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a kb entry body",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printEntryBody(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func getEntry(
	ctx context.Context,
	backplaneURL, slug string,
) (*api.ShowKbApiV1KbSlugGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ShowKbApiV1KbSlugGetResponse, error) {
			return authed.ShowKbApiV1KbSlugGetWithResponse(ctx, slug, nil)
		},
		func(r *api.ShowKbApiV1KbSlugGetResponse) int { return r.StatusCode() },
	)
}

// printEntryBody writes the entry's Markdown body to stdout
// verbatim with exactly one trailing newline. The substrate stores
// the body unmodified, so a Markdown file that already ends in `\n`
// (the conventional shape — most editors enforce a trailing LF on
// save) would otherwise produce two newlines when `Fprintln` adds
// its own. Trimming `\r` + `\n` from the right keeps the single-
// trailing-newline contract regardless of whether the operator
// stored their body with or without a trailing LF/CRLF.
func printEntryBody(w io.Writer, e *api.KbEntry) {
	if e == nil {
		return
	}
	fmt.Fprintln(w, strings.TrimRight(e.Body, "\r\n"))
}
