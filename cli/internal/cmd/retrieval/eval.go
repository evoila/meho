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
	"net/url"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// EvalQueryResult mirrors the backend Pydantic shape one-for-one so
// the JSON unmarshal is straight field-for-field. Kept hand-written
// (rather than oapi-codegen-generated) because the Go regen pass
// for the new endpoint runs in a follow-up PR — the shape is small
// and locked by the matching backend test, so a minor schema drift
// on either side surfaces in either the Go test or the backend test.
type EvalQueryResult struct {
	Query                  string   `json:"query"`
	ExpectedHits           []string `json:"expected_hits"`
	MehoHits               []string `json:"meho_hits"`
	PrecisionAt5           float64  `json:"precision_at_5"`
	ReciprocalRank         float64  `json:"reciprocal_rank"`
	CoverageAt5            float64  `json:"coverage_at_5"`
	BaselineHits           []string `json:"baseline_hits,omitempty"`
	BaselinePrecisionAt5   *float64 `json:"baseline_precision_at_5,omitempty"`
	BaselineReciprocalRank *float64 `json:"baseline_reciprocal_rank,omitempty"`
	BaselineCoverageAt5    *float64 `json:"baseline_coverage_at_5,omitempty"`
}

// EvalSurfaceResult mirrors the backend SurfaceResult model.
type EvalSurfaceResult struct {
	Surface              string            `json:"surface"`
	QueryCount           int               `json:"query_count"`
	PrecisionAt5         float64           `json:"precision_at_5"`
	MRR                  float64           `json:"mrr"`
	Coverage             float64           `json:"coverage"`
	Verdict              string            `json:"verdict"`
	BaselineKind         *string           `json:"baseline_kind,omitempty"`
	BaselinePrecisionAt5 *float64          `json:"baseline_precision_at_5,omitempty"`
	BaselineMRR          *float64          `json:"baseline_mrr,omitempty"`
	BaselineCoverage     *float64          `json:"baseline_coverage,omitempty"`
	BaselineVerdict      *string           `json:"baseline_verdict,omitempty"`
	Queries              []EvalQueryResult `json:"queries"`
}

// EvalResult mirrors the backend EvalResult model — the top-level
// shape returned by POST /api/v1/retrieve/eval.
type EvalResult struct {
	RanAt          string              `json:"ran_at"`
	Surfaces       []EvalSurfaceResult `json:"surfaces"`
	OverallVerdict string              `json:"overall_verdict"`
	Thresholds     json.RawMessage     `json:"thresholds"`
}

// evalRequest is the POST body shape; mirrors the backend EvalRequest.
type evalRequest struct {
	Surface  string `json:"surface"`
	Baseline string `json:"baseline,omitempty"`
}

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
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			classifyBackplaneError(err),
			opts.JSONOut,
		)
	}

	result, err := postEval(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}

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
	if result.OverallVerdict == "red" || len(regressions) > 0 {
		return errEvalGate
	}
	return nil
}

// errEvalGate is the sentinel returned when the eval verdict is red
// (or a baseline regression fired). main translates non-nil RunE
// errors into a non-zero exit; cobra's SilenceErrors is true on this
// command so we don't double-print the error string.
var errEvalGate = errors.New("eval gate failed")

// postEval calls POST /api/v1/retrieve/eval with the eval request body
// and decodes the JSON response into an EvalResult. The function
// transparently retries on 401 once after a token refresh — same
// shape as api.AuthedClient.GetHealth.
func postEval(ctx context.Context, backplaneURL string, opts evalOptions) (*EvalResult, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	httpClient := authed.HTTPClient()
	bearer := authed.AccessToken()
	if bearer == "" {
		return nil, errors.New("meho: stored token has no access_token")
	}

	body, err := json.Marshal(evalRequest{
		Surface:  opts.Surface,
		Baseline: opts.Baseline,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal eval request: %w", err)
	}

	resp, err := postEvalWithBearer(ctx, httpClient, backplaneURL, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		// One-shot refresh + retry, mirroring api.AuthedClient.GetHealth.
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close()
			return nil, rerr
		}
		resp.Body.Close()
		bearer = authed.AccessToken()
		resp, err = postEvalWithBearer(ctx, httpClient, backplaneURL, bearer, body)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}

	var out EvalResult
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode eval response: %w", err)
	}
	return &out, nil
}

