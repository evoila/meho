// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// Surface labels honoured by the retire-checklist verb. Aligned with
// the backend's `meho_backplane.retrieval.usage.SUPPORTED_SURFACES`
// and the parent Initiative's three-surface model (kb / memory /
// operations).
var retireSurfaces = []string{"kb", "memory", "operations"}

// blockerLabel is the label the gh issue list query filters on.
// Documented in `docs/cross-repo/retrieval-retirement.md` (T7 #446)
// and added to the evoila/meho repo once T7 ships. The CLI's lookup
// against an unset label simply returns zero issues — treated as
// "no blockers filed" (green for criterion 5).
const blockerLabel = "retrieval-migration-blocker"

// surfaceLabelToBucket maps the existing per-issue surface labels
// to the three retrieval surfaces the retire-checklist verdicts
// against. The naming matches the scheme T7 (#446) ships in
// `docs/cross-repo/retrieval-retirement.md`: a single repo-wide
// `retrieval-migration-blocker` label plus the existing surface-
// adjacent labels operators already use (`knowledge` on kb issues,
// `memory` on memory issues, `connector` on operation-substrate
// issues). T7's body explicitly chose this scheme over a per-surface
// `surface:*` namespace ("no per-surface labels in v0.2; simpler.
// Per-surface filtering done by the issue's existing labels
// (`knowledge`, `connector`, etc.) cross-referenced via `gh issue
// list --label retrieval-migration-blocker --label knowledge` etc.").
// An issue labeled `retrieval-migration-blocker` without any of
// these surface markers is treated as a general blocker and counted
// against every surface — the conservative interpretation that a
// generic blocker holds every retire candidate until resolved.
var surfaceLabelToBucket = map[string]string{
	"knowledge": "kb",
	"memory":    "memory",
	"connector": "operations",
}

// defaultGHRepo is the repository the lookup defaults to. Overridable
// via `--gh-repo` so operators with a fork or with the migration-
// blocker label living in `evoila-bosnia/meho-internal` can point the
// lookup at the right place.
const defaultGHRepo = "evoila/meho"

// ghLookupTimeout caps the gh subprocess so a misconfigured network
// can't block the retire-checklist verb indefinitely.
const ghLookupTimeout = 30 * time.Second

// ghIssueLabel is one element of the labels array returned by
// `gh issue list --json labels`. This type stays hand-typed (not
// generated) because it describes the `gh` CLI subprocess JSON
// shape, not a meho backplane API; it has no place in the
// openapi.json snapshot.
type ghIssueLabel struct {
	Name string `json:"name"`
}

// ghIssue captures the slice of the gh JSON output we need (number
// for diagnostics, labels for the surface bucket). Same posture as
// ghIssueLabel — describes the `gh` subprocess output, not a
// backplane API.
type ghIssue struct {
	Number int            `json:"number"`
	Labels []ghIssueLabel `json:"labels"`
}

