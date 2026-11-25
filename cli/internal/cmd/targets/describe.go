// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDescribeCmd returns the `meho targets describe` command.
//
// CLI shape:
//
//	meho targets describe <name, alias or id> \
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// The query accepts the target's canonical name, any of its aliases,
// or its id — the lookup happens server-side (name, then alias, then
// id; see backend/src/meho_backplane/api/v1/targets.py).
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
		Use:   "describe <name, alias or id>",
		Short: "Describe a single target by name, alias or id",
		Long: "describe calls GET /api/v1/targets/{name} and renders the " +
			"full Target row. The supplied argument can be the target's " +
			"canonical name, any of its aliases, or its id — resolution happens " +
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
			output.Unexpected("describe requires a non-empty <name, alias or id> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getTarget(cmd.Context(), backplaneURL, opts.Query)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	t := resp.JSON200
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), t)
	}
	printTargetSummary(cmd.OutOrStdout(), t)
	return nil
}

func getTarget(
	ctx context.Context,
	backplaneURL, query string,
) (*api.DescribeTargetApiV1TargetsNameGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.DescribeTargetApiV1TargetsNameGetResponse, error) {
			// The typed client path-escapes `query` internally
			// (matching url.PathEscape) before substituting into
			// /api/v1/targets/{name}.
			return authed.DescribeTargetApiV1TargetsNameGetWithResponse(ctx, query, nil)
		},
		func(r *api.DescribeTargetApiV1TargetsNameGetResponse) int { return r.StatusCode() },
	)
}

// printTargetSummary renders the full Target as a stable, scannable
// key-value summary. Keep output discipline: one fact per line, key
// padded to a fixed width for vertical alignment. Fingerprint is
// rendered as a compact one-liner with the high-signal fields; the
// full structure is in --json.
func printTargetSummary(w io.Writer, t *api.Target) {
	fmt.Fprintf(w, "%-18s %s\n", "name:", output.SafeText(t.Name))
	fmt.Fprintf(w, "%-18s %s\n", "id:", t.Id.String())
	fmt.Fprintf(w, "%-18s %s\n", "tenant_id:", t.TenantId.String())
	if len(t.Aliases) > 0 {
		fmt.Fprintf(w, "%-18s %s\n", "aliases:", output.SafeText(strings.Join(t.Aliases, ", ")))
	} else {
		fmt.Fprintf(w, "%-18s -\n", "aliases:")
	}
	fmt.Fprintf(w, "%-18s %s\n", "product:", output.SafeText(t.Product))
	fmt.Fprintf(w, "%-18s %s\n", "host:", output.SafeText(t.Host))
	if t.Port != nil {
		fmt.Fprintf(w, "%-18s %d\n", "port:", *t.Port)
	}
	if t.Fqdn != nil && *t.Fqdn != "" {
		fmt.Fprintf(w, "%-18s %s\n", "fqdn:", output.SafeText(*t.Fqdn))
	}
	fmt.Fprintf(w, "%-18s %s\n", "auth_model:", string(t.AuthModel))
	fmt.Fprintf(w, "%-18s %t\n", "vpn_required:", t.VpnRequired)
	if t.SecretRef != nil && *t.SecretRef != "" {
		if t.SecretRefDerived != nil && *t.SecretRefDerived {
			fmt.Fprintf(w, "%-18s %s (built from the target name; store the credential there or set --secret-ref)\n", "secret_ref:", output.SafeText(*t.SecretRef))
		} else {
			fmt.Fprintf(w, "%-18s %s\n", "secret_ref:", output.SafeText(*t.SecretRef))
		}
	}
	if t.SecretFieldMap != nil && len(*t.SecretFieldMap) > 0 {
		fmt.Fprintf(w, "%-18s %s\n", "secret_field_map:", output.SafeText(formatSecretFieldMap(*t.SecretFieldMap)))
	}
	if t.PreferredImplId != nil && *t.PreferredImplId != "" {
		fmt.Fprintf(w, "%-18s %s\n", "preferred_impl_id:", output.SafeText(*t.PreferredImplId))
	} else {
		fmt.Fprintf(w, "%-18s -\n", "preferred_impl_id:")
	}
	fmt.Fprintf(w, "%-18s %s\n", "fingerprint:", output.SafeText(formatFingerprint(t.Fingerprint)))
	if t.Notes != nil && *t.Notes != "" {
		fmt.Fprintf(w, "%-18s %s\n", "notes:", output.SafeText(*t.Notes))
	}
	if len(t.Extras) > 0 {
		fmt.Fprintf(w, "%-18s %s\n", "extras:", output.SafeText(formatExtras(t.Extras)))
	}
	if line := keycloakRealmsLine(t); line != "" {
		fmt.Fprintf(w, "%-18s %s\n", "realms:", output.SafeText(line))
	}
	// Render timestamps in the same RFC3339Z shape the backend's
	// model_dump(mode="json") emits, so --json output and the human
	// view show byte-identical strings.
	fmt.Fprintf(w, "%-18s %s\n", "created_at:", t.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	fmt.Fprintf(w, "%-18s %s\n", "updated_at:", t.UpdatedAt.UTC().Format("2006-01-02T15:04:05Z"))
}

// keycloakRealmsLine shows the admin realm and the managed realm of a
// Keycloak target side by side. The target works on the managed realm only
// and signs in to the admin realm (`master` when none is set). It is empty
// for any other product.
func keycloakRealmsLine(t *api.Target) string {
	if !strings.EqualFold(strings.TrimSpace(t.Product), "keycloak") {
		return ""
	}
	admin, _ := t.Extras["admin_realm"].(string)
	if admin == "" {
		admin = "master"
	}
	managed, _ := t.Extras["managed_realm"].(string)
	if managed == "" {
		managed = "(not set)"
	}
	return fmt.Sprintf("admin=%s, managed=%s", admin, managed)
}

// formatSecretFieldMap renders the secret field map as a sorted
// `connector_field=source_field, ...` list (field names only).
func formatSecretFieldMap(m map[string]string) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = k + "=" + m[k]
	}
	return strings.Join(parts, ", ")
}

// formatFingerprint renders the cached fingerprint as a one-line
// summary "<vendor>/<product> <version> (probed_at <ts>, method <m>,
// reachable=<bool>)" when set; "(none — never probed)" otherwise.
// Full key set is in --json. Keep the high-signal fields visible
// without overflowing the line width.
func formatFingerprint(fp *map[string]interface{}) string {
	if fp == nil || len(*fp) == 0 {
		return "(none — never probed)"
	}
	m := *fp
	vendor, _ := m["vendor"].(string)
	product, _ := m["product"].(string)
	version, _ := m["version"].(string)
	probedAt, _ := m["probed_at"].(string)
	method, _ := m["probe_method"].(string)
	reachable, _ := m["reachable"].(bool)
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
func formatExtras(extras map[string]interface{}) string {
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
