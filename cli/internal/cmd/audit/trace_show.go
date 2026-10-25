// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"fmt"
	"io"
	"strings"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newTraceShowCmd returns `meho audit trace show <audit-id>` (#4855).
//
// The human output prints only what the route returns and never a
// header or body value: those are in --json only, which prints the
// route's answer unchanged. The route reads through the same
// tenant-scoped `load_trace` as the console, so the CLI shows the
// same stored, redacted spans.
//
// Exit codes:
//   - 0   trace printed, or the audit row exists but has no stored
//     trace (the API treats "no trace" as a normal state)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response: unknown or other-tenant audit row
//     (404, the same text as `meho audit show`), malformed UUID
//   - 5   insufficient_role
func newTraceShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <audit-id>",
		Short: "Print the stored flight-recorder trace of one operation run",
		Long: "show calls GET /api/v1/audit/{audit_id}/trace and prints when " +
			"the trace was recorded, when MEHO will delete it, whether it is " +
			"withheld from agents, and then one line per recorded step in " +
			"order: a vendor call, a sub-step, or the result reduction. " +
			"Headers and bodies are only in --json, which prints the route's " +
			"answer unchanged. When no trace is stored for the row, show says " +
			"so and exits 0. An unknown audit id, or one of another tenant, " +
			"is 'audit row not found' (exit 4), as on `meho audit show`.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runTraceShow(cmd, traceShowOptions{
				AuditID:           args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"print the route's AuditTraceResult JSON unchanged (includes redacted headers and bodies)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type traceShowOptions struct {
	AuditID           string
	JSONOut           bool
	BackplaneOverride string
}

func runTraceShow(cmd *cobra.Command, opts traceShowOptions) error {
	parsed, err := uuid.Parse(strings.TrimSpace(opts.AuditID))
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("audit-id is not a valid UUID: %s", opts.AuditID)),
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
	rawBody, result, err := getTrace(cmd.Context(), client, auditID)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against AuditTraceResult"),
			opts.JSONOut,
		)
	}
	printTraceSummary(cmd.OutOrStdout(), result)
	return nil
}

// getTrace drives the typed-client `TraceApiV1AuditAuditIdTraceGet`
// endpoint with the same one-shot 401-retry as `getEntry`. Returns
// the raw body (for --json passthrough) and the decoded envelope, or
// an `*httpResponseError` for a non-2xx status.
func getTrace(
	ctx context.Context,
	client *api.AuthedClient,
	auditID openapi_types.UUID,
) ([]byte, *api.AuditTraceResult, error) {
	resp, err := client.TraceApiV1AuditAuditIdTraceGetWithResponse(ctx, auditID, nil)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.TraceApiV1AuditAuditIdTraceGetWithResponse(ctx, auditID, nil)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}

// printTraceSummary renders the trace header and one line per span.
// For a vendor call the method and URL come from the span's
// attributes; the redacted headers and bodies in the same map are
// never printed here.
func printTraceSummary(w io.Writer, r *api.AuditTraceResult) {
	if !r.TracePresent || r.Trace == nil {
		fmt.Fprintln(w, "No trace is stored for this audit row.")
		fmt.Fprintln(w, "Usual reasons: capture is off for this tenant or target, "+
			"the trace was already deleted (7 days by default), "+
			"or the row is not an operation run.")
		if ec := strDeref(r.EventClass); ec != "" {
			fmt.Fprintf(w, "%-22s %s\n", "event_class:", ec)
		}
		return
	}
	t := r.Trace
	fmt.Fprintf(w, "%-22s %s\n", "audit_id:", t.AuditId.String())
	fmt.Fprintf(w, "%-22s %s\n", "recorded_at:", formatTS(t.CreatedAt))
	fmt.Fprintf(w, "%-22s %s\n", "expires_at:", formatTS(t.ExpiresAt))
	withheld := "no"
	if t.RedactionUncertain {
		withheld = "yes (MEHO could not prove every secret was removed; only operators may read it)"
	}
	fmt.Fprintf(w, "%-22s %s\n", "withheld_from_agents:", withheld)
	fmt.Fprintf(w, "%-22s %d\n", "spans:", len(t.Spans))
	if len(t.Spans) == 0 {
		return
	}
	fmt.Fprintln(w)
	fmt.Fprintf(w, "%-4s %-16s %-40s %-8s %-12s %s\n",
		"#", "KIND", "NAME", "STATUS", "DURATION_MS", "CALL")
	for _, span := range t.Spans {
		fmt.Fprintf(w, "%-4d %-16s %-40s %-8s %-12s %s\n",
			span.Seq,
			truncate(span.SpanKind, 16),
			truncate(span.Name, 40),
			truncate(strOrDash(strDeref(span.Status)), 8),
			strOrDash(strDeref(span.DurationMs)),
			strOrDash(vendorCall(span.Attributes)),
		)
	}
}

// vendorCall returns "<method> <url>" from a span's attributes when
// both are present as strings (a vendor call), else "". Only these
// two keys are read; headers and bodies stay in --json.
func vendorCall(attrs map[string]interface{}) string {
	method, _ := attrs["method"].(string)
	url, _ := attrs["url"].(string)
	if method == "" || url == "" {
		return ""
	}
	return method + " " + url
}
