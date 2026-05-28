// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newEvalCmd returns the `meho retrieval eval` subcommand.
//
// CLI shape (matches issue #441 spec):
//
//	meho retrieval eval \
//	  [--surface kb|memory|operations|all]   # default: all
//	  [--baseline grep]                       # run grep -r baseline (kb only)
//	  [--save-baseline <file>]                # save this run for regression detection
//	  [--compare-baseline <file>]             # compare against saved; exit 1 on regression
//	  [--json]                                # machine-readable output
//	  [--backplane <url>]                     # override the configured backplane
//
// Exit codes:
//   - 0   eval ran cleanly + verdict was green / yellow
//   - 1   verdict was red (CI gate signal)
//   - 1   --compare-baseline detected a regression
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
func newEvalCmd() *cobra.Command {
	var (
		surface             string
		baseline            string
		saveBaselinePath    string
		compareBaselinePath string
		jsonOut             bool
		backplaneOverride   string
	)

	cmd := &cobra.Command{
		Use:   "eval",
		Short: "Run the checked-in eval corpus + report precision@5 / MRR / coverage",
		Long: "eval runs the eval corpus shipped with meho_backplane against " +
			"/api/v1/retrieve/eval (one or more retrieval surfaces) and " +
			"prints precision@5, MRR, and coverage with a green / yellow / " +
			"red verdict per surface plus an overall verdict.\n\n" +
			"Use --baseline grep to also run a grep-based baseline against " +
			"the kb surface (operators may retire kb/ only when MEHO ranking " +
			"is at least as good as the pre-MEHO grep workflow).\n\n" +
			"Use --save-baseline / --compare-baseline for regression " +
			"detection: --save-baseline writes today's metrics to a JSON " +
			"file, and --compare-baseline reads a prior run + exits 1 if " +
			"any per-surface metric dropped by more than the noise floor " +
			"(0.02). The CI gate (.github/workflows/eval-gate.yml) calls " +
			"the latter against ci/eval-baseline.json on every backend PR.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEval(cmd, evalOptions{
				Surface:             surface,
				Baseline:            baseline,
				SaveBaselinePath:    saveBaselinePath,
				CompareBaselinePath: compareBaselinePath,
				JSONOut:             jsonOut,
				BackplaneOverride:   backplaneOverride,
			})
		},
	}

	cmd.Flags().StringVar(&surface, "surface", "all",
		"retrieval surface to evaluate (kb|memory|operations|all)")
	cmd.Flags().StringVar(&baseline, "baseline", "",
		"baseline kind to also run; only `grep` is supported in v0.2 (kb surface only)")
	cmd.Flags().StringVar(&saveBaselinePath, "save-baseline", "",
		"write the eval result to this file for future regression comparison")
	cmd.Flags().StringVar(&compareBaselinePath, "compare-baseline", "",
		"compare today's eval against this saved baseline; exit 1 on any per-metric regression")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a machine-readable JSON envelope on stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")

	return cmd
}

type evalOptions struct {
	Surface             string
	Baseline            string
	SaveBaselinePath    string
	CompareBaselinePath string
	JSONOut             bool
	BackplaneOverride   string
}

// runEval is the eval command's RunE body. Kept separate so the
// flag-parsing boilerplate stays in newEvalCmd and the request-
// response logic stays testable in isolation.
func runEval(cmd *cobra.Command, opts evalOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			backplane.ClassifyError(err),
			opts.JSONOut,
		)
	}

	resp, err := postEval(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil.
	// printEvalTable's empty-surfaces branch silently emits the
	// header without any rows — without this guard, a malformed 200
	// would print a meaningless table and exit 0. Mirrors the
	// convention in `cli/internal/cmd/status.go:142`.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without an eval result payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	result := resp.JSON200

	if opts.SaveBaselinePath != "" {
		if writeErr := writeBaseline(result, opts.SaveBaselinePath); writeErr != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("write baseline: %v", writeErr)),
				opts.JSONOut,
			)
		}
	}

	regressions, err := maybeCompareBaseline(result, opts.CompareBaselinePath)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("compare baseline: %v", err)),
			opts.JSONOut,
		)
	}

	if opts.JSONOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), result); err != nil {
			return err
		}
	} else {
		printEvalTable(cmd.OutOrStdout(), result, regressions)
	}

	// Exit semantics: red verdict OR baseline regression → 1.
	if result.OverallVerdict == api.EvalResultOverallVerdictRed || len(regressions) > 0 {
		return errEvalGate
	}
	return nil
}

