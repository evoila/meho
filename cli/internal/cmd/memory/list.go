// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// NewListCmd returns the top-level `meho list` command (issue
// #424).
//
// CLI shape:
//
//	meho list [--scope SCOPE] [--tag T] [--slug-pattern P]
//	          [--include-expired] [--limit N] [--json] [--backplane <url>]
//
// Calls GET /api/v1/memory and renders the visible memories as a
// text table sorted by (scope, slug). Tenant-scoped server-side
// via the JWT — no surface accepts a tenant id.
//
// The verb is named `list` (no namespace) per the consumer-needs.md
// §G5 ergonomic shape and the explicit issue #424 contract
// (`meho list --scope user`). The collision-note escape valve
// (namespace under `memory` if cobra refuses to register the
// top-level name) does not fire — cobra has no built-in `list` verb.
//
// Role: any authenticated operator including `read_only` (the
// substrate explicitly allows read_only to read tenant/target
// memories; user-scoped rows still filter by user_sub at the
// service layer).
//
// Exit codes:
//   - 0   list returned cleanly (including zero rows)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 422 invalid_scope / limit_out_of_range)
//   - 5   insufficient_role
func NewListCmd() *cobra.Command {
	var (
		scopeFlag         string
		tagFlag           string
		slugPatternFlag   string
		includeExpired    bool
		limitFlag         int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List memories visible to the operator (GET /api/v1/memory)",
		Long: "list calls GET /api/v1/memory and renders the memories " +
			"visible to the operator in their tenant. Tenant-scoping " +
			"is enforced server-side via the JWT; user-scoped rows " +
			"further filter to the operator that wrote them.\n\n" +
			"--scope narrows by one of user|user-tenant|user-target|" +
			"tenant|target (omitted: every scope visible to the " +
			"operator is shown). --tag narrows to memories carrying " +
			"the supplied tag in metadata. --slug-pattern narrows via " +
			"a substring match on the slug. --include-expired surfaces " +
			"memories past their expires_at (omitted: expired entries " +
			"are filtered out per the G5.1 read-side contract; G5.2 " +
			"ships the daily cleanup task that physically removes " +
			"them). --limit caps the page size (1..500, server default " +
			"100).",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				ScopeArg:          scopeFlag,
				TagArg:            tagFlag,
				SlugPatternArg:    slugPatternFlag,
				IncludeExpired:    includeExpired,
				LimitArg:          limitFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&scopeFlag, "scope", "",
		"filter by memory scope: user|user-tenant|user-target|tenant|target")
	cmd.Flags().StringVar(&tagFlag, "tag", "",
		"filter by tag (memories whose metadata.tags contains this string)")
	cmd.Flags().StringVar(&slugPatternFlag, "slug-pattern", "",
		"filter by substring match on slug (forwarded to MemoryService.list_memories)")
	cmd.Flags().BoolVar(&includeExpired, "include-expired", false,
		"include memories past their expires_at (omitted: expired entries filtered out)")
	cmd.Flags().IntVar(&limitFlag, "limit", 0,
		"max memories per page (1..500, server default 100 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}

type listOptions struct {
	ScopeArg          string
	TagArg            string
	SlugPatternArg    string
	IncludeExpired    bool
	LimitArg          int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if opts.ScopeArg != "" {
		scope, err := parseScope(opts.ScopeArg)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(err.Error()), opts.JSONOut)
		}
		// Write the trimmed Scope value back so buildListPath
		// forwards the normalised form. Without this, a padded
		// input like `--scope " user "` passes the preflight (the
		// validScopes lookup trims) and then 422s on the backend
		// when the raw query string reaches FastAPI's enum check.
		// Mirrors the recall.go:191-203 pattern where parseScope's
		// return value is propagated through kindFilter.
		opts.ScopeArg = string(scope)
	}
	// Mirror the kb list helper's bound check: server clamps with
	// Query(ge=1, le=500). Surface the constraint string locally so
	// operators see the bound without a 422 round-trip.
	if opts.LimitArg < 0 || opts.LimitArg > 500 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--limit must be between 1 and 500; got %d", opts.LimitArg)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printListTable(cmd.OutOrStdout(), resp)
	return nil
}

// buildListPath assembles the GET /api/v1/memory query string from
// the per-call options. Exposed for unit tests so the URL
// construction stays unit-checkable without standing up an
// httptest.Server.
func buildListPath(opts listOptions) string {
	q := url.Values{}
	if opts.ScopeArg != "" {
		q.Set("scope", opts.ScopeArg)
	}
	if opts.SlugPatternArg != "" {
		q.Set("slug_pattern", opts.SlugPatternArg)
	}
	if opts.TagArg != "" {
		q.Set("tag", opts.TagArg)
	}
	if opts.IncludeExpired {
		q.Set("include_expired", "true")
	}
	if opts.LimitArg > 0 {
		q.Set("limit", strconv.Itoa(opts.LimitArg))
	}
	path := "/api/v1/memory"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getList(ctx context.Context, backplaneURL string, opts listOptions) (*ListResponse, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out ListResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode list response: %w", err)
	}
	return &out, nil
}

// printListTable renders the list as a compact, scannable table.
// Columns: SCOPE, SLUG, EXPIRES, BODY (preview). The full ISO-8601
// expires_at is kept verbatim because operators correlating with
// audit-log rows want the precise cutoff; "(none)" is rendered for
// the absent case. Body is truncated to 60 chars so a default
// terminal width doesn't wrap.
func printListTable(w io.Writer, r *ListResponse) {
	if r == nil || len(r.Entries) == 0 {
		fmt.Fprintln(w, "no memories visible in this tenant")
		return
	}
	fmt.Fprintf(w, "%-14s %-32s %-32s %s\n",
		"SCOPE", "SLUG", "EXPIRES", "BODY")
	for _, e := range r.Entries {
		fmt.Fprintf(w, "%-14s %-32s %-32s %s\n",
			truncate(string(e.Scope), 14),
			truncate(e.Slug, 32),
			truncate(pluralisePtr(e.ExpiresAt), 32),
			truncate(snippetOf(e.Body), 60),
		)
	}
}
