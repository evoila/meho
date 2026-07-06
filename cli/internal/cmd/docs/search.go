// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

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

// newSearchCmd returns the `meho docs search` command.
//
// CLI shape (per issues #1552 / #1554):
//
//	meho docs search <query> --collection <c> \
//	  [--product <p>] [--version <v>] [--limit N] [--json] [--backplane <url>]
//	meho docs search <query> --collection <a> --collection <b>   # fan-out
//	meho docs search <query> --collection all                    # fan-out across all
//
// Role: operator. Calls POST /api/v1/search_docs (T3, #1552) via the
// shared authed client (bearer + 401-refresh). There is no client-side
// capability gate (#2109): access is decided server-side by the
// backplane, identically to the REST route.
//
// --collection is the mandatory binary scope: it routes the query to a
// backend and gates per-collection entitlement. The CLI fails fast on a
// missing --collection (exit 4) before the round-trip, mirroring the
// route's 422 rather than incurring it. --product / --version are
// optional refinements within a single collection.
//
// --collection may be repeated for a cross-collection fan-out (#1554): the
// query runs against each named collection and the hits are merged by
// reciprocal-rank fusion server-side. The sentinel `--collection all` fans
// out across every collection the operator is entitled to. A fan-out
// ignores --product / --version (each collection is a pre-scoped corpus).
//
// Exit codes:
//   - 0   search returned cleanly (incl. zero-hit result)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (missing --collection, out-of-range
//     --limit, a 422/503 from the route)
//   - 5   insufficient_role (read_only operator, or a per-collection
//     entitlement miss the backplane 403s on)
func newSearchCmd() *cobra.Command {
	var (
		collections       []string
		product           string
		version           string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Search vendor-document collection(s) (mandatory --collection)",
		Long: "search calls POST /api/v1/search_docs and renders the " +
			"ranked cited chunks as a text table. --collection is " +
			"mandatory: it is the binary scope that routes the query to a " +
			"collection's backend and gates per-collection entitlement (a " +
			"docs query without it is rejected rather than run unscoped). " +
			"Repeat --collection (or pass --collection all) for a " +
			"cross-collection fan-out merged by reciprocal-rank fusion; a " +
			"fan-out ignores --product / --version. " +
			"--product and --version are optional refinements within a " +
			"single collection. The query routes through the backplane so it " +
			"is audited centrally; the raw query is never logged (only " +
			"its hash). --json emits the raw SearchDocsResponse with the " +
			"full DocsChunk shape (chunk id, document id, score, source " +
			"url).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSearch(cmd, searchOptions{
				Query:             args[0],
				Collections:       collections,
				Product:           product,
				Version:           version,
				Limit:             limit,
				Changed:           cmd.Flags().Changed("limit"),
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringArrayVar(&collections, "collection", nil,
		"collection key to search (required; e.g. vmware). Repeat for a "+
			"cross-collection fan-out, or pass 'all' to fan out across every "+
			"entitled collection.")
	cmd.Flags().StringVar(&product, "product", "",
		"optional vendor-product refinement within a single collection")
	cmd.Flags().StringVar(&version, "version", "",
		"optional product-version refinement within a single collection")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max chunks to return (1..50, server default 10 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw SearchDocsResponse JSON (full DocsChunk shape)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type searchOptions struct {
	Query string
	// Collections is the repeatable --collection scope. Zero values is the
	// missing-scope error; one non-"all" value is the single-collection
	// path; the sentinel "all" or two-or-more values is a cross-collection
	// fan-out (#1554).
	Collections []string
	Product     string
	Version     string
	Limit       int
	// Changed mirrors `cobra.Command.Flags().Changed("limit")` for the
	// "operator-supplied 0 is an error; default-0 means 'use the
	// server default'" gate. Threaded as a field rather than re-read
	// off cmd so tests that drive runSearch directly (bypassing the
	// cobra flag-parse path) can opt the gate in / out explicitly.
	Changed           bool
	JSONOut           bool
	BackplaneOverride string
}

func runSearch(cmd *cobra.Command, opts searchOptions) error {
	if opts.Query == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("search requires a non-empty <query> argument"),
			opts.JSONOut,
		)
	}
	// Collection scope, client-side: --collection is the mandatory
	// binary scope. Fail fast with the same constraint the route would
	// 422 on, so operators see it locally without a round-trip.
	// --product / --version are optional refinements (no client gate).
	if len(opts.Collections) == 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("search requires --collection (the mandatory binary scope)"),
			opts.JSONOut,
		)
	}
	// The 'all' sentinel is whole-scope: it cannot be mixed with explicit
	// collection keys (the server would 422 on the ambiguous scope). Fail
	// fast locally with a clearer message than the route's.
	if len(opts.Collections) > 1 && containsAllSentinel(opts.Collections) {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("--collection all cannot be combined with other --collection values"),
			opts.JSONOut,
		)
	}
	// Fail fast on out-of-range --limit. The backend clamps with
	// Field(ge=1, le=50); the CLI mirrors the bound. Zero is cobra's
	// default for an unset IntVar — preserve that as the "no flag"
	// sentinel (buildSearchBody omits the field on zero), but reject
	// an explicit --limit=0 (outside the documented 1..50 range).
	if opts.Limit < 0 || opts.Limit > 50 || (opts.Changed && opts.Limit == 0) {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 50 when provided; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := searchDocs(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil. A
	// malformed 200 must not be conflated with a genuinely-empty
	// result set (which printSearchTable renders as "no docs hits").
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a search_docs response payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printSearchTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// allCollectionsSentinel is the wire value of --collection that fans out
// across every entitled collection (mirrors the backend sentinel).
const allCollectionsSentinel = "all"

// containsAllSentinel reports whether any --collection value is the "all"
// sentinel (used to reject mixing the whole-scope sentinel with keys).
func containsAllSentinel(collections []string) bool {
	for _, c := range collections {
		if c == allCollectionsSentinel {
			return true
		}
	}
	return false
}

// buildSearchBody assembles the typed POST body for /api/v1/search_docs.
// The collection scope is mandatory and non-empty by the time this is
// reached; it maps to the wire body as one of:
//   - a single `collection` (one value, not the "all" sentinel) — the
//     single-collection path, with optional product/version refinements;
//   - `collection: "all"` (the sole value "all") — fan out across every
//     entitled collection;
//   - `collections: [...]` (two or more values) — fan out across the named
//     keys.
//
// product/version are optional refinements that only apply to (and only
// land on the wire for) the single-collection path — a fan-out is a merge
// across pre-scoped corpora, so the backend ignores them there. They land
// via the generated `omitempty` tags so an unset refinement keeps the JSON
// key absent. Limit only lands when the operator passed a positive --limit,
// so the backend's Field(ge=1, le=50, default=10) applies on absence.
func buildSearchBody(opts searchOptions) api.SearchDocsRequest {
	body := api.SearchDocsRequest{Query: opts.Query}
	switch {
	case len(opts.Collections) > 1:
		// Explicit fan-out across the named keys.
		collections := append([]string(nil), opts.Collections...)
		body.Collections = &collections
		// product/version do not apply to a fan-out; leave them unset.
		return finalizeLimit(body, opts)
	case opts.Collections[0] == allCollectionsSentinel:
		// The whole-scope sentinel — fan out across every entitled
		// collection. Sent as `collection: "all"`.
		collection := allCollectionsSentinel
		body.Collection = &collection
		return finalizeLimit(body, opts)
	default:
		// Single collection — the T3 path, with optional refinements.
		collection := opts.Collections[0]
		body.Collection = &collection
	}
	if opts.Product != "" {
		product := opts.Product
		body.Product = &product
	}
	if opts.Version != "" {
		version := opts.Version
		body.Version = &version
	}
	return finalizeLimit(body, opts)
}

// finalizeLimit attaches the optional --limit to *body* (only when a
// positive value was supplied) and returns it, so each scope branch of
// buildSearchBody shares one limit-handling path.
func finalizeLimit(body api.SearchDocsRequest, opts searchOptions) api.SearchDocsRequest {
	if opts.Limit > 0 {
		limit := opts.Limit
		body.Limit = &limit
	}
	return body
}

func searchDocs(
	ctx context.Context,
	backplaneURL string,
	opts searchOptions,
) (*api.SearchDocsEndpointApiV1SearchDocsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildSearchBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.SearchDocsEndpointApiV1SearchDocsPostResponse, error) {
			return authed.SearchDocsEndpointApiV1SearchDocsPostWithResponse(
				ctx,
				&api.SearchDocsEndpointApiV1SearchDocsPostParams{},
				reqBody,
			)
		},
		func(r *api.SearchDocsEndpointApiV1SearchDocsPostResponse) int { return r.StatusCode() },
	)
}

