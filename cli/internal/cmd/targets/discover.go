// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// CandidateHint mirrors the backend CandidateHint Pydantic model
// (backend/src/meho_backplane/connectors/schemas.py L274). One
// potentially-reachable target a connector inferred from a seed
// (a vCenter exposing its managed ESXi hosts, a kubeconfig listing
// peer cluster contexts, …). `Evidence` is the connector's debugging
// payload — whatever made it think the candidate exists; `Confidence`
// is its probe-vs-curation self-assessment. Hand-written rather than
// aliased to a generated type for the same decoupling reason the
// other target shapes (TargetSummary etc.) document.
type CandidateHint struct {
	Name       string         `json:"name"`
	Host       string         `json:"host"`
	Port       *int           `json:"port"`
	Evidence   map[string]any `json:"evidence"`
	Confidence string         `json:"confidence"`
}

// SkippedConnector mirrors the backend SkippedConnector Pydantic
// model (backend/src/meho_backplane/api/v1/targets.py). One connector
// that contributed nothing for the product — clean-but-empty
// ("no candidates") or errored (exception class + message). One bad
// connector never fails the sweep server-side; the CLI surfaces the
// skip list so the operator sees why a candidate they expected is
// missing.
type SkippedConnector struct {
	Name   string `json:"name"`
	Reason string `json:"reason"`
}

// DiscoverResult mirrors the backend aggregate
// (backend/src/meho_backplane/api/v1/targets.py): merged candidate
// list across every connector registered for the product, plus the
// connectors that contributed nothing. The verb never auto-creates
// `targets` rows — the operator reviews `discovered` and runs
// `meho targets create` (Initiative #363: auto-registration is
// v0.2.next).
type DiscoverResult struct {
	Discovered []CandidateHint    `json:"discovered"`
	Skipped    []SkippedConnector `json:"skipped"`
}

// newDiscoverCmd returns the `meho targets discover <product>` command.
//
//	meho targets discover <product> [--seed-target <name>]
//	  [--json] [--backplane <url>]
//	# GET /api/v1/targets/discover?product=<p>&seed_target=...
//
// The G0.3-deferred verb (#256 explicitly defers it to G9.1-T6).
// Iterates every connector registered for <product>, calling each
// connector's list_candidates discovery hook, and surfaces the merged
// candidate list for the operator to review before running
// `meho targets create`. --seed-target scopes discovery to one
// already-registered target's reach (e.g. peer clusters in the same
// kubeconfig); it is resolved tenant-scoped server-side so a 404 on a
// cross-tenant seed name is identical to a typo.
//
// Exit codes mirror the sibling targets verbs:
//   - 0   discovery ran (including the zero-candidate case — an empty
//     lab is operationally meaningful, never 404)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (404 no_target on a bad --seed-target,
//     409 ambiguous_target, malformed JSON, …)
//   - 5   insufficient_role (403; backend names the required role)
func newDiscoverCmd() *cobra.Command {
	var (
		seedTarget        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "discover <product>",
		Short: "Discover candidate targets a connector can reach for a product",
		Long: "discover calls GET /api/v1/targets/discover and lists " +
			"potentially-reachable targets every connector registered " +
			"for <product> inferred but that are not yet registered. " +
			"The verb is read-only — it never creates `targets` rows; " +
			"the operator reviews the candidates and runs " +
			"`meho targets create`. --seed-target scopes discovery to " +
			"one already-registered target's reach (resolved tenant-" +
			"scoped server-side; a cross-tenant seed 404s like a typo). " +
			"Connectors that contributed nothing are listed under " +
			"SKIPPED with the reason so an expected-but-missing " +
			"candidate is explainable.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDiscover(cmd, discoverOptions{
				Product:           args[0],
				SeedTarget:        seedTarget,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&seedTarget, "seed-target", "",
		"scope discovery to one already-registered target's reach (resolved tenant-scoped)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human tables")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type discoverOptions struct {
	Product           string
	SeedTarget        string
	JSONOut           bool
	BackplaneOverride string
}

func runDiscover(cmd *cobra.Command, opts discoverOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, err := getDiscover(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printDiscoverTables(cmd.OutOrStdout(), result)
	return nil
}

// buildDiscoverPath assembles the GET /api/v1/targets/discover query
// string. `product` is required (the cobra ExactArgs(1) guarantees a
// value); `seed_target` is omitted when unset. The path-segment
// `/discover` is matched as the dedicated route ahead of the
// parametrised describe route server-side. Exposed for unit tests.
func buildDiscoverPath(opts discoverOptions) string {
	q := url.Values{}
	q.Set("product", opts.Product)
	if opts.SeedTarget != "" {
		q.Set("seed_target", opts.SeedTarget)
	}
	return "/api/v1/targets/discover?" + q.Encode()
}

func getDiscover(ctx context.Context, backplaneURL string, opts discoverOptions) (*DiscoverResult, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildDiscoverPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out DiscoverResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode discover response: %w", err)
	}
	return &out, nil
}

// printDiscoverTables renders the discovered candidates as a
// NAME / HOST / PORT / CONFIDENCE table, then the skipped connectors
// as NAME / REASON so the operator can see both what was found and
// why an expected candidate is absent. Zero candidates renders the
// no-candidates line (operationally meaningful — an empty lab) rather
// than a bare header.
func printDiscoverTables(w io.Writer, r *DiscoverResult) {
	if len(r.Discovered) == 0 {
		fmt.Fprintln(w, "no candidate targets discovered for this product")
	} else {
		fmt.Fprintf(w, "%-30s %-30s %-6s %s\n", "NAME", "HOST", "PORT", "CONFIDENCE")
		for _, c := range r.Discovered {
			port := "-"
			if c.Port != nil {
				port = fmt.Sprintf("%d", *c.Port)
			}
			fmt.Fprintf(w, "%-30s %-30s %-6s %s\n",
				truncate(c.Name, 30),
				truncate(c.Host, 30),
				port,
				truncate(c.Confidence, 10),
			)
		}
	}
	if len(r.Skipped) > 0 {
		fmt.Fprintln(w)
		fmt.Fprintf(w, "%-30s %s\n", "SKIPPED", "REASON")
		for _, s := range r.Skipped {
			fmt.Fprintf(w, "%-30s %s\n", truncate(s.Name, 30), truncate(s.Reason, 80))
		}
	}
	if len(r.Discovered) > 0 {
		fmt.Fprintln(w, strings.TrimSpace(
			"\nreview candidates, then register with `meho targets create` (auto-registration is v0.2.next)"))
	}
}