// newRetireChecklistCmd returns the `meho retrieval retire-checklist`
// subcommand.
//
// CLI shape (matches issue #445 spec):
//
//	meho retrieval retire-checklist \
//	  [--surface kb|memory|operations|all]   # default: all
//	  [--json]                                # structured RetireChecklistReport
//	  [--backplane <url>]                     # override the configured backplane
//	  [--gh-repo <owner/name>]                # repo to query for blocker issues
//	  [--no-blockers]                         # skip gh lookup; send blocker_counts=null
//	  [--baseline-file <path>]                # JSON file with per-surface baseline metrics
//
// Exit codes:
//   - 0   request succeeded (any verdict — this verb is informational,
//     not a CI gate; the operator + team-of-4 read the verdict and
//     decide).
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
//
// Note: a `NOT YET` verdict does not exit non-zero. The retire-
// checklist is decision support, not enforcement; gating CI on it
// would block every backend PR until kb is retire-ready, which is
// the opposite of the v0.2 intent.
func newRetireChecklistCmd() *cobra.Command {
	var (
		surface           string
		jsonOut           bool
		backplaneOverride string
		ghRepo            string
		noBlockers        bool
		baselineFile      string
	)

	cmd := &cobra.Command{
		Use:   "retire-checklist",
		Short: "Run the 5-criterion retire-decision checklist per retrieval surface",
		Long: "retire-checklist combines T2's corpus eval results with T5's " +
			"audit-log-backed usage telemetry against Goal #215 decision #2's " +
			"5 criteria, and prints a per-surface green/yellow/red checklist " +
			"plus an overall verdict (READY TO RETIRE / REVIEW MANUALLY / NOT YET).\n\n" +
			"Criterion 5 (zero open `retrieval-migration-blocker` issues) is " +
			"computed locally: the verb runs `gh issue list --label " +
			"retrieval-migration-blocker --state open --json number,labels` " +
			"against --gh-repo (default: evoila/meho) and buckets results by " +
			"the existing per-surface labels documented in T7's runbook " +
			"(`knowledge` → kb, `memory` → memory, `connector` → operations). " +
			"Pass --no-blockers to skip the lookup; the backplane reports " +
			"criterion 5 as `REVIEW MANUALLY` when the count is unknown.\n\n" +
			"Criterion 4 (MEHO ≥ baseline) needs side-by-side baseline numbers " +
			"the v0.2 backplane cannot produce on its own (no server-side " +
			"corpus snapshot). Pass --baseline-file <path> pointing at a JSON " +
			"file produced by `meho retrieval eval --baseline grep --save-baseline " +
			"<path>` to supply the per-surface baseline metrics; without it, " +
			"criterion 4 stays yellow (REVIEW MANUALLY).\n\n" +
			"The verb exits 0 on any verdict — it is decision support, not a " +
			"CI gate. Operators + the team-of-4 read the output and make the " +
			"retire call manually (the actual retire commit is operator-driven " +
			"per the parent Initiative).",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRetireChecklist(cmd, retireOptions{
				Surface:           surface,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
				GHRepo:            ghRepo,
				NoBlockers:        noBlockers,
				BaselineFile:      baselineFile,
			})
		},
	}

	cmd.Flags().StringVar(&surface, "surface", "all",
		"retrieval surface to evaluate (kb|memory|operations|all)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the structured RetireChecklistReport on stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.Flags().StringVar(&ghRepo, "gh-repo", defaultGHRepo,
		"GitHub repo to query for `retrieval-migration-blocker` issues")
	cmd.Flags().BoolVar(&noBlockers, "no-blockers", false,
		"skip the gh lookup; the backplane reports criterion 5 as REVIEW MANUALLY")
	cmd.Flags().StringVar(&baselineFile, "baseline-file", "",
		"JSON file containing per-surface baseline metrics (output of "+
			"`meho retrieval eval --baseline grep --save-baseline ...`); "+
			"without it, criterion 4 stays yellow")

	return cmd
}

type retireOptions struct {
	Surface           string
	JSONOut           bool
	BackplaneOverride string
	GHRepo            string
	NoBlockers        bool
	BaselineFile      string
}

// runRetireChecklist orchestrates the retire-checklist request: resolve
// the backplane URL, optionally compute blocker counts via `gh`, POST
// the request, render the response. Each error class is mapped to its
// structured-error category so main() picks the right exit code.
func runRetireChecklist(cmd *cobra.Command, opts retireOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			backplane.ClassifyError(err),
			opts.JSONOut,
		)
	}

	// Resolve blocker counts before talking to the backplane so a gh
	// failure doesn't waste an authenticated round-trip. The lookup
	// is best-effort: if `gh` is missing or the call fails, we log
	// a warning to stderr and send null (backend reports criterion 5
	// as yellow / REVIEW MANUALLY).
	var blockerCounts *map[string]int
	if !opts.NoBlockers {
		counts, lookupErr := lookupBlockerCounts(cmd.Context(), opts.GHRepo)
		if lookupErr != nil {
			fmt.Fprintf(cmd.ErrOrStderr(),
				"warning: blocker lookup failed (criterion 5 will be REVIEW MANUALLY): %v\n",
				lookupErr,
			)
		} else {
			blockerCounts = &counts
		}
	}

	// Resolve baseline overrides from --baseline-file if provided.
	// Same best-effort posture as blocker counts: an unreadable /
	// malformed baseline file warns and falls back to criterion 4
	// yellow rather than failing the whole verb. The fail-soft shape
	// matches operator expectations — the baseline data is auxiliary,
	// the request should still produce a usable verdict on the other
	// four criteria.
	var baselineOverrides *map[string]api.BaselineMetricsOverride
	if opts.BaselineFile != "" {
		overrides, baselineErr := loadBaselineOverrides(opts.BaselineFile)
		if baselineErr != nil {
			fmt.Fprintf(cmd.ErrOrStderr(),
				"warning: baseline-file load failed (criterion 4 will be REVIEW MANUALLY): %v\n",
				baselineErr,
			)
		} else {
			baselineOverrides = &overrides
		}
	}

	resp, err := postRetireChecklist(
		cmd.Context(), backplaneURL, opts, blockerCounts, baselineOverrides,
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil.
	// printRetireTable's empty-surfaces branch silently emits the
	// overall verdict line only — without this guard, a malformed
	// 200 would print an empty checklist and exit 0. Mirrors the
	// convention in `cli/internal/cmd/status.go:142`.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a retire checklist payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	report := resp.JSON200

	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), report)
	}
	printRetireTable(cmd.OutOrStdout(), report)
	return nil
}

