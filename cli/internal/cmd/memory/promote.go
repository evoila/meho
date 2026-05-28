// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// exitIdempotentPromote is the informational exit code surfaced when a
// `meho promote` re-run returns the existing target row instead of
// minting a fresh one. The route returns 200 in both cases (per the
// G5.2-T4 #626 idempotency contract); the CLI distinguishes them by
// comparing the entry's “created_at“ against a pre-POST timestamp.
//
// 6 was the next free slot in the cli/internal/output exit-code ladder
// (2 auth_expired, 3 unreachable, 4 unexpected_response, 5
// insufficient_role). The code is **informational** -- under “--json“
// the verb collapses to exit 0 so scripts piping through “jq“ don't
// trip on a successful no-op. Issue #627 acceptance criterion: "exit 6
// in human mode, exit 0 under --json".
const exitIdempotentPromote = 6

// errCodeIdempotentPromote is the stable machine-readable identifier
// for the idempotent-re-run path. Mirrors the “output.ErrCode*“
// naming convention; appears in the human-mode summary line so
// forensic log scrapers can grep one string across promote runs that
// hit the no-op branch.
const errCodeIdempotentPromote = "already_promoted_at_target_idempotent"

// NewPromoteCmd returns the top-level `meho promote` command (issue
// #627).
//
// CLI shape:
//
//	meho promote <scope>/<slug> --to <target-scope> [--move] [--json] [--backplane <url>]
//
// Calls POST /api/v1/memory/{scope}/{slug}/promote (G5.2-T4 #626).
// Role: any operator who can read the source row AND satisfies the
// per-ladder-step authority gate (own user-scoped row → user-tenant /
// user-target needs operator; user-tenant → tenant needs tenant_admin;
// see T3 “assert_can_promote“ for the matrix). The service-layer
// :class:`PermissionDeniedError` surfaces as 403 with the canonical
// “insufficient_promotion_authority“ detail.
//
// Idempotency: a re-run against an already-promoted slug returns 200
// with the existing target row (no duplicate insert, no 409). The CLI
// detects this case by comparing the entry's “created_at“ against a
// pre-POST timestamp and surfaces exit code 6 (informational) in
// human-readable mode; “--json“ collapses to exit 0 so scripts
// don't treat the no-op as failure.
//
// Exit codes:
//   - 0  success — fresh promotion
//   - 2  auth_expired
//   - 3  unreachable
//   - 4  unexpected_response (incl. 400 cross-ladder, 404 source not visible)
//   - 5  insufficient_promotion_authority (HTTP 403)
//   - 6  already_promoted_at_target_idempotent (HTTP 200 no-op,
//     human-readable mode only; “--json“ collapses to exit 0)
func NewPromoteCmd() *cobra.Command {
	var (
		toFlag            string
		moveFlag          bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "promote <scope>/<slug>",
		Short: "Promote one memory to a strictly broader scope (POST /api/v1/memory/{scope}/{slug}/promote)",
		Long: "promote calls POST /api/v1/memory/{scope}/{slug}/promote " +
			"to broaden a memory's visibility. The ladder is enforced " +
			"server-side: user → user-tenant / user-target → tenant / " +
			"target. Cross-ladder steps (e.g. user-tenant → target) are " +
			"rejected as 400 cross_ladder and surfaced as " +
			"unexpected_response (exit 4).\n\n" +
			"--to NAME selects the target scope (required). --move " +
			"deletes the source row in the same transaction as the " +
			"target insert (broadens-and-leaves vs. broadens-and-" +
			"rewires); default behaviour leaves the source intact.\n\n" +
			"Idempotency: a re-run against an already-promoted slug " +
			"returns 200 with the existing target row (no duplicate " +
			"insert, no 409). Human-readable output surfaces this as " +
			"exit 6 (informational); --json collapses to exit 0 so " +
			"scripts piping through `jq` don't treat the no-op as " +
			"failure.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPromote(cmd, promoteOptions{
				ScopeSlugArg:      args[0],
				ToArg:             toFlag,
				Move:              moveFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
				Now:               time.Now,
			})
		},
	}
	cmd.Flags().StringVar(&toFlag, "to", "",
		"target scope: user-tenant|user-target|tenant|target (required)")
	cmd.Flags().BoolVar(&moveFlag, "move", false,
		"delete the source row in the same transaction (broadens-and-leaves vs. broadens-and-rewires)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw MemoryEntry JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by `meho login`)")
	// --to is required at the CLI layer; failing to flag it raises a
	// cobra error before any RBAC / network round-trip.
	if err := cmd.MarkFlagRequired("to"); err != nil {
		// cobra.MarkFlagRequired returns an error only when the flag
		// doesn't exist on the command. Defensive panic: the flag is
		// declared two lines above, so this is a programmer-error path
		// that would surface in CI well before any operator ran it.
		panic(fmt.Sprintf("cli/promote: MarkFlagRequired(\"to\"): %v", err))
	}
	return cmd
}

