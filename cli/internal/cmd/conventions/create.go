// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCreateCmd returns the `meho conventions create` command.
//
//	meho conventions create \
//	  --slug S --kind K --title T --body @file|@-|<inline-text> \
//	  [--priority N] [--json] [--backplane <url>]
//
// Role: tenant_admin. Operator-role JWT lands as 403 insufficient_role.
//
// A duplicate (tenant, slug) returns 409 with detail
// `convention_already_exists`. An over-budget `operational` body
// returns 422 with the estimated-and-budget detail surfaced verbatim.
//
// Exit codes:
//   - 0   convention created (201)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 409 duplicate, 422 invalid /
//     over-budget)
//   - 5   insufficient_role
func newCreateCmd() *cobra.Command {
	var (
		slug              string
		kind              string
		title             string
		body              string
		priority          int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create one convention (tenant_admin)",
		Long: "create calls POST /api/v1/conventions to create one " +
			"convention under the operator's tenant. Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"--slug is the operator-visible identifier (lowercase ASCII, " +
			"digits, hyphen; max 128 chars; enforced server-side). " +
			"--kind is one of operational | workflow | reference; only " +
			"operational conventions are packed into the preamble. " +
			"--title is a short display label. --body accepts inline " +
			"text, @<path> to read a file, or @- to read from stdin; the " +
			"realistic shape is @<path> with a Markdown rule file. " +
			"--priority (default 0) is the ranking key the T4 preamble " +
			"assembler uses to pack highest-priority-first — operational " +
			"conventions with higher priority survive over-budget drops.\n\n" +
			"A duplicate (same tenant + slug) returns 409 with detail " +
			"convention_already_exists. An over-budget operational body " +
			"returns 422 with an `estimated=X, budget=Y` detail so the " +
			"operator can re-size the body precisely.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runCreate(cmd, createOptions{
				Slug:              slug,
				Kind:              kind,
				Title:             title,
				BodyArg:           body,
				Priority:          priority,
				prioritySet:       cmd.Flags().Changed("priority"),
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&slug, "slug", "",
		"operator-visible identifier (lowercase ASCII, digits, hyphen; max 128 chars)")
	cmd.Flags().StringVar(&kind, "kind", "",
		"convention kind: operational | workflow | reference")
	cmd.Flags().StringVar(&title, "title", "",
		"short display label")
	cmd.Flags().StringVar(&body, "body", "",
		"convention body: inline text, @<path> to read a file, or @- to read from stdin")
	cmd.Flags().IntVar(&priority, "priority", 0,
		"ranking key (default 0; range -32768..32767; higher wins on over-budget drops)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw Convention JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("slug")
	_ = cmd.MarkFlagRequired("kind")
	_ = cmd.MarkFlagRequired("title")
	_ = cmd.MarkFlagRequired("body")
	return cmd
}

type createOptions struct {
	Slug              string
	Kind              string
	Title             string
	BodyArg           string
	Priority          int
	prioritySet       bool
	JSONOut           bool
	BackplaneOverride string
}

func runCreate(cmd *cobra.Command, opts createOptions) error {
	if opts.Slug == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("create requires a non-empty --slug"), opts.JSONOut)
	}
	if opts.Title == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("create requires a non-empty --title"), opts.JSONOut)
	}
	if !validKinds[opts.Kind] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--kind must be one of: operational, workflow, reference; got %q",
				opts.Kind,
			)),
			opts.JSONOut)
	}
	// SMALLINT column range (PG); the server would otherwise return a
	// confusing low-level OverflowError-style 500 / 422 mix on a value
	// outside [-32768, 32767]. Fail fast at the CLI.
	if opts.prioritySet && (opts.Priority < -32768 || opts.Priority > 32767) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--priority must be between -32768 and 32767; got %d",
				opts.Priority,
			)),
			opts.JSONOut)
	}
	body, err := loadBodyFlag(cmd, opts.BodyArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	resp, err := postCreate(cmd.Context(), backplaneURL, opts, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode, resp.Body, opts.JSONOut)
	}
	var conv api.Convention
	if err := json.Unmarshal(resp.Body, &conv); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode conventions create response: %v", err)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), conv)
	}
	printCreateSummary(cmd.OutOrStdout(), &conv)
	return nil
}

// buildCreateBody maps the verb's validated options + loaded body onto
// the generated ConventionCreate body. Priority is a *int in the
// generated type (the backend's pydantic model treats null/omitted as
// "use the column's server_default of 0"); we send the pointer only
// when the operator passed --priority so an unset flag leaves the
// field absent on the wire instead of stamping an explicit 0 the
// backend would treat as "operator pinned to 0" vs "operator didn't
// say" — the latter being important if the column default ever moves.
func buildCreateBody(opts createOptions, body string) api.ConventionCreate {
	out := api.ConventionCreate{
		Slug:  opts.Slug,
		Kind:  api.ConventionKind(opts.Kind),
		Title: opts.Title,
		Body:  body,
	}
	if opts.prioritySet {
		p := opts.Priority
		out.Priority = &p
	}
	return out
}

func postCreate(
	ctx context.Context,
	backplaneURL string,
	opts createOptions,
	body string,
) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildCreateBody(opts, body)
	return doRequest(ctx, authed,
		func(ctx context.Context) (*http.Response, error) {
			return authed.CreateConventionApiV1ConventionsPost(
				ctx,
				&api.CreateConventionApiV1ConventionsPostParams{},
				reqBody,
			)
		},
	)
}

// printCreateSummary renders the created convention as a compact
// one-line confirmation plus the round-tripped slug / timestamps /
// body length. Operators who want the full body should chase with
// `meho conventions show <slug>`.
//
// CreatedAt / UpdatedAt arrive as typed time.Time off the generated
// Convention type (were strings on the pre-migration consumer-side
// duplicate); we format them back to the RFC 3339 shape the
// operator-facing summary contract has always used.
func printCreateSummary(w io.Writer, c *api.Convention) {
	if c == nil {
		return
	}
	fmt.Fprintf(w, "created convention %q\n", c.Slug)
	fmt.Fprintf(w, "%-14s %s\n", "id:", c.Id.String())
	fmt.Fprintf(w, "%-14s %s\n", "tenant_id:", c.TenantId.String())
	fmt.Fprintf(w, "%-14s %s\n", "kind:", c.Kind)
	fmt.Fprintf(w, "%-14s %d\n", "priority:", c.Priority)
	fmt.Fprintf(w, "%-14s %s\n", "title:", c.Title)
	fmt.Fprintf(w, "%-14s %s\n", "created_at:", c.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintf(w, "%-14s %s\n", "updated_at:", c.UpdatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintf(w, "%-14s %d bytes\n", "body:", len(c.Body))
}