// retireRequestBody assembles the typed POST body for
// /api/v1/retrieve/retire-checklist. `Surface` is set only when the
// operator supplied a non-default value (the generated pointer
// shape's `omitempty` keeps the wire compact). `BlockerCounts` and
// `BaselineOverrides` flow through unchanged — both are
// `*map[...]...` on the generated `api.RetireChecklistRequest` so a
// nil pointer drops the key (Pydantic absence == null) while a
// non-nil pointer to an empty map serialises as `{}` ("zero
// blockers everywhere" intent, distinct from the unknown intent).
func retireRequestBody(
	opts retireOptions,
	blockerCounts *map[string]int,
	baselineOverrides *map[string]api.BaselineMetricsOverride,
) api.RetireChecklistRequest {
	body := api.RetireChecklistRequest{
		BlockerCounts:     blockerCounts,
		BaselineOverrides: baselineOverrides,
	}
	if opts.Surface != "" {
		surface := api.RetireChecklistRequestSurface(opts.Surface)
		body.Surface = &surface
	}
	return body
}

// postRetireChecklist calls POST /api/v1/retrieve/retire-checklist
// with the surface + blocker_counts + baseline_overrides body via
// the generated typed client. The 401-refresh-retry loop runs
// through retryOn401.
func postRetireChecklist(
	ctx context.Context,
	backplaneURL string,
	opts retireOptions,
	blockerCounts *map[string]int,
	baselineOverrides *map[string]api.BaselineMetricsOverride,
) (*api.RetireChecklistEndpointApiV1RetrieveRetireChecklistPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := retireRequestBody(opts, blockerCounts, baselineOverrides)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RetireChecklistEndpointApiV1RetrieveRetireChecklistPostResponse, error) {
			return authed.RetireChecklistEndpointApiV1RetrieveRetireChecklistPostWithResponse(
				ctx, nil, body,
			)
		},
		func(r *api.RetireChecklistEndpointApiV1RetrieveRetireChecklistPostResponse) int {
			return r.StatusCode()
		},
	)
}

// lookupBlockerCounts queries `gh issue list` for open issues labeled
// `retrieval-migration-blocker` in *repo* and buckets the results by
// the `surface:<name>` marker label. Returns a map covering every
// supported surface (zeros included so a downstream consumer can rely
// on the key set).
//
// An issue labeled `retrieval-migration-blocker` *without* a
// `surface:<name>` marker is counted against every surface — the
// conservative interpretation: a generic blocker holds every retire
// candidate until resolved.
//
// Failure modes:
//   - `gh` not on PATH → returns a wrapped exec.LookPath error.
//   - `gh` exits non-zero → returns a wrapped exit error with the
//     captured stderr.
//   - Output not valid JSON → returns a wrapped json error.
//
// The caller (runRetireChecklist) treats any error as "blocker count
// unknown" and proceeds with `blocker_counts=null` in the request.
func lookupBlockerCounts(ctx context.Context, repo string) (map[string]int, error) {
	// Run `gh` with a bounded timeout so a misconfigured PATH or a
	// stalled gh process can't hang the verb. Bounded context replaces
	// the parent so the rest of the verb's work isn't cut short.
	lookupCtx, cancel := context.WithTimeout(ctx, ghLookupTimeout)
	defer cancel()

	cmd := exec.CommandContext( //nolint:gosec // controlled CLI args + bounded ctx
		lookupCtx,
		"gh", "issue", "list",
		"--repo", repo,
		"--label", blockerLabel,
		"--state", "open",
		"--json", "number,labels",
		"--limit", "200",
	)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		stderrTrim := strings.TrimSpace(stderr.String())
		if stderrTrim != "" {
			return nil, fmt.Errorf("gh issue list failed: %w: %s", err, stderrTrim)
		}
		return nil, fmt.Errorf("gh issue list failed: %w", err)
	}

	var issues []ghIssue
	if err := json.Unmarshal(stdout.Bytes(), &issues); err != nil {
		return nil, fmt.Errorf("parse gh issue list output: %w", err)
	}

	counts := make(map[string]int, len(retireSurfaces))
	for _, surface := range retireSurfaces {
		counts[surface] = 0
	}
	for _, issue := range issues {
		surfacesHit := surfacesFromLabels(issue.Labels)
		if len(surfacesHit) == 0 {
			// Generic blocker (no surface marker) — count against
			// every surface per the conservative-interpretation rule.
			for _, surface := range retireSurfaces {
				counts[surface]++
			}
			continue
		}
		for _, surface := range surfacesHit {
			counts[surface]++
		}
	}
	return counts, nil
}

