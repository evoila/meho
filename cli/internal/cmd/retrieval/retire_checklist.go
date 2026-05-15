// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os/exec"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
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

// surfaceLabelPrefix is the per-issue surface marker. An issue labeled
// `retrieval-migration-blocker` + `surface:kb` is bucketed against kb.
// An issue labeled `retrieval-migration-blocker` with no surface
// marker is treated as a general blocker and counted against every
// surface — the conservative interpretation that a generic blocker
// holds every retire candidate until resolved.
const surfaceLabelPrefix = "surface:"

// defaultGHRepo is the repository the lookup defaults to. Overridable
// via `--gh-repo` so operators with a fork or with the migration-
// blocker label living in `evoila-bosnia/meho-internal` can point the
// lookup at the right place.
const defaultGHRepo = "evoila/meho"

// ghLookupTimeout caps the gh subprocess so a misconfigured network
// can't block the retire-checklist verb indefinitely.
const ghLookupTimeout = 30 * time.Second

// RetireCriterionResult mirrors the backend `CriterionResult` shape.
// Hand-written (rather than oapi-codegen-generated) because the Go
// regen pass for the new endpoint runs in a follow-up PR; the shape
// is small and pinned by the matching backend test.
type RetireCriterionResult struct {
	Name             string  `json:"name"`
	Verdict          string  `json:"verdict"`
	ObservedValue    string  `json:"observed_value"`
	ThresholdSummary string  `json:"threshold_summary"`
	Notes            *string `json:"notes,omitempty"`
}

// RetireSurfaceChecklist mirrors the backend `SurfaceChecklist` model.
type RetireSurfaceChecklist struct {
	Surface  string                  `json:"surface"`
	Verdict  string                  `json:"verdict"`
	Criteria []RetireCriterionResult `json:"criteria"`
}

// RetireChecklistReport mirrors the backend `RetireChecklistReport`.
type RetireChecklistReport struct {
	RanAt           string                   `json:"ran_at"`
	TenantID        *string                  `json:"tenant_id,omitempty"`
	Since           string                   `json:"since"`
	Until           string                   `json:"until"`
	Surfaces        []RetireSurfaceChecklist `json:"surfaces"`
	OverallVerdict  string                   `json:"overall_verdict"`
}

// retireRequest is the POST body shape; mirrors the backend
// `RetireChecklistRequest`. `BlockerCounts` is a pointer so Go's
// `omitempty` only suppresses the field when no map is supplied —
// an empty map would otherwise be serialised as `{}` which the
// backend treats as "every surface has zero blockers" (green) rather
// than the intended "unknown" (yellow).
type retireRequest struct {
	Surface       string          `json:"surface"`
	BlockerCounts *map[string]int `json:"blocker_counts,omitempty"`
}

// ghIssueLabel is one element of the labels array returned by
// `gh issue list --json labels`.
type ghIssueLabel struct {
	Name string `json:"name"`
}

// ghIssue captures the slice of the gh JSON output we need (number
// for diagnostics, labels for the surface bucket).
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
//
// Exit codes:
//   - 0   request succeeded (any verdict — this verb is informational,
//         not a CI gate; the operator + team-of-4 read the verdict and
//         decide).
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
			"against --gh-repo (default: evoila/meho), buckets results by the " +
			"`surface:<name>` marker label, and passes the per-surface count " +
			"to the backplane. Pass --no-blockers to skip the lookup; the " +
			"backplane reports criterion 5 as `REVIEW MANUALLY` when the count " +
			"is unknown.\n\n" +
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

	return cmd
}

type retireOptions struct {
	Surface           string
	JSONOut           bool
	BackplaneOverride string
	GHRepo            string
	NoBlockers        bool
}

// runRetireChecklist orchestrates the retire-checklist request: resolve
// the backplane URL, optionally compute blocker counts via `gh`, POST
// the request, render the response. Each error class is mapped to its
// structured-error category so main() picks the right exit code.
func runRetireChecklist(cmd *cobra.Command, opts retireOptions) error {
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			classifyBackplaneError(err),
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

	report, err := postRetireChecklist(cmd.Context(), backplaneURL, opts, blockerCounts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}

	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), report)
	}
	printRetireTable(cmd.OutOrStdout(), report)
	return nil
}

// postRetireChecklist calls POST /api/v1/retrieve/retire-checklist with
// the surface + blocker_counts body. Mirrors `postEval`'s 401-retry
// shape: one transparent refresh + retry on auth failure.
func postRetireChecklist(
	ctx context.Context,
	backplaneURL string,
	opts retireOptions,
	blockerCounts *map[string]int,
) (*RetireChecklistReport, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	httpClient := authed.HTTPClient()
	bearer := authed.AccessToken()
	if bearer == "" {
		return nil, errors.New("meho: stored token has no access_token")
	}

	body, err := json.Marshal(retireRequest{
		Surface:       opts.Surface,
		BlockerCounts: blockerCounts,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal retire request: %w", err)
	}

	resp, err := postRetireWithBearer(ctx, httpClient, backplaneURL, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close()
			return nil, rerr
		}
		resp.Body.Close()
		bearer = authed.AccessToken()
		resp, err = postRetireWithBearer(ctx, httpClient, backplaneURL, bearer, body)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}

	var out RetireChecklistReport
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode retire response: %w", err)
	}
	return &out, nil
}

func postRetireWithBearer(
	ctx context.Context,
	client *http.Client,
	backplaneURL, bearer string,
	body []byte,
) (*http.Response, error) {
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost,
		backplaneURL+"/api/v1/retrieve/retire-checklist",
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, fmt.Errorf("build retire request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	return client.Do(req)
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
// to, derived from `surface:<name>` marker labels. An issue with
// multiple surface markers is bucketed against every named surface
// (the multi-surface blocker case).
func surfacesFromLabels(labels []ghIssueLabel) []string {
	var out []string
	for _, label := range labels {
		if !strings.HasPrefix(label.Name, surfaceLabelPrefix) {
			continue
		}
		name := strings.TrimPrefix(label.Name, surfaceLabelPrefix)
		for _, surface := range retireSurfaces {
			if name == surface {
				out = append(out, surface)
				break
			}
		}
	}
	return out
}

// printRetireTable renders the report as a human-readable table per
// surface, with a final overall-verdict line. The format is
// deliberately compact (one line per criterion) so a 4-surface
// `--surface all` report still fits on a single terminal screen.
func printRetireTable(w io.Writer, r *RetireChecklistReport) {
	fmt.Fprintf(w, "Retire checklist (%s) — overall: %s\n", r.RanAt, r.OverallVerdict)
	if r.TenantID != nil {
		fmt.Fprintf(w, "tenant: %s\n", *r.TenantID)
	}
	for _, surface := range r.Surfaces {
		fmt.Fprintf(w, "\n  %s — %s\n", surface.Surface, surface.Verdict)
		for _, c := range surface.Criteria {
			notes := ""
			if c.Notes != nil && *c.Notes != "" {
				notes = " (" + *c.Notes + ")"
			}
			fmt.Fprintf(w, "    [%s] %-22s  %s  (threshold: %s)%s\n",
				strings.ToUpper(c.Verdict),
				c.Name,
				c.ObservedValue,
				c.ThresholdSummary,
				notes,
			)
		}
	}
}