// errEvalGate is the sentinel returned when the eval verdict is red
// (or a baseline regression fired). main translates non-nil RunE
// errors into a non-zero exit; cobra's SilenceErrors is true on this
// command so we don't double-print the error string.
var errEvalGate = errors.New("eval gate failed")

// evalRequestBody assembles the typed POST body for /api/v1/retrieve/eval.
// The generated `api.EvalRequest` types `Surface` as `*EvalRequestSurface`
// (omitempty) and `Baseline` as `*string` (no omitempty — the backend's
// `extra="forbid"` schema treats absent + null identically). Both are
// pointer-set only when the operator supplied a non-default value so the
// wire stays minimal.
func evalRequestBody(opts evalOptions) api.EvalRequest {
	body := api.EvalRequest{}
	if opts.Surface != "" {
		surface := api.EvalRequestSurface(opts.Surface)
		body.Surface = &surface
	}
	if opts.Baseline != "" {
		baseline := opts.Baseline
		body.Baseline = &baseline
	}
	return body
}

// postEval calls POST /api/v1/retrieve/eval with the eval request
// body via the generated typed client. The 401-refresh-retry loop
// runs through retryOn401.
func postEval(
	ctx context.Context,
	backplaneURL string,
	opts evalOptions,
) (*api.EvalEndpointApiV1RetrieveEvalPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := evalRequestBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EvalEndpointApiV1RetrieveEvalPostResponse, error) {
			return authed.EvalEndpointApiV1RetrieveEvalPostWithResponse(ctx, nil, body)
		},
		func(r *api.EvalEndpointApiV1RetrieveEvalPostResponse) int { return r.StatusCode() },
	)
}

// writeBaseline serialises the EvalResult to a JSON file. Mirrors
// the backend save_baseline shape — pretty-printed (indent=2) so
// the file is human-readable when checked into git.
func writeBaseline(result *api.EvalResult, path string) error {
	pretty, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return err
	}
	pretty = append(pretty, '\n')
	return os.WriteFile(path, pretty, 0o644)
}

// maybeCompareBaseline returns the regression list when compare is
// requested, or empty when compare is not requested or there are no
// regressions.
func maybeCompareBaseline(today *api.EvalResult, path string) ([]string, error) {
	if path == "" {
		return nil, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read baseline %q: %w", path, err)
	}
	var baseline api.EvalResult
	if err := json.Unmarshal(raw, &baseline); err != nil {
		return nil, fmt.Errorf("parse baseline %q: %w", path, err)
	}
	return diffEvalResults(today, &baseline, defaultEpsilon), nil
}

// defaultEpsilon mirrors the backend RegressionEpsilon defaults
// (0.02 per metric). Kept here as a constant rather than a flag
// because the v0.2 issue body locks the value; tuning it for a
// specific PR is a v0.2.next concern.
var defaultEpsilon = struct{ PrecisionAt5, MRR, Coverage float32 }{0.02, 0.02, 0.02}

// diffEvalResults compares the per-surface metrics in *today* against
// *baseline* and returns a slice of human-readable regression
// strings. Mirrors the backend's compare_baseline behaviour exactly:
// regression = today < baseline - epsilon; surfaces only on one
// side or with zero queries are skipped.
func diffEvalResults(
	today, baseline *api.EvalResult,
	eps struct{ PrecisionAt5, MRR, Coverage float32 },
) []string {
	baseBySurface := make(map[api.SurfaceResultSurface]api.SurfaceResult, len(baseline.Surfaces))
	for _, s := range baseline.Surfaces {
		baseBySurface[s.Surface] = s
	}
	out := []string{}
	for _, t := range today.Surfaces {
		b, ok := baseBySurface[t.Surface]
		if !ok || t.QueryCount == 0 || b.QueryCount == 0 {
			continue
		}
		out = append(out, diffOneSurface(t, b, eps)...)
	}
	return out
}

