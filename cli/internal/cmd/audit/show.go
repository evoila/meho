// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

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

// newShowCmd returns the `meho audit show` command.
//
// CLI shape (per issue #467):
//
//	meho audit show <audit-id> [--json] [--backplane <url>]
//
// 404 from the backend surfaces as "audit row not found". The
// cross-tenant probe always reads as 404 — the substrate's tenant
// WHERE clause yields zero rows for an audit_id outside the
// operator's tenant, and the route returns 404 (not 403) so the
// existence of an audit row in another tenant never leaks through
// status-code discrimination.
//
// Exit codes:
//   - 0   row rendered cleanly
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (includes 404 not-found and 422
//     malformed-UUID)
//   - 5   insufficient_role
func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <audit-id>",
		Short: "Fetch a single audit row by id",
		Long: "show calls GET /api/v1/audit/show/{audit_id} and renders the " +
			"single audit row. The argument must be a UUID (the backend " +
			"validates and returns 422 on malformed input). A 404 means " +
			"either the audit_id doesn't exist or it belongs to another " +
			"tenant — the route deliberately conflates the two so " +
			"existence is never leaked across tenant boundaries.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShow(cmd, showOptions{
				AuditID:           args[0],
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

type showOptions struct {
	AuditID           string
	JSONOut           bool
	BackplaneOverride string
}

func runShow(cmd *cobra.Command, opts showOptions) error {
	if opts.AuditID == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("show requires a non-empty <audit-id> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	entry, err := getEntry(cmd.Context(), backplaneURL, opts.AuditID)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

// buildShowPath assembles the GET path. Exposed for unit tests so
// URL encoding of UUIDs with unusual rendering stays covered.
func buildShowPath(auditID string) string {
	return "/api/v1/audit/show/" + pathEscape(auditID)
}

func getEntry(ctx context.Context, backplaneURL, auditID string) (*Entry, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildShowPath(auditID), nil)
	if err != nil {
		return nil, err
	}
	var out Entry
	if err := decodeAuditResponse(raw, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// printEntrySummary renders the full audit row as a stable
// key-value summary. Optional fields are shown as "-" when null so
// the layout stays grep-able and predictable across rows.
func printEntrySummary(w io.Writer, e *Entry) {
	fmt.Fprintf(w, "%-18s %s\n", "id:", e.ID)
	fmt.Fprintf(w, "%-18s %s\n", "ts:", e.TS)
	fmt.Fprintf(w, "%-18s %s\n", "tenant_id:", strDerefOrDash(e.TenantID))
	fmt.Fprintf(w, "%-18s %s\n", "principal_sub:", e.PrincipalSub)
	fmt.Fprintf(w, "%-18s %s\n", "principal_name:", strDerefOrDash(e.PrincipalName))
	fmt.Fprintf(w, "%-18s %s\n", "target_id:", strDerefOrDash(e.TargetID))
	fmt.Fprintf(w, "%-18s %s\n", "target_name:", strDerefOrDash(e.TargetName))
	fmt.Fprintf(w, "%-18s %s\n", "method:", e.Method)
	fmt.Fprintf(w, "%-18s %s\n", "path:", e.Path)
	fmt.Fprintf(w, "%-18s %d\n", "status_code:", e.StatusCode)
	fmt.Fprintf(w, "%-18s %s\n", "request_id:", strDerefOrDash(e.RequestID))
	fmt.Fprintf(w, "%-18s %s\n", "duration_ms:", strDerefOrDash(e.DurationMS))
	fmt.Fprintf(w, "%-18s %s\n", "op_id:", e.OpID)
	fmt.Fprintf(w, "%-18s %s\n", "op_class:", e.OpClass)
	fmt.Fprintf(w, "%-18s %s\n", "result_status:", e.ResultStatus)
	fmt.Fprintf(w, "%-18s %s\n", "parent_audit_id:", strDerefOrDash(e.ParentAuditID))
	fmt.Fprintf(w, "%-18s %s\n", "agent_session_id:", strDerefOrDash(e.AgentSessionID))
	fmt.Fprintf(w, "%-18s %s\n", "broadcast_event_id:", strDerefOrDash(e.BroadcastEventID))
	if len(e.Payload) > 0 {
		fmt.Fprintf(w, "%-18s %s\n", "payload:", formatPayload(e.Payload))
	} else {
		fmt.Fprintf(w, "%-18s -\n", "payload:")
	}
}

// strDerefOrDash returns *s, or "-" when s is nil / empty. Used by
// the audit-row summary so absent optional fields stay grep-friendly
// rather than rendering as a blank cell.
func strDerefOrDash(s *string) string {
	v := strDeref(s)
	if v == "" {
		return "-"
	}
	return v
}

// formatPayload renders the audit payload as a sorted-key
// `k=v, k=v` summary. Sorted keys keep the output deterministic for
// snapshot tests and operator diffs across runs.
func formatPayload(payload map[string]any) string {
	keys := make([]string, 0, len(payload))
	for k := range payload {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%s", k, formatPayloadScalar(payload[k])))
	}
	return strings.Join(parts, ", ")
}

// formatPayloadScalar renders one payload value compactly. Scalars
// print directly; objects/lists round-trip through json.Marshal so
// at least the JSON form is readable on a single line.
//
// “json.Number“ is the load-bearing case: the package's decoder
// uses “UseNumber()“ (see “decodeAuditResponse“) so payload
// numbers survive as their exact decimal string rather than rounding
// through float64. A 64-bit “hit_count“ like “1745923128091“ (a
// Unix-millis timestamp) prints back identical to what the backend
// wrote, where “float64(1745923128091)“ would round-trip lossily.
func formatPayloadScalar(v any) string {
	switch v := v.(type) {
	case string:
		return v
	case bool:
		return fmt.Sprintf("%t", v)
	case json.Number:
		return v.String()
	case float64:
		// Defensive fallback for code paths that bypass the
		// UseNumber decoder (e.g. payloads constructed by tests).
		if v == float64(int64(v)) {
			return fmt.Sprintf("%d", int64(v))
		}
		return fmt.Sprintf("%g", v)
	case nil:
		return "null"
	default:
		blob, err := json.Marshal(v)
		if err != nil {
			return fmt.Sprintf("%v", v)
		}
		return string(blob)
	}
}