// printSearchTable renders the ranked cited chunks as a compact table.
// Columns: RANK (1-based), SCORE (corpus score; "-" when the corpus
// omitted it), DOCUMENT (the document id citation), SNIPPET (200-char
// excerpt of the chunk content). A cross-collection fan-out (#1554) adds a
// COLLECTION column carrying each chunk's source-collection provenance; it
// is omitted on a single-collection search (where the chunks carry no
// provenance tag), so that output is byte-identical to before. The chunk id
// and source url are available in --json output for citation drill-down;
// rendering them in the table would overflow a default terminal width.
func printSearchTable(w io.Writer, r *api.SearchDocsResponse) {
	if r == nil || len(r.Chunks) == 0 {
		fmt.Fprintln(w, "no docs hits for this query")
		return
	}
	if anyTaggedWithCollection(r.Chunks) {
		printFanoutTable(w, r.Chunks)
		return
	}
	fmt.Fprintf(w, "%-5s %-8s %-40s %s\n", "RANK", "SCORE", "DOCUMENT", "SNIPPET")
	for i, chunk := range r.Chunks {
		fmt.Fprintf(w, "%-5d %-8s %-40s %s\n",
			i+1,
			formatScore(chunk.Score),
			truncate(docID(chunk.DocumentId), 40),
			truncate(snippetOf(chunk.Content), 80),
		)
	}
}