// surfacesFromLabels returns the surface bucket(s) the issue belongs
// to, derived from the existing per-surface labels T7 (#446) lists
// in the operator runbook (`knowledge` → kb, `memory` → memory,
// `connector` → operations). An issue with multiple surface markers
// is bucketed against every named surface (the multi-surface
// blocker case). Unknown labels are ignored — operators may add
// extra labels for triage that don't bear on retire-decision
// bucketing.
func surfacesFromLabels(labels []ghIssueLabel) []string {
	var out []string
	seen := make(map[string]bool, len(retireSurfaces))
	for _, label := range labels {
		bucket, ok := surfaceLabelToBucket[label.Name]
		if !ok || seen[bucket] {
			continue
		}
		seen[bucket] = true
		out = append(out, bucket)
	}
	return out
}

// loadBaselineOverrides reads a JSON file produced by
// `meho retrieval eval --baseline grep --save-baseline <path>` and
// extracts the per-surface baseline metrics into the wire shape the
// backend's `BaselineMetricsOverride` expects. Returns one entry per
// surface that has a non-nil baseline triple in the source file;
// surfaces without baseline data (the v0.2 memory + operations
// surfaces) are simply omitted from the returned map and the backend
// leaves criterion 4 yellow for those surfaces.
//
// The shape this function reads matches `api.EvalResult` from the
// generated typed client — same struct the eval verb's
// `--save-baseline` writes, so re-using it keeps the round-trip
// tight rather than declaring a parallel struct that could drift.
func loadBaselineOverrides(path string) (map[string]api.BaselineMetricsOverride, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read baseline file %q: %w", path, err)
	}
	var parsed api.EvalResult
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, fmt.Errorf("parse baseline file %q: %w", path, err)
	}
	out := make(map[string]api.BaselineMetricsOverride, len(parsed.Surfaces))
	for _, s := range parsed.Surfaces {
		// Skip surfaces whose baseline didn't run — the eval runner
		// reports `baseline_kind=null` (decoded as `nil` here) for
		// memory + operations in v0.2 and for kb when the operator
		// didn't pass `--baseline grep`. Without a baseline triple
		// criterion 4 cannot go green; emitting an empty / zero-
		// metric override would silently flip the criterion red
		// instead of yellow, which is the wrong default.
		if s.BaselineKind == nil ||
			s.BaselinePrecisionAt5 == nil ||
			s.BaselineMrr == nil ||
			s.BaselineCoverage == nil {
			continue
		}
		kind := *s.BaselineKind
		out[string(s.Surface)] = api.BaselineMetricsOverride{
			PrecisionAt5: *s.BaselinePrecisionAt5,
			Mrr:          *s.BaselineMrr,
			Coverage:     *s.BaselineCoverage,
			Kind:         &kind,
		}
	}
	return out, nil
}

// printRetireTable renders the report as a human-readable table per
// surface, with a final overall-verdict line. The format is
// deliberately compact (one line per criterion) so a 4-surface
// `--surface all` report still fits on a single terminal screen.
//
// `RanAt` is a `time.Time` on the generated `api.RetireChecklistReport`;
// formatted as RFC3339 to preserve the pre-migration shape operators
// correlate with audit-log rows.
func printRetireTable(w io.Writer, r *api.RetireChecklistReport) {
	fmt.Fprintf(w, "Retire checklist (%s) — overall: %s\n",
		r.RanAt.Format("2006-01-02T15:04:05Z07:00"), r.OverallVerdict)
	if r.TenantId != nil {
		fmt.Fprintf(w, "tenant: %s\n", r.TenantId.String())
	}
	for _, surface := range r.Surfaces {
		fmt.Fprintf(w, "\n  %s — %s\n", surface.Surface, surface.Verdict)
		for _, c := range surface.Criteria {
			notes := ""
			if c.Notes != nil && *c.Notes != "" {
				notes = " (" + *c.Notes + ")"
			}
			fmt.Fprintf(w, "    [%s] %-22s  %s  (threshold: %s)%s\n",
				strings.ToUpper(string(c.Verdict)),
				c.Name,
				c.ObservedValue,
				c.ThresholdSummary,
				notes,
			)
		}
	}
}
