// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

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

// NewRecallCmd returns the top-level `meho recall` command. Two
// modes (issue #424):
//
//   - `meho recall <scope>/<slug> [--target NAME]` — exact-key
//     fetch via GET /api/v1/memory/{scope}/{slug}. The route
//     deliberately conflates "missing" and "no access" into 404 to
//     avoid leaking tenant boundaries via status-code differential;
//     the CLI surfaces the detail string (`memory_not_found`) verbatim.
//   - `meho recall --query "search terms" [--scope SCOPE] [--limit N]`
//     — hybrid retrieval via POST /api/v1/memory/search, the same
//     MemoryService.search_memories call the MCP search_memory tool
//     makes. The server drops memories you may not see and expired
//     memories before ranking, so the CLI and an agent get the same
//     hits for the same input.
//
// The two modes are mutually exclusive — supplying both a positional
// arg and `--query` fails fast client-side. Supplying neither also
// fails fast.
//
// Role: any authenticated operator including `read_only` (the
// substrate explicitly allows read_only to read tenant/target
// memories per consumer-needs.md §G5 L131's "team becomes the unit
// of memory" property; user-scoped rows still filter by user_sub at
// the service layer).
//
// Exit codes:
//   - 0   entry rendered cleanly (positional mode) or hits returned
//     (query mode, including the zero-hit case)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404 memory_not_found,
//     422 invalid_scope)
//   - 5   insufficient_role
func NewRecallCmd() *cobra.Command {
	var (
		queryFlag         string
		scopeFlag         string
		limitFlag         int
		targetFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "recall <scope>/<slug>",
		Short: "Fetch a memory by natural key or search memories (GET /api/v1/memory or POST /api/v1/memory/search)",
		Long: "recall has two modes. The positional form fetches one " +
			"memory by its <scope>/<slug> natural key via " +
			"GET /api/v1/memory/{scope}/{slug}; the body is written to " +
			"stdout (memory bodies are stored verbatim, so pipe through " +
			"`glow` or `bat -l md` if you want them rendered). The " +
			"query form (--query \"search terms\") runs hybrid " +
			"BM25 + cosine retrieval over the operator's visible " +
			"memories via POST /api/v1/memory/search. Expired memories " +
			"and memories you may not see are left out before ranking.\n\n" +
			"--scope narrows the query-mode search to one scope; without " +
			"it, every scope visible to the operator is considered. " +
			"--limit caps the result count (1..50, server default 10 " +
			"when omitted). --target is only consulted in positional " +
			"mode for user-target / target scopes; without it, those " +
			"scopes surface as 404 by the route's info-leak avoidance " +
			"(the substrate can't tell which target the operator meant).\n\n" +
			"A 404 in positional mode means the slug doesn't exist in " +
			"your tenant (the route deliberately conflates cross-tenant " +
			"probes with genuine absence so existence is never leaked " +
			"across tenant boundaries).",
		// MaximumNArgs(1) — positional arg is optional when --query
		// is set; the runner enforces "exactly one of (positional,
		// --query)" with a clearer message than cobra's default.
		Args:          cobra.MaximumNArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			arg := ""
			if len(args) == 1 {
				arg = args[0]
			}
			return runRecall(cmd, recallOptions{
				ScopeSlugArg:      arg,
				QueryArg:          queryFlag,
				ScopeFilterArg:    scopeFlag,
				LimitArg:          limitFlag,
				LimitChanged:      cmd.Flags().Changed("limit"),
				TargetArg:         targetFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&queryFlag, "query", "",
		"run hybrid retrieval against memories with this query; "+
			"mutually exclusive with the <scope>/<slug> positional")
	cmd.Flags().StringVar(&scopeFlag, "scope", "",
		"narrow --query mode to one scope: user|user-tenant|user-target|tenant|target")
	cmd.Flags().IntVar(&limitFlag, "limit", 0,
		"max hits to return in --query mode (1..50, server default 10 when omitted)")
	cmd.Flags().StringVar(&targetFlag, "target", "",
		"target name for user-target / target scopes (positional mode only)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw MemoryEntry / MemorySearchResponse JSON instead of human output")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	return cmd
}

type recallOptions struct {
	ScopeSlugArg   string
	QueryArg       string
	ScopeFilterArg string
	LimitArg       int
	// LimitChanged mirrors `cmd.Flags().Changed("limit")` for
	// runRecallByQuery's "operator-supplied 0 is an error;
	// default-0 means 'use the server default'" gate. Threaded as
	// a field rather than re-reading off cmd so tests that drive
	// runRecall directly (bypassing the cobra flag-parse path) can
	// opt in / out of the gate explicitly. Mirrors the kb sibling's
	// searchOptions.Changed shape.
	LimitChanged      bool
	TargetArg         string
	JSONOut           bool
	BackplaneOverride string
}

func runRecall(cmd *cobra.Command, opts recallOptions) error {
	hasArg := opts.ScopeSlugArg != ""
	hasQuery := opts.QueryArg != ""
	if hasArg == hasQuery {
		// True == True (both supplied) or False == False (neither).
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(
				"recall requires exactly one of <scope>/<slug> or --query \"...\""),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			backplane.ClassifyError(err), opts.JSONOut)
	}
	if hasArg {
		return runRecallByKey(cmd, backplaneURL, opts)
	}
	return runRecallByQuery(cmd, backplaneURL, opts)
}

func runRecallByKey(cmd *cobra.Command, backplaneURL string, opts recallOptions) error {
	scope, slug, err := parseScopeSlugArg(opts.ScopeSlugArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	// requireTargetForScope mirrors the AC "scope=target requires
	// --target". The backend would respond 404 (info-leak avoidance)
	// rather than 422; the CLI's pre-flight error is more actionable.
	if err := requireTargetForScope(scope, opts.TargetArg); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	resp, err := getRecall(cmd.Context(), backplaneURL, scope, slug, opts.TargetArg)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil
	// (writeBodyToStdout silently writes one newline, so the operator
	// would see an empty stdout with exit 0 — phantom success).
	// Mirrors `cli/internal/cmd/status.go:142` + the kb sibling's
	// post-iter-2 nil-guard pattern.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a memory entry payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	writeBodyToStdout(cmd.OutOrStdout(), resp.JSON200.Body)
	return nil
}

func runRecallByQuery(cmd *cobra.Command, backplaneURL string, opts recallOptions) error {
	// Mirror the --limit clamp from `meho kb search`: backend
	// rejects with 422 outside 1..50; surface the constraint string
	// locally so operators see the bound without a round-trip.
	if opts.LimitArg < 0 || opts.LimitArg > 50 || (opts.LimitChanged && opts.LimitArg == 0) {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--limit must be between 1 and 50 when provided; got %d", opts.LimitArg)),
			opts.JSONOut,
		)
	}
	var scopeFilter *Scope
	if opts.ScopeFilterArg != "" {
		scope, err := parseScope(opts.ScopeFilterArg)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(err.Error()), opts.JSONOut)
		}
		scopeFilter = &scope
	}
	resp, err := postRecallQuery(cmd.Context(), backplaneURL, opts, scopeFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil
	// (printRecallTable's nil-or-empty branch would print "no hits"
	// — actively misleading on a malformed 200). Mirrors the kb
	// sibling's post-iter-2 nil-guard pattern.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a memory search payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printRecallTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// buildRecallParams maps the positional-mode CLI flags onto the
// generated query-param shape. `TargetName` is set only when the
// operator supplied `--target` so an unset flag stays absent on the
// wire (user / user-tenant / tenant recalls have no target_name
// semantics).
func buildRecallParams(targetName string) *api.RecallApiV1MemoryScopeSlugGetParams {
	params := &api.RecallApiV1MemoryScopeSlugGetParams{}
	if targetName != "" {
		t := targetName
		params.TargetName = &t
	}
	return params
}

func getRecall(
	ctx context.Context,
	backplaneURL string,
	scope Scope,
	slug, targetName string,
) (*api.RecallApiV1MemoryScopeSlugGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := buildRecallParams(targetName)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RecallApiV1MemoryScopeSlugGetResponse, error) {
			return authed.RecallApiV1MemoryScopeSlugGetWithResponse(ctx, scope, slug, params)
		},
		func(r *api.RecallApiV1MemoryScopeSlugGetResponse) int { return r.StatusCode() },
	)
}