// postEvalWithBearer issues the actual POST request with the
// supplied bearer token. Split out so the 401-retry path can re-use
// the body bytes without re-marshalling.
func postEvalWithBearer(
	ctx context.Context,
	client *http.Client,
	backplaneURL, bearer string,
	body []byte,
) (*http.Response, error) {
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost,
		backplaneURL+"/api/v1/retrieve/eval",
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, fmt.Errorf("build eval request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	return client.Do(req)
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" (→ auth_expired exit
// code 2 — the right fix is `meho login`) from URL-parse failures
// (→ unexpected exit code 4 — the right fix is correcting argv).
// Wrapped via %w so errors.Is(err, auth.ErrConfigNotFound) still
// matches; the user-facing message points at the actionable fix.
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane re-implements the host-trimming + parsing rules
// the cmd package's resolveBackplaneURL applies. We can't import
// cmd from a subpackage without an import cycle (cmd/root.go grafts
// this package onto the tree), so the resolution shape is mirrored
// here.
func resolveBackplane(override string) (string, error) {
	if override != "" {
		return normaliseURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", &errNoBackplaneConfigured{inner: err}
		}
		return "", err
	}
	return normaliseURL(cfg.BackplaneURL)
}

// classifyBackplaneError maps a resolveBackplane error to the right
// output.StructuredError category. The documented exit codes (see
// runEval header) name 2 = auth_expired vs 3 = unreachable vs 4 =
// unexpected; collapsing every error to auth_expired (the previous
// shape) sends operators down the `meho login` path when the actual
// cause was a typo in --backplane or a stale config file. Classify:
//
//   - ErrConfigNotFound (or our wrapper) → auth_expired: operator
//     hasn't run `meho login` yet, that's exactly the fix.
//   - everything else (parse errors, file-system errors from
//     LoadConfig) → unexpected: the cause is operator argv or a
//     corrupt config, not an expired token.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail
// fast on garbage input. Mirrors normalizeBackplaneURL in
// cmd/status.go (kept independent because of the import-cycle
// concern noted on resolveBackplane).
func normaliseURL(s string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(s), "/")
	if trimmed == "" {
		return "", errors.New("backplane URL is empty")
	}
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid backplane URL %q: %w", s, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
}

// renderRequestError translates an error from postEval into the
// right output.RenderError category (auth_expired vs unreachable
// vs unexpected). Kept separate so the runEval body stays focused
// on orchestration, not error classification.
func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// writeBaseline serialises the EvalResult to a JSON file. Mirrors
// the backend save_baseline shape — pretty-printed (indent=2) so
// the file is human-readable when checked into git.
func writeBaseline(result *EvalResult, path string) error {
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
func maybeCompareBaseline(today *EvalResult, path string) ([]string, error) {
	if path == "" {
		return nil, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read baseline %q: %w", path, err)
	}
	var baseline EvalResult
	if err := json.Unmarshal(raw, &baseline); err != nil {
		return nil, fmt.Errorf("parse baseline %q: %w", path, err)
	}
	return diffEvalResults(today, &baseline, defaultEpsilon), nil
}

// defaultEpsilon mirrors the backend RegressionEpsilon defaults
// (0.02 per metric). Kept here as a constant rather than a flag
// because the v0.2 issue body locks the value; tuning it for a
// specific PR is a v0.2.next concern.
var defaultEpsilon = struct{ PrecisionAt5, MRR, Coverage float64 }{0.02, 0.02, 0.02}

// diffEvalResults compares the per-surface metrics in *today* against
// *baseline* and returns a slice of human-readable regression
// strings. Mirrors the backend's compare_baseline behaviour exactly:
// regression = today < baseline - epsilon; surfaces only on one
// side or with zero queries are skipped.
func diffEvalResults(
	today, baseline *EvalResult,
	eps struct{ PrecisionAt5, MRR, Coverage float64 },
) []string {
	baseBySurface := make(map[string]EvalSurfaceResult, len(baseline.Surfaces))
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
	t, b EvalSurfaceResult,
	eps struct{ PrecisionAt5, MRR, Coverage float64 },
) []string {
	out := []string{}
	for _, pair := range []struct {
		Name               string
		TodayV, BaseV, Eps float64
	}{
		{"precision_at_5", t.PrecisionAt5, b.PrecisionAt5, eps.PrecisionAt5},
		{"mrr", t.MRR, b.MRR, eps.MRR},
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
func printEvalTable(w io.Writer, r *EvalResult, regressions []string) {
	fmt.Fprintf(w, "Eval (%s) — overall verdict: %s\n", r.RanAt, r.OverallVerdict)
	fmt.Fprintf(w, "%-12s %-7s %10s %10s %10s %10s\n",
		"surface", "verdict", "queries", "precision@5", "mrr", "coverage")
	for _, s := range r.Surfaces {
		fmt.Fprintf(w, "%-12s %-7s %10d %10.3f %10.3f %10.3f\n",
			s.Surface, s.Verdict, s.QueryCount, s.PrecisionAt5, s.MRR, s.Coverage)
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
			s.BaselineMRR != nil &&
			s.BaselineCoverage != nil {
			fmt.Fprintf(w, "%-12s %-7s %10s %10.3f %10.3f %10.3f\n",
				"  baseline:"+*s.BaselineKind,
				strDeref(s.BaselineVerdict),
				"-",
				*s.BaselinePrecisionAt5,
				*s.BaselineMRR,
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

func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