type promoteOptions struct {
	ScopeSlugArg      string
	ToArg             string
	Move              bool
	JSONOut           bool
	BackplaneOverride string
	// Now is injectable so unit tests can pin the reference time used
	// to distinguish "fresh promotion" from "idempotent re-run".
	// Production callers pass ``time.Now``.
	Now func() time.Time
}

func runPromote(cmd *cobra.Command, opts promoteOptions) error {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	sourceScope, sourceSlug, err := parseScopeSlugArg(opts.ScopeSlugArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	targetScope, err := parseScope(opts.ToArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			backplane.ClassifyError(err), opts.JSONOut)
	}
	reqBody := api.PromoteBody{
		To:   targetScope,
		Move: &opts.Move,
	}
	// Capture the pre-POST wall clock so a successful response can be
	// classified as fresh (entry.CreatedAt >= preCallAt) vs.
	// idempotent re-run (entry.CreatedAt < preCallAt). The granularity
	// is comfortable: backend writes ``created_at`` from a clock
	// inside the same transaction that returns the row, so the gap
	// between "client about to send POST" and "row inserted" is
	// bounded by the round-trip — single-digit milliseconds in
	// practice. A re-run finds an existing row whose ``created_at`` is
	// from a previous wall-clock, which is on the seconds-or-more
	// timescale.
	preCallAt := opts.Now().UTC()
	resp, err := postPromote(cmd.Context(), backplaneURL, sourceScope, sourceSlug, reqBody)
	if err != nil {
		return renderPromoteRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderPromoteHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil
	// (printPromoteSummary nil-guards, but the operator would see an
	// empty line with exit 0 — phantom success). Mirrors
	// `cli/internal/cmd/status.go:142` + the kb sibling's
	// post-iter-2 nil-guard pattern.
	entry := resp.JSON200
	if entry == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a memory entry payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	idempotent := isIdempotentRerun(entry, preCallAt)
	if opts.JSONOut {
		// JSON consumers should never see a non-zero exit on an
		// idempotent re-run — the response shape is identical and
		// scripts that pipe into jq would otherwise need to special-
		// case exit 6. The acceptance criterion (#627) pins this
		// shape: ``--json`` collapses to exit 0 even on the no-op
		// branch.
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	printPromoteSummary(cmd.OutOrStdout(), entry, idempotent, opts.Move)
	if idempotent {
		// Return a typed silent error so cli/cmd/meho/main.go's
		// ExitCoder type assertion picks up the informational exit
		// code without re-emitting the human line (already printed
		// above).
		return &idempotentPromoteError{}
	}
	return nil
}

// idempotentPromoteError carries the informational exit code 6 to
// main.go via the “output.ExitCoder“ type assertion. The “Error“
// method returns an empty string so cobra's default error printer
// stays silent — the printed line lives in
// :func:`printPromoteSummary`.
//
// Implements “error“ + “ExitCode() int“. Same silent-error shape
// :mod:`cli/internal/output`'s “RenderError“ uses for the JSON
// path; reused here for the human-mode exit-6 surface.
type idempotentPromoteError struct{}

func (idempotentPromoteError) Error() string  { return "" }
func (*idempotentPromoteError) ExitCode() int { return exitIdempotentPromote }

// isIdempotentRerun returns true when the entry returned by the
// promote route looks like an idempotent re-run rather than a fresh
// insert. The signal is a “created_at“ timestamp strictly before
// the wall clock the CLI captured immediately before sending the
// POST.
//
// Robustness notes:
//
//   - Zero / unset “CreatedAt“ → treated as fresh (false).
//     Misclassifying a re-run as fresh degrades the UX (operator sees
//     exit 0 + "promoted" wording) but never returns the wrong row;
//     the alternative — treating a missing timestamp as idempotent —
//     would block a successful first promotion behind a false-positive
//     warning.
//   - Sub-second skew between client and server clocks is tolerable
//     because a re-run carries the “created_at“ from a previous
//     promotion, which is seconds-or-more in the past.
func isIdempotentRerun(entry *api.MemoryEntry, preCallAt time.Time) bool {
	if entry == nil || entry.CreatedAt.IsZero() {
		return false
	}
	return entry.CreatedAt.UTC().Before(preCallAt)
}

func postPromote(
	ctx context.Context,
	backplaneURL string,
	sourceScope Scope,
	sourceSlug string,
	req api.PromoteBody,
) (*api.PromoteApiV1MemoryScopeSlugPromotePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.PromoteApiV1MemoryScopeSlugPromotePostResponse, error) {
			return authed.PromoteApiV1MemoryScopeSlugPromotePostWithResponse(
				ctx,
				sourceScope,
				sourceSlug,
				&api.PromoteApiV1MemoryScopeSlugPromotePostParams{},
				req,
			)
		},
		func(r *api.PromoteApiV1MemoryScopeSlugPromotePostResponse) int { return r.StatusCode() },
	)
}