func diffOneSurface(
	t, b api.SurfaceResult,
	eps struct{ PrecisionAt5, MRR, Coverage float32 },
) []string {
	out := []string{}
	for _, pair := range []struct {
		Name               string
		TodayV, BaseV, Eps float32
	}{
		{"precision_at_5", t.PrecisionAt5, b.PrecisionAt5, eps.PrecisionAt5},
		{"mrr", t.Mrr, b.Mrr, eps.MRR},
		{"coverage", t.Coverage, b.Coverage, eps.Coverage},
	} {
		if pair.TodayV < pair.BaseV-pair.Eps {
			out = append(out, fmt.Sprintf(
				"%s.%s: today=%.3f baseline=%.3f delta=%+.3f epsilon=%.3f",
				t.Surface, pair.Name, pair.TodayV, pair.BaseV,
				pair.TodayV-pair.BaseV, pair.Eps,
			))
		}
	}
	return out
}

// printEvalTable renders the EvalResult as a human-readable table
// to *w*. Compact two-line-per-surface format keeps the output
// scannable; per-query breakdown is gated on --json.
//
// `RanAt` is a `time.Time` on the generated `api.EvalResult` so the
// renderer formats it as RFC3339 to preserve the pre-migration
// `YYYY-MM-DDTHH:MM:SS±HH:MM` shape operators correlate with
// audit-log rows.
func printEvalTable(w io.Writer, r *api.EvalResult, regressions []string) {
	fmt.Fprintf(w, "Eval (%s) — overall verdict: %s\n",
		r.RanAt.Format("2006-01-02T15:04:05Z07:00"), r.OverallVerdict)
	fmt.Fprintf(w, "%-12s %-7s %10s %10s %10s %10s\n",
		"surface", "verdict", "queries", "precision@5", "mrr", "coverage")
	for _, s := range r.Surfaces {
		fmt.Fprintf(w, "%-12s %-7s %10d %10.3f %10.3f %10.3f\n",
			s.Surface, s.Verdict, s.QueryCount, s.PrecisionAt5, s.Mrr, s.Coverage)
		// Guard all four baseline metric pointers: the backend
		// SurfaceResult model declares each ``baseline_*`` field as
		// independently nullable, so a partial-population shape (e.g.
		// only precision_at_5 set) would nil-deref the CLI. The runner
		// today always populates the trio together via
		// ``_aggregate_baseline``, but the contract surfaced in the
		// result model permits any subset — defensive gate keeps the
		// CLI safe against future partial baselines.
		if s.BaselineKind != nil &&
			s.BaselinePrecisionAt5 != nil &&
			s.BaselineMrr != nil &&
			s.BaselineCoverage != nil {
			fmt.Fprintf(w, "%-12s %-7s %10s %10.3f %10.3f %10.3f\n",
				"  baseline:"+*s.BaselineKind,
				strDerefVerdict(s.BaselineVerdict),
				"-",
				*s.BaselinePrecisionAt5,
				*s.BaselineMrr,
				*s.BaselineCoverage,
			)
		}
	}
	if len(regressions) > 0 {
		fmt.Fprintf(w, "\nRegressions vs baseline (epsilon=%.3f):\n",
			defaultEpsilon.PrecisionAt5)
		for _, r := range regressions {
			fmt.Fprintf(w, "  - %s\n", r)
		}
	}
}

// strDerefVerdict is the typed-enum-aware dereference for the
// SurfaceResult.BaselineVerdict field. The generated `*SurfaceResultBaselineVerdict`
// type is a string enum; the helper returns the empty string for
// a nil pointer so the table renders the column as blank rather
// than panicking on a partial baseline population.
func strDerefVerdict(v *api.SurfaceResultBaselineVerdict) string {
	if v == nil {
		return ""
	}
	return string(*v)
}
