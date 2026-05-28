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
//     — hybrid retrieval via POST /api/v1/retrieve with
//     source="memory" and optional kind=memory-<scope>. The substrate
//     post-filters user-scoped hits against operator.sub so a search
//     never surfaces another operator's user-scoped row.
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
		Short: "Fetch a memory by natural key or via retrieval (GET /api/v1/memory or /api/v1/retrieve)",
		Long: "recall has two modes. The positional form fetches one " +
			"memory by its <scope>/<slug> natural key via " +
			"GET /api/v1/memory/{scope}/{slug}; the body is written to " +
			"stdout (memory bodies are stored verbatim, so pipe through " +
			"`glow` or `bat -l md` if you want them rendered). The " +
			"query form (--query \"search terms\") runs hybrid " +
			"BM25 + cosine retrieval over the operator's visible " +
			"memories via POST /api/v1/retrieve, pinning " +
			"source=\"memory\" so only memory rows are ranked.\n\n" +
			"--scope narrows the query-mode search to one scope " +
			"(translated to kind=memory-<scope> server-side); without " +
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
		"emit raw MemoryEntry / RetrieveResponse JSON instead of human output")
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
	var kindFilter string
	if opts.ScopeFilterArg != "" {
		scope, err := parseScope(opts.ScopeFilterArg)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(err.Error()), opts.JSONOut)
		}
		// kind_for_scope translation matches
		// `backend/src/meho_backplane/memory/schemas.py::kind_for_scope`:
		// `f"memory-{scope.value}"`. Inlined here (no shared package)
		// because the CLI is a thin caller and a function would be
		// noise.
		kindFilter = "memory-" + string(scope)
	}
	resp, err := postRecallQuery(cmd.Context(), backplaneURL, opts, kindFilter)
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
				"call %s: HTTP 200 without a retrieve response payload",
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
// /api/v1/retrieve. `Source` is pinned to "memory" so the substrate
// scopes hits to memory-entry rows. `Kind` is set only when the
// operator narrowed via --scope (translated to `memory-<scope>`).
// `Limit` flows onto the wire only when the operator passed a
// positive value; the generated field tag is `omitempty`, so a nil
// pointer keeps the JSON key absent and the backend's
// `Field(ge=1, le=50, default=10)` applies.
func buildRecallQueryBody(opts recallOptions, kindFilter string) api.RetrieveRequest {
	src := "memory"
	body := api.RetrieveRequest{
		Query:  opts.QueryArg,
		Source: &src,
	}
	if kindFilter != "" {
		k := kindFilter
		body.Kind = &k
	}
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
	kindFilter string,
) (*api.RetrieveEndpointApiV1RetrievePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildRecallQueryBody(opts, kindFilter)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RetrieveEndpointApiV1RetrievePostResponse, error) {
			return authed.RetrieveEndpointApiV1RetrievePostWithResponse(
				ctx,
				&api.RetrieveEndpointApiV1RetrievePostParams{},
				reqBody,
			)
		},
		func(r *api.RetrieveEndpointApiV1RetrievePostResponse) int { return r.StatusCode() },
	)
}

// printRecallTable renders the ranked hits from --query mode as a
// compact table. Columns: RANK (1-based), SCORE (fused), SCOPE
// (lifted from the hit's `kind` by stripping the `memory-` prefix),
// SLUG (the substrate's `source_id`), SNIPPET (200-char excerpt).
// The fused / per-signal scores are visible in --json output for
// retrieval-tuning sessions.
func printRecallTable(w io.Writer, r *api.RetrieveResponse) {
	if r == nil || len(r.Hits) == 0 {
		fmt.Fprintln(w, "no memory hits for this query")
		return
	}
	fmt.Fprintf(w, "%-5s %-8s %-14s %-40s %s\n",
		"RANK", "SCORE", "SCOPE", "SLUG", "SNIPPET")
	for i, hit := range r.Hits {
		fmt.Fprintf(w, "%-5d %-8.4f %-14s %-40s %s\n",
			i+1,
			hit.FusedScore,
			scopeFromKind(hit.Kind),
			truncate(slugFromSourceID(hit.SourceId), 40),
			truncate(snippetOf(hit.Body), 80),
		)
	}
	if r.QueryDurationMs > 0 {
		fmt.Fprintf(w, "queried in %.2f ms\n", r.QueryDurationMs)
	}
}

// scopeFromKind reverses the `memory-<scope>` prefix the backend
// stores. Mirrors
// `backend/src/meho_backplane/memory/schemas.py::scope_for_kind` —
// inlined for the table renderer because the CLI doesn't need a
// typed Scope here (it's only printed). Returns the raw `kind` on
// shapes that don't match so an unexpected backend value doesn't
// crash the renderer.
func scopeFromKind(kind string) string {
	const prefix = "memory-"
	if len(kind) > len(prefix) && kind[:len(prefix)] == prefix {
		return kind[len(prefix):]
	}
	return kind
}

// slugFromSourceID extracts the human-friendly slug from the
// substrate's source_id encoding. The backend's
// `meho_backplane/memory/_internal.py::encode_source_id` joins
// natural-key segments with `:`; the slug is always the LAST
// segment. The CLI doesn't need to decode the user_sub / target_name
// segments — those are visible separately in --json output.
func slugFromSourceID(sourceID string) string {
	// rsplit(':', 1) — same shape the backend's slug_from_source_id
	// helper uses. Without the `:` delimiter the source_id IS the
	// slug.
	for i := len(sourceID) - 1; i >= 0; i-- {
		if sourceID[i] == ':' {
			return sourceID[i+1:]
		}
	}
	return sourceID
}

// snippetOf returns the first ~200 chars of body so the table render
// fits a default terminal width. Same shape as `meho kb search`'s
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