// buildRecallQueryBody assembles the typed POST body for
// /api/v1/memory/search. `Scope` is set only when the operator
// narrowed via --scope; without it the server searches every scope
// the operator can see. `Limit` flows onto the wire only when the
// operator passed a positive value; the generated field tag is
// `omitempty`, so a nil pointer keeps the JSON key absent and the
// backend's `Field(ge=1, le=50, default=10)` applies.
func buildRecallQueryBody(opts recallOptions, scope *Scope) api.MemorySearchBody {
	body := api.MemorySearchBody{Query: opts.QueryArg, Scope: scope}
	if opts.LimitArg > 0 {
		limit := opts.LimitArg
		body.Limit = &limit
	}
	return body
}

func postRecallQuery(
	ctx context.Context,
	backplaneURL string,
	opts recallOptions,
	scope *Scope,
) (*api.SearchMemoriesApiV1MemorySearchPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildRecallQueryBody(opts, scope)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.SearchMemoriesApiV1MemorySearchPostResponse, error) {
			return authed.SearchMemoriesApiV1MemorySearchPostWithResponse(
				ctx,
				&api.SearchMemoriesApiV1MemorySearchPostParams{},
				reqBody,
			)
		},
		func(r *api.SearchMemoriesApiV1MemorySearchPostResponse) int { return r.StatusCode() },
	)
}

// printRecallTable renders the ranked hits from --query mode as a
// compact table. Columns: RANK (1-based order, not a confidence
// score), SCOPE, SLUG, SNIPPET. The server sends a short excerpt per
// hit, not the full body; the table cuts it to 80 chars. Read the full
// entry with the positional `<scope>/<slug>` form. Provenance fields,
// expiry state, and the locator are in --json output.
func printRecallTable(w io.Writer, r *api.MemorySearchResponse) {
	if r == nil || len(r.Hits) == 0 {
		fmt.Fprintln(w, "no memory hits for this query")
		return
	}
	fmt.Fprintf(w, "%-5s %-14s %-40s %s\n",
		"RANK", "SCOPE", "SLUG", "SNIPPET")
	for _, hit := range r.Hits {
		fmt.Fprintf(w, "%-5d %-14s %-40s %s\n",
			hit.Rank,
			string(hit.Scope),
			truncate(hit.Slug, 40),
			truncate(hit.Snippet, 80),
		)
	}
}

// snippetOf returns the first ~200 chars of body so the `meho memory
// list` table render fits a default terminal width. Same shape as `meho kb search`'s
// helper of the same name; kept inline here to avoid a cross-package
// import that would close a cycle through cmd/root.go.
func snippetOf(body string) string {
	const snippetChars = 200
	runes := []rune(body)
	if len(runes) <= snippetChars {
		return body
	}
	return string(runes[:snippetChars]) + "…"
}
