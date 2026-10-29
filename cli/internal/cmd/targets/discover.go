// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDiscoverCmd returns the `meho targets discover <product>` command.
//
//	meho targets discover <product> [--seed-target <name>]
//	  [--json | --import-yaml] [--backplane <url>]
//	# GET /api/v1/targets/discover?product=<p>&seed_target=...
//
// The G0.3-deferred verb (#256 explicitly defers it to G9.1-T6).
// Iterates every connector registered for <product>, calling each
// connector's list_candidates discovery hook, and surfaces the merged
// candidate list for the operator to review before registering
// them with `meho targets import`. --seed-target scopes discovery to one
// already-registered target's reach (e.g. peer clusters in the same
// kubeconfig); it is resolved tenant-scoped server-side so a 404 on a
// cross-tenant seed name is identical to a typo. --import-yaml prints the
// candidates as a `targets:` file that `meho targets import` reads.
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
		importYAML        bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "discover <product>",
		Short: "Discover candidate targets a connector can reach for a product",
		Long: "discover calls GET /api/v1/targets/discover and lists " +
			"potentially-reachable targets every connector registered " +
			"for <product> inferred but that are not yet registered. " +
			"The verb is read-only — it never creates `targets` rows; " +
			"the operator reviews the candidates and registers them " +
			"with `meho targets import`. --seed-target scopes discovery to " +
			"one already-registered target's reach (resolved tenant-" +
			"scoped server-side; a cross-tenant seed 404s like a typo). " +
			"Connectors that contributed nothing are listed under " +
			"SKIPPED with the reason so an expected-but-missing " +
			"candidate is explainable. --import-yaml prints the " +
			"candidates as a file for `meho targets import` instead " +
			"(the skipped connectors go to stderr).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDiscover(cmd, discoverOptions{
				Product:           args[0],
				SeedTarget:        seedTarget,
				JSONOut:           jsonOut,
				ImportYAML:        importYAML,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&seedTarget, "seed-target", "",
		"scope discovery to one already-registered target's reach (resolved tenant-scoped)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human tables")
	cmd.Flags().BoolVar(&importYAML, "import-yaml", false,
		"print the candidates as a file for `meho targets import` instead of the tables")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.MarkFlagsMutuallyExclusive("json", "import-yaml")
	return cmd
}

type discoverOptions struct {
	Product           string
	SeedTarget        string
	JSONOut           bool
	ImportYAML        bool
	BackplaneOverride string
}

func runDiscover(cmd *cobra.Command, opts discoverOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getDiscover(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	result := resp.JSON200
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	if opts.ImportYAML {
		return printImportYAML(cmd.OutOrStdout(), cmd.ErrOrStderr(), opts.Product, result)
	}
	printDiscoverTables(cmd.OutOrStdout(), result)
	return nil
}

// buildDiscoverParams assembles the typed query-parameter struct for
// GET /api/v1/targets/discover. `product` is required (the cobra
// ExactArgs(1) guarantees a value); `seed_target` is omitted when
// unset. The path-segment `/discover` is matched as the dedicated
// route ahead of the parametrised describe route server-side.
// Exposed for unit tests.
func buildDiscoverParams(opts discoverOptions) *api.DiscoverTargetsApiV1TargetsDiscoverGetParams {
	params := &api.DiscoverTargetsApiV1TargetsDiscoverGetParams{
		Product: opts.Product,
	}
	if opts.SeedTarget != "" {
		s := opts.SeedTarget
		params.SeedTarget = &s
	}
	return params
}

func getDiscover(
	ctx context.Context,
	backplaneURL string,
	opts discoverOptions,
) (*api.DiscoverTargetsApiV1TargetsDiscoverGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := buildDiscoverParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.DiscoverTargetsApiV1TargetsDiscoverGetResponse, error) {
			return authed.DiscoverTargetsApiV1TargetsDiscoverGetWithResponse(ctx, params)
		},
		func(r *api.DiscoverTargetsApiV1TargetsDiscoverGetResponse) int { return r.StatusCode() },
	)
}

// printDiscoverTables renders the discovered candidates as a
// NAME / HOST / PORT / CONFIDENCE table, then the skipped connectors
// as NAME / REASON so the operator can see both what was found and
// why an expected candidate is absent. Zero candidates renders the
// no-candidates line (operationally meaningful — an empty lab) rather
// than a bare header.
func printDiscoverTables(w io.Writer, r *api.TargetsDiscoverResult) {
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
				truncate(string(c.Confidence), 10),
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
			"\nreview candidates, then register with `meho targets import` (one-shot auto-registration is not yet available)"))
	}
}

// importEntry is one entry of the `targets:` file `meho targets import`
// reads: the target fields a candidate carries, plus the product.
type importEntry struct {
	Name      string         `yaml:"name"`
	Product   string         `yaml:"product"`
	Host      string         `yaml:"host"`
	Port      *int           `yaml:"port,omitempty"`
	SecretRef *string        `yaml:"secret_ref,omitempty"`
	Extras    map[string]any `yaml:"extras,omitempty"`
}

// printImportYAML writes the candidates as a `targets:` file that
// `meho targets import` reads. The skipped connectors (and the note
// when nothing was found) go to errOut, so out stays a clean file.
func printImportYAML(out, errOut io.Writer, product string, r *api.TargetsDiscoverResult) error {
	entries := make([]importEntry, 0, len(r.Discovered))
	for _, c := range r.Discovered {
		entry := importEntry{Name: c.Name, Product: product, Host: c.Host, Port: c.Port, SecretRef: c.SecretRef}
		if c.Extras != nil && len(*c.Extras) > 0 {
			entry.Extras = *c.Extras
		}
		entries = append(entries, entry)
	}
	if len(entries) == 0 {
		fmt.Fprintln(errOut, "no candidate targets discovered for this product")
	}
	for _, s := range r.Skipped {
		fmt.Fprintf(errOut, "skipped %s: %s\n", s.Name, s.Reason)
	}
	encoder := yaml.NewEncoder(out)
	encoder.SetIndent(2)
	if err := encoder.Encode(struct {
		Targets []importEntry `yaml:"targets"`
	}{Targets: entries}); err != nil {
		return err
	}
	return encoder.Close()
}
