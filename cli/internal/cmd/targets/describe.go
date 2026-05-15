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

	"github.com/evoila/meho/cli/internal/output"
)

// Target mirrors the backend Target Pydantic model
// (backend/src/meho_backplane/targets/schemas.py L75-112). The
// shape includes the G0.3-T1.5 additions:
//
//   - Fingerprint — cached FingerprintResult from the last successful
//     probe. Server-managed; only the probe handler writes it.
//   - PreferredImplId — operator override for the G0.6 resolver's
//     tie-break ladder.
//
// Fingerprint is typed as map[string]any rather than the concrete
// FingerprintResult struct so the CLI surfaces every key the backend
// persists without needing a Go regen each time
// FingerprintResult's optional field set grows; --json round-trips
// the wire shape verbatim.
type Target struct {
	ID              string         `json:"id"`
	TenantID        string         `json:"tenant_id"`
	Name            string         `json:"name"`
	Aliases         []string       `json:"aliases"`
	Product         string         `json:"product"`
	Host            string         `json:"host"`
	Port            *int           `json:"port"`
	Fqdn            *string        `json:"fqdn"`
	SecretRef       *string        `json:"secret_ref"`
	AuthModel       string         `json:"auth_model"`
	VpnRequired     bool           `json:"vpn_required"`
	Extras          map[string]any `json:"extras"`
	Notes           *string        `json:"notes"`
	Fingerprint     map[string]any `json:"fingerprint"`
	PreferredImplID *string        `json:"preferred_impl_id"`
	CreatedAt       string         `json:"created_at"`
	UpdatedAt       string         `json:"updated_at"`
}

// newDescribeCmd returns the `meho targets describe` command.
//
// CLI shape:
//
//	meho targets describe <name|alias> \
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// The query accepts either the target's canonical name or any of its
// aliases — alias resolution happens server-side via the resolver
// (backend/src/meho_backplane/targets/resolver.py).
//
// Exit codes:
//   - 0   target rendered cleanly
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 404 "Target not found",
//     409 "Ambiguous query")
//   - 5   insufficient_role
func newDescribeCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "describe <name-or-alias>",
		Short: "Describe a single target (alias-aware)",
		Long: "describe calls GET /api/v1/targets/{name} and renders the " +
			"full Target row. The supplied argument can be the target's " +
			"canonical name or any of its aliases — resolution happens " +
			"server-side. On a 404 the CLI surfaces the near-miss " +
			"suggestions the resolver computed so the operator can fix " +
			"a typo in one shot. --json emits the raw API response.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDescribe(cmd, describeOptions{
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

type describeOptions struct {
	Query             string
	JSONOut           bool
	BackplaneOverride string
}

func runDescribe(cmd *cobra.Command, opts describeOptions) error {
	if opts.Query == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("describe requires a non-empty <name-or-alias> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	t, err := getTarget(cmd.Context(), backplaneURL, opts.Query)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), t)
	}
	printTargetSummary(cmd.OutOrStdout(), t)
	return nil
}

// buildDescribePath assembles the GET path. Exposed for unit tests
// so the URL encoding of names with special characters stays
// covered.
func buildDescribePath(query string) string {
	// PathEscape escapes path-segment-unsafe characters (spaces,
	// slashes, ?, #). Operators routinely pick names with hyphens
	// and dots; the unsafe set is rare but worth protecting.
	return "/api/v1/targets/" + pathEscape(query)
}

func getTarget(ctx context.Context, backplaneURL, query string) (*Target, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildDescribePath(query), nil)
	if err != nil {
		return nil, err
	}
	var out Target
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode target response: %w", err)
	}
	return &out, nil
}