// renderPromoteRequestError translates a transport-layer error from
// postPromote into the right “output.StructuredError“ category.
// The transport surface is identical to the shared
// :func:`renderRequestError`; specialisation is at the HTTP-status
// layer (:func:`renderPromoteHTTPStatus`).
func renderPromoteRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	return renderRequestError(cmd, backplaneURL, err, jsonOut)
}

// renderPromoteHTTPStatus classifies a non-2xx promote response.
// Specialises the shared :func:`renderHTTPStatus` for the promote
// route's distinct 403 mapping (`insufficient_promotion_authority`)
// and the 400 cross-ladder / 404 source-not-visible / 409 / 422 /
// 501 spread that all collapse to `unexpected_response` (exit 4).
func renderPromoteHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	switch statusCode {
	case http.StatusUnauthorized:
		// Mirrors the shared helper; pinned here so the per-verb
		// error table is one read.
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"backplane rejected the stored token; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	case http.StatusForbidden:
		// Issue #627 AC: 403 → exit 5 with detail surfacing the
		// route's canonical ``insufficient_promotion_authority``
		// string verbatim. The route's HTTPException carries the
		// short literal as the ``detail`` field (see
		// :func:`api.v1.memory.promote`); decodeDetailString unwraps
		// it.
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(string(body))),
			jsonOut,
		)
	default:
		// 400 cross-ladder, 404 source-not-visible, 409 target slug
		// conflict, 422 validation, 501 not-implemented per-target
		// ACL gap — all surface as unexpected_response (exit 4) with
		// the route's detail string passed through. The CLI is the
		// thin transport; the operator-facing remedy lives in the
		// detail prose.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, statusCode, decodeDetailString(string(body)))),
			jsonOut,
		)
	}
}

// printPromoteSummary renders the promoted entry as a compact
// confirmation line plus the natural-key coordinates the operator
// will use for a subsequent “meho recall“ against the target scope.
// On an idempotent re-run the wording flips to "already promoted" so
// the operator sees the no-op explicitly; the human-mode exit-6 code
// is what scripts read.
func printPromoteSummary(w io.Writer, e *api.MemoryEntry, idempotent bool, move bool) {
	if e == nil {
		return
	}
	verb := "promoted"
	if idempotent {
		verb = fmt.Sprintf("already promoted (%s)", errCodeIdempotentPromote)
	}
	suffix := ""
	if move && !idempotent {
		// "--move" on a re-run is a no-op (the source was already
		// deleted on the original call); only surface the suffix on
		// the fresh-insert path so the operator's mental model
		// matches the wire shape.
		suffix = " (source row removed)"
	}
	fmt.Fprintf(w, "%s %s/%s%s\n", verb, e.Scope, e.Slug, suffix)
	fmt.Fprintf(w, "%-14s %s\n", "id:", e.Id.String())
	fmt.Fprintf(w, "%-14s %s\n", "scope:", e.Scope)
	fmt.Fprintf(w, "%-14s %s\n", "slug:", e.Slug)
	fmt.Fprintf(w, "%-14s %s\n", "expires_at:", formatTimePtr(e.ExpiresAt))
	fmt.Fprintf(w, "%-14s %s\n", "user_sub:", pluralisePtr(e.UserSub))
	fmt.Fprintf(w, "%-14s %s\n", "target_name:", pluralisePtr(e.TargetName))
	fmt.Fprintf(w, "%-14s %s\n", "created_at:", e.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	if promotedFrom := metadataStringField(e.Metadata, "promoted_from"); promotedFrom != "" {
		fmt.Fprintf(w, "%-14s %s\n", "promoted_from:", promotedFrom)
	}
}

// metadataStringField extracts a string field from the typed
// “map[string]any“ metadata blob, returning "" when the field is
// absent or non-string. The promoted_from key is the load-bearing
// provenance marker the backend writes (`memory/service.py
// _build_promotion_metadata`); surfacing it in the summary lets the
// operator verify the ladder step in one read.
func metadataStringField(md map[string]any, key string) string {
	if md == nil {
		return ""
	}
	v, ok := md[key]
	if !ok {
		return ""
	}
	s, ok := v.(string)
	if !ok {
		return ""
	}
	return s
}
