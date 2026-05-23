// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// FingerprintResult mirrors the backend FingerprintResult Pydantic
// model (backend/src/meho_backplane/connectors/schemas.py L96-118).
// The shape matches the response of
// POST /api/v1/targets/{name}/probe. Per the 2026-05-14 amendment to
// Initiative #224 (G0.3-T1.5 / Task #477) the backend now returns
// FingerprintResult (not ProbeResult) and persists it to
// targets.fingerprint so the G0.6 resolver can read it without
// re-probing.
//
// Optional fields (version / build / edition) are `*string` so the
// CLI can distinguish "connector explicitly omitted the field" from
// "connector reported an empty string".
type FingerprintResult struct {
	Vendor      string         `json:"vendor"`
	Product     string         `json:"product"`
	Version     *string        `json:"version"`
	Build       *string        `json:"build"`
	Edition     *string        `json:"edition"`
	Reachable   bool           `json:"reachable"`
	ProbedAt    string         `json:"probed_at"`
	ProbeMethod string         `json:"probe_method"`
	Extras      map[string]any `json:"extras,omitempty"`
}

// newProbeCmd returns the `meho targets probe` command.
//
// CLI shape:
//
//	meho targets probe <name|alias> \
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// The backend calls Connector.fingerprint(target) and persists the
// result to `targets.fingerprint` on success — so a subsequent
// `meho targets describe` surfaces the cached snapshot without
// reprobing. On 501 (no connector registered for the target's
// product yet) the column is *not* touched; any previously-cached
// fingerprint survives.
//
// Exit codes:
//   - 0   probe completed; FingerprintResult rendered
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404 "Target not found",
//     501 "no connector registered for product=X yet", and
//     500s when the connector raised — the backend propagates
//     the exception per #477's accepted trade-off)
//   - 5   insufficient_role
func newProbeCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "probe <name-or-alias>",
		Short: "Probe a target's connector and refresh its fingerprint",
		Long: "probe calls POST /api/v1/targets/{name}/probe. The " +
			"backend invokes the connector registered for the target's " +
			"product, returns the FingerprintResult, and persists it to " +
			"`targets.fingerprint` for the G0.6 resolver to read later " +
			"without re-probing. On 501 (no connector yet for the " +
			"target's product) the CLI surfaces a friendly pointer to " +
			"Goal G3 where the per-product connector work lives. On a " +
			"connector exception the backend propagates a 500; the CLI " +
			"renders it as unexpected_response with the underlying " +
			"detail so operators can decide whether to retry, file a " +
			"connector bug, or check connectivity.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProbe(cmd, probeOptions{
				Query:             args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type probeOptions struct {
	Query             string
	JSONOut           bool
	BackplaneOverride string
}

func runProbe(cmd *cobra.Command, opts probeOptions) error {
	if opts.Query == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("probe requires a non-empty <name-or-alias> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	fp, err := postProbe(cmd.Context(), backplaneURL, opts.Query)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), fp)
	}
	printFingerprint(cmd.OutOrStdout(), fp)
	return nil
}

// buildProbePath assembles the POST path. Exposed for unit tests so
// the URL encoding of names with special characters stays covered.
func buildProbePath(query string) string {
	return "/api/v1/targets/" + pathEscape(query) + "/probe"
}

func postProbe(ctx context.Context, backplaneURL, query string) (*FingerprintResult, error) {
	// Empty body — the route reads only the path-param name + the
	// authed operator. Pass nil so doAuthedRequest skips the
	// Content-Type/Content-Length stamp and lets the server route
	// see an actual zero-length body.
	raw, err := doAuthedRequest(ctx, backplaneURL, "POST", buildProbePath(query), nil)
	if err != nil {
		return nil, err
	}
	var out FingerprintResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode fingerprint response: %w", err)
	}
	return &out, nil
}

// printFingerprint renders a FingerprintResult as a key-value
// summary. Same shape as the describe command's fingerprint line,
// but with full multi-line breakdown — operators run `probe` for
// the explicit "what did this connector report just now?" answer
// and want every field visible.
func printFingerprint(w io.Writer, fp *FingerprintResult) {
	fmt.Fprintf(w, "%-14s %s\n", "vendor:", fp.Vendor)
	fmt.Fprintf(w, "%-14s %s\n", "product:", fp.Product)
	if v := strDeref(fp.Version); v != "" {
		fmt.Fprintf(w, "%-14s %s\n", "version:", v)
	}
	if b := strDeref(fp.Build); b != "" {
		fmt.Fprintf(w, "%-14s %s\n", "build:", b)
	}
	if e := strDeref(fp.Edition); e != "" {
		fmt.Fprintf(w, "%-14s %s\n", "edition:", e)
	}
	fmt.Fprintf(w, "%-14s %t\n", "reachable:", fp.Reachable)
	fmt.Fprintf(w, "%-14s %s\n", "probed_at:", fp.ProbedAt)
	fmt.Fprintf(w, "%-14s %s\n", "probe_method:", fp.ProbeMethod)
	if len(fp.Extras) > 0 {
		fmt.Fprintf(w, "%-14s %s\n", "extras:", formatProbeExtras(fp.Extras))
	}
}

// formatProbeExtras renders the extras map deterministically. Same
// shape as describe.go::formatExtras; duplicated here to avoid an
// implicit coupling between the two verbs' rendering rules.
func formatProbeExtras(extras map[string]any) string {
	if len(extras) == 0 {
		return "-"
	}
	keys := make([]string, 0, len(extras))
	for k := range extras {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%s", k, formatScalar(extras[k])))
	}
	return strings.Join(parts, ", ")
}