// printTargetSummary renders the full Target as a stable, scannable
// key-value summary. Keep output discipline: one fact per line, key
// padded to a fixed width for vertical alignment. Fingerprint is
// rendered as a compact one-liner with the high-signal fields; the
// full structure is in --json.
func printTargetSummary(w io.Writer, t *Target) {
	fmt.Fprintf(w, "%-18s %s\n", "name:", t.Name)
	fmt.Fprintf(w, "%-18s %s\n", "id:", t.ID)
	fmt.Fprintf(w, "%-18s %s\n", "tenant_id:", t.TenantID)
	if len(t.Aliases) > 0 {
		fmt.Fprintf(w, "%-18s %s\n", "aliases:", strings.Join(t.Aliases, ", "))
	} else {
		fmt.Fprintf(w, "%-18s -\n", "aliases:")
	}
	fmt.Fprintf(w, "%-18s %s\n", "product:", t.Product)
	fmt.Fprintf(w, "%-18s %s\n", "host:", t.Host)
	if t.Port != nil {
		fmt.Fprintf(w, "%-18s %d\n", "port:", *t.Port)
	}
	if t.Fqdn != nil && *t.Fqdn != "" {
		fmt.Fprintf(w, "%-18s %s\n", "fqdn:", *t.Fqdn)
	}
	fmt.Fprintf(w, "%-18s %s\n", "auth_model:", t.AuthModel)
	fmt.Fprintf(w, "%-18s %t\n", "vpn_required:", t.VpnRequired)
	if t.SecretRef != nil && *t.SecretRef != "" {
		fmt.Fprintf(w, "%-18s %s\n", "secret_ref:", *t.SecretRef)
	}
	if t.PreferredImplID != nil && *t.PreferredImplID != "" {
		fmt.Fprintf(w, "%-18s %s\n", "preferred_impl_id:", *t.PreferredImplID)
	} else {
		fmt.Fprintf(w, "%-18s -\n", "preferred_impl_id:")
	}
	fmt.Fprintf(w, "%-18s %s\n", "fingerprint:", formatFingerprint(t.Fingerprint))
	if t.Notes != nil && *t.Notes != "" {
		fmt.Fprintf(w, "%-18s %s\n", "notes:", *t.Notes)
	}
	if len(t.Extras) > 0 {
		fmt.Fprintf(w, "%-18s %s\n", "extras:", formatExtras(t.Extras))
	}
	fmt.Fprintf(w, "%-18s %s\n", "created_at:", t.CreatedAt)
	fmt.Fprintf(w, "%-18s %s\n", "updated_at:", t.UpdatedAt)
}

// formatFingerprint renders the cached fingerprint as a one-line
// summary "<vendor>/<product> <version> (probed_at <ts>, method <m>,
// reachable=<bool>)" when set; "(none — never probed)" otherwise.
// Full key set is in --json. Keep the high-signal fields visible
// without overflowing the line width.
func formatFingerprint(fp map[string]any) string {
	if len(fp) == 0 {
		return "(none — never probed)"
	}
	vendor, _ := fp["vendor"].(string)
	product, _ := fp["product"].(string)
	version, _ := fp["version"].(string)
	probedAt, _ := fp["probed_at"].(string)
	method, _ := fp["probe_method"].(string)
	reachable, _ := fp["reachable"].(bool)
	head := strings.TrimSpace(vendor + "/" + product)
	if version != "" {
		head = head + " " + version
	}
	return fmt.Sprintf("%s (probed_at=%s method=%s reachable=%t)",
		head, probedAt, method, reachable)
}

// formatExtras renders the extras map as a `key=value, key=value`
// list with keys sorted for deterministic output (tests + diffs).
// Non-scalar values land as their JSON representation.
func formatExtras(extras map[string]any) string {
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

// formatScalar renders an extras value compactly. Scalars print
// directly; objects/lists round-trip through json.Marshal so a
// pretty JSON form is at least readable on one line.
func formatScalar(v any) string {
	switch v := v.(type) {
	case string:
		return v
	case bool:
		return fmt.Sprintf("%t", v)
	case float64:
		// json.Unmarshal decodes JSON numbers to float64 by default.
		// Render integers without trailing zeros so version-like keys
		// (extras["build"]) don't surface as "12345.000000".
		if v == float64(int64(v)) {
			return fmt.Sprintf("%d", int64(v))
		}
		return fmt.Sprintf("%g", v)
	default:
		blob, err := json.Marshal(v)
		if err != nil {
			return fmt.Sprintf("%v", v)
		}
		return string(blob)
	}
}
