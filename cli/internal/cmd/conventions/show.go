// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
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
	resp, err := getShow(cmd.Context(), backplaneURL, opts.Slug)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode, resp.Body, opts.JSONOut)
	}
	var conv api.Convention
	if err := json.Unmarshal(resp.Body, &conv); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode conventions show response: %v", err)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), conv)
	}
	printConventionBody(cmd.OutOrStdout(), &conv)
	return nil
}

// getShow runs the typed Show call against the generated client with
// the standard 401-refresh retry around it.
func getShow(
	ctx context.Context,
	backplaneURL, slug string,
) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return doRequest(ctx, authed,
		func(ctx context.Context) (*http.Response, error) {
			return authed.ShowConventionApiV1ConventionsSlugGet(
				ctx, slug, &api.ShowConventionApiV1ConventionsSlugGetParams{},
			)
		},
	)
}

// getConvention is the success-path convenience wrapper for callers
// that just want the decoded Convention (or an error categorised by
// the standard ladder). Used by the edit verb's $EDITOR-mode
// pre-fetch; the verb separately classifies non-2xx via
// renderHTTPStatus on the wrapping run handler so a 404 surfaces with
// the backend's `convention_not_found` detail.
func getConvention(
	ctx context.Context,
	backplaneURL, slug string,
) (*api.Convention, error) {
	resp, err := getShow(ctx, backplaneURL, slug)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &showHTTPError{
			StatusCode: resp.StatusCode,
			Body:       resp.Body,
		}
	}
	var conv api.Convention
	if err := json.Unmarshal(resp.Body, &conv); err != nil {
		return nil, fmt.Errorf("decode conventions show response: %w", err)
	}
	return &conv, nil
}

// showHTTPError wraps a non-2xx response from the show endpoint so
// the edit verb's pre-fetch can route it back through renderHTTPStatus
// once it reaches the runEdit caller (the categorisation matches
// pre-migration behaviour: 404 on the show fetch surfaces as
// convention_not_found, not as a generic "couldn't fetch" message).
type showHTTPError struct {
	StatusCode int
	Body       []byte
}

func (e *showHTTPError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, strings.TrimSpace(string(e.Body)))
}

// printConventionBody writes the convention's Markdown body to stdout
// verbatim with exactly one trailing newline. The substrate stores the
// body unmodified, so a body that already ends in `\n` (the
// conventional shape — most editors enforce a trailing LF on save)
// would otherwise produce two newlines when Fprintln adds its own.
// Trimming `\r` + `\n` from the right keeps the single-trailing-
// newline contract regardless of whether the operator stored their
// body with or without a trailing LF/CRLF.
func printConventionBody(w io.Writer, c *api.Convention) {
	if c == nil {
		return
	}
	fmt.Fprintln(w, strings.TrimRight(c.Body, "\r\n"))
}
