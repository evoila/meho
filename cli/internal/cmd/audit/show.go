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

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
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
	// The typed-client's `auditId` path parameter is
	// `openapi_types.UUID`. Parse the operator string at the verb
	// edge so a malformed argument surfaces as a clean
	// output.Unexpected instead of either a server-side 422 after
	// the round-trip or a panic on assignment to the typed param.
	parsed, err := uuid.Parse(strings.TrimSpace(opts.AuditID))
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"audit-id is not a valid UUID: %s", opts.AuditID)),
			opts.JSONOut,
		)
	}
	auditID := openapi_types.UUID(parsed)
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	rawBody, entry, err := getEntry(cmd.Context(), client, auditID)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		// Emit server bytes verbatim so a payload integer above
		// 2^53 survives without rounding through the generated
		// `map[string]interface{}` decoder. The human summary path
		// accepts the float64 conversion for the table view (a
		// rounding loss only matters in the extreme-int edge case,
		// and the --json contract is the precision-preserving one
		// jq pipelines consume).
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if entry == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against AuditEntry"),
			opts.JSONOut,
		)
	}
	printEntrySummary(cmd.OutOrStdout(), entry)
	return nil
}

// getEntry drives the typed-client `ShowApiV1AuditShowAuditIdGet`
// endpoint with a one-shot 401-retry (mirrors `postQuery`). Returns
// the raw body bytes (for --json verbatim passthrough) plus the
// decoded `*api.AuditEntry` (for the key-value summary), or an
// `*httpResponseError` for a non-2xx status.
func getEntry(
	ctx context.Context,
	client *api.AuthedClient,
	auditID openapi_types.UUID,
) ([]byte, *api.AuditEntry, error) {
	resp, err := client.ShowApiV1AuditShowAuditIdGetWithResponse(ctx, auditID, nil)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.ShowApiV1AuditShowAuditIdGetWithResponse(ctx, auditID, nil)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}

// printEntrySummary renders the full audit row as a stable
// key-value summary. Optional fields are shown as "-" when null so
// the layout stays grep-able and predictable across rows.
func printEntrySummary(w io.Writer, e *api.AuditEntry) {
	fmt.Fprintf(w, "%-18s %s\n", "id:", e.Id.String())
	fmt.Fprintf(w, "%-18s %s\n", "ts:", formatTS(e.Ts))
	fmt.Fprintf(w, "%-18s %s\n", "tenant_id:", strOrDash(uuidDeref(e.TenantId)))
	fmt.Fprintf(w, "%-18s %s\n", "principal_sub:", e.PrincipalSub)
	fmt.Fprintf(w, "%-18s %s\n", "principal_name:", strOrDash(strDeref(e.PrincipalName)))
	fmt.Fprintf(w, "%-18s %s\n", "target_id:", strOrDash(uuidDeref(e.TargetId)))
	fmt.Fprintf(w, "%-18s %s\n", "target_name:", strOrDash(strDeref(e.TargetName)))
	fmt.Fprintf(w, "%-18s %s\n", "method:", e.Method)
	fmt.Fprintf(w, "%-18s %s\n", "path:", e.Path)
	fmt.Fprintf(w, "%-18s %d\n", "status_code:", e.StatusCode)
	fmt.Fprintf(w, "%-18s %s\n", "request_id:", strOrDash(uuidDeref(e.RequestId)))
	fmt.Fprintf(w, "%-18s %s\n", "duration_ms:", strOrDash(strDeref(e.DurationMs)))
	fmt.Fprintf(w, "%-18s %s\n", "op_id:", e.OpId)
	fmt.Fprintf(w, "%-18s %s\n", "op_class:", e.OpClass)
	fmt.Fprintf(w, "%-18s %s\n", "result_status:", e.ResultStatus)
	fmt.Fprintf(w, "%-18s %s\n", "parent_audit_id:", strOrDash(uuidDeref(e.ParentAuditId)))
	fmt.Fprintf(w, "%-18s %s\n", "agent_session_id:", strOrDash(uuidDeref(e.AgentSessionId)))
	fmt.Fprintf(w, "%-18s %s\n", "broadcast_event_id:", strOrDash(uuidDeref(e.BroadcastEventId)))
	if len(e.Payload) > 0 {
		fmt.Fprintf(w, "%-18s %s\n", "payload:", formatPayload(e.Payload))
	} else {
		fmt.Fprintf(w, "%-18s -\n", "payload:")
	}
}

// strOrDash returns "-" when s is empty, otherwise s. Used by the
// audit-row summary so absent optional fields stay grep-friendly
// rather than rendering as a blank cell.
func strOrDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
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
// The generated client decodes payload fields as `interface{}` via
// `json.Unmarshal`, which lands integer values as `float64`. The
// helper folds the integer case (a float64 whose fractional part is
// zero) back to a bare integer render so `hit_count=7` doesn't
// surface as `hit_count=7.000000`. For the rare ≥2^53 integer the
// rounding-through-float64 loss is real; operators who need
// exact-precision payload bytes pipe through `--json`, which writes
// the server bytes verbatim.
func formatPayloadScalar(v any) string {
	switch v := v.(type) {
	case string:
		return v
	case bool:
		return fmt.Sprintf("%t", v)
	case json.Number:
		// Test fixtures that explicitly construct entries with
		// json.Number values exercise this arm; production calls
		// land as float64 via the generated decoder.
		return v.String()
	case float64:
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