// anyTaggedWithCollection reports whether any chunk carries a source-
// collection provenance tag — the signal that this was a fan-out result.
func anyTaggedWithCollection(chunks []api.DocsChunk) bool {
	for _, chunk := range chunks {
		if chunk.Collection != nil && *chunk.Collection != "" {
			return true
		}
	}
	return false
}

// printFanoutTable renders a fan-out result with the extra COLLECTION
// provenance column so the operator can attribute each rank-fused hit.
func printFanoutTable(w io.Writer, chunks []api.DocsChunk) {
	fmt.Fprintf(w, "%-5s %-16s %-8s %-32s %s\n", "RANK", "COLLECTION", "SCORE", "DOCUMENT", "SNIPPET")
	for i, chunk := range chunks {
		collection := "-"
		if chunk.Collection != nil && *chunk.Collection != "" {
			collection = *chunk.Collection
		}
		fmt.Fprintf(w, "%-5d %-16s %-8s %-32s %s\n",
			i+1,
			truncate(collection, 16),
			formatScore(chunk.Score),
			truncate(docID(chunk.DocumentId), 32),
			truncate(snippetOf(chunk.Content), 80),
		)
	}
}

// docID dereferences the optional document-id citation, which is now a
// *string on the wire (DocsChunk.DocumentId). A nil id renders as a blank
// cell — mirroring the nil-guard used for the optional Collection field —
// so an absent citation stays empty rather than panicking on a nil deref.
func docID(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// formatScore renders the corpus score, which is optional on the wire
// (DocsChunk.Score is a *float32). A nil score renders as "-" so the
// column stays aligned and an absent score isn't misread as 0.0000.
func formatScore(score *float32) string {
	if score == nil {
		return "-"
	}
	return fmt.Sprintf("%.4f", *score)
}

// snippetOf returns the first ~200 chars of content so the table
// render fits a default terminal width. Kept separate from truncate
// (which trims to a final length with an ellipsis) so the snippet
// width is independent of the truncate(maxLen) contract used for the
// other columns.
func snippetOf(content string) string {
	const snippetChars = 200
	runes := []rune(content)
	if len(runes) <= snippetChars {
		return content
	}
	return string(runes[:snippetChars]) + "…"
}
