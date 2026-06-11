// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// listEntry mirrors all 13 fields of the backend ConnectorListItem
// Pydantic model (operations/ingest/api_schemas.py): one row per
// listed connector with parsed coordinates + tenant scope +
// per-status group counts + the operation rollup — `operation_count`
// total vs `enabled_operation_count` dispatchable subset (#1636) —
// plus the dispatchability `state` ("ingested" = DB-backed, resolves
// through the dispatcher; "registered" = v2-registry entry without
// descriptor rows yet, #773) and the `next_step` remediation hint
// shipped on registered rows (#1133). The full per-op detail comes
// from `meho connector review <id>`. Keep the field set complete:
// the `--json` path re-marshals this struct (not the raw response
// body), so any ConnectorListItem field missing here silently
// vanishes from machine-readable output — `encoding/json.Unmarshal`
// ignores unknown keys. TestListEntryDecodesCanonical pins the
// fixture⇄struct direction with DisallowUnknownFields.
//
// Kept as a package-private decode shape (not surfaced through the
// generated `api.*` types) because the FastAPI list endpoint
// deliberately returns `dict[str, list[dict[str, object]]]` instead
// of declaring a `response_model=ConnectorListResponse` — the route
// dumps each item via `model_dump(mode="json")` so per-row
// `tenant_id` UUIDs render as strings (see
// `backend/src/meho_backplane/api/v1/connectors_ingest.py:list_endpoint`).
// oapi-codegen reflects the lack of `response_model` as
// `JSON200 *map[string][]map[string]interface{}`, which is harder to
// drive than a hand-typed decode against the raw `Body` bytes. The
// transport / auth / 401-refresh path still routes through the
// generated `*WithResponse` method — this struct is just the JSON
// schema for the body.
//
// Note: the canonical payload has no `review_status` field. The
// review state is per-group (some groups can be staged while others
// are enabled — common when an operator partially approves a
// connector). The operator-facing rollup label rendered in the
// human table is derived from the three per-status counts at render
// time; see `deriveRollupLabel`.
//
// `tenant_id` is a UUID for tenant-curated connectors and JSON `null`
// for built-in connectors. Pointer-to-string so we can distinguish
// "field absent" from "empty string" in the rendered table.
type listEntry struct {
	ConnectorID           string    `json:"connector_id"`
	Product               string    `json:"product"`
	Version               string    `json:"version"`
	ImplID                string    `json:"impl_id"`
	TenantID              *string   `json:"tenant_id"`
	GroupCount            int       `json:"group_count"`
	StagedGroupCount      int       `json:"staged_group_count"`
	EnabledGroupCount     int       `json:"enabled_group_count"`
	DisabledGroupCount    int       `json:"disabled_group_count"`
	OperationCount        int       `json:"operation_count"`
	EnabledOperationCount int       `json:"enabled_operation_count"`
	State                 string    `json:"state"`
	NextStep              *nextStep `json:"next_step"`
}

// nextStep mirrors the backend NextStep Pydantic model (same module):
// the self-describing hint shipped on `state="registered"` rows —
// the `meho connector ingest ...` verb that closes the workflow plus
// the rationale for why that verb applies. The listEntry field is
// pointer-typed because the backend ships JSON `null` on every
// `state="ingested"` row (nothing left for the operator to do), and
// `null` must decode to nil — same null-vs-absent discipline as
// `TenantID` above.
type nextStep struct {
	Verb      string `json:"verb"`
	Rationale string `json:"rationale"`
}

// connectorListEnvelope is the package-private decode shape for the
// `{"connectors": [...]}` envelope the list endpoint returns. The
// envelope is wrapped (rather than a bare list) so future paging
// fields can land non-breakingly, mirroring the GET /catalog list
// shape; see the route's docstring for the reasoning.
type connectorListEnvelope struct {
	Connectors []listEntry `json:"connectors"`
}

// validStatuses pins the allowed --status values. `all` is the
// no-filter default; the wire shape uses an absent status query
// param for that case (T6's `Literal | None` semantic).
var validStatuses = map[string]struct{}{
	"staged":   {},
	"enabled":  {},
	"disabled": {},
	"all":      {},
}

// newListCmd returns the `meho connector list` command.
//
// CLI shape:
//
//	meho connector list [--status staged|enabled|disabled|all] [--json] [--backplane <url>]
//
// Hits GET /api/v1/connectors?status=... and renders the result as
// a one-line-per-connector table. operator role suffices (read-only).
func newListCmd() *cobra.Command {
	var (
		status            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List ingested connectors filtered by review status",
		Long: "list calls GET /api/v1/connectors against the configured\n" +
			"backplane and renders one row per ingested connector with its\n" +
			"review state + group/op counts. Default `--status all` lists\n" +
			"every connector visible to the operator's tenant (incl. built-in\n" +
			"connectors with tenant_id IS NULL).\n\n" +
			"Operator-role tokens can list; tenant_admin is not required for\n" +
			"this read-only verb.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Status:            status,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&status, "status", "all",
		"filter by review status: staged | enabled | disabled | all (default all)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Status            string
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if _, ok := validStatuses[opts.Status]; !ok {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--status %q invalid; expected one of: staged | enabled | disabled | all",
				opts.Status,
			)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := getList(cmd.Context(), backplaneURL, opts.Status)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) {
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printListTable(cmd.OutOrStdout(), opts.Status, result)
	return nil
}

// listQueryParams maps the CLI flags onto the generated query-param
// shape. `all` is omitted from the wire (the backplane's
// `Literal | None` default reads the absence as "no filter"); any
// other status passes through as the typed enum value.
func listQueryParams(status string) *api.ListEndpointApiV1ConnectorsGetParams {
	params := &api.ListEndpointApiV1ConnectorsGetParams{}
	if status != "" && status != "all" {
		v := api.ListEndpointApiV1ConnectorsGetParamsStatus(status)
		params.Status = &v
	}
	return params
}

// getList drives the typed-client list endpoint with a one-shot
// 401-retry (mirrors `api.AuthedClient.GetHealth`'s contract for
// /api/v1/health). Non-2xx surfaces as *httpResponseError so the
// caller routes the body through renderHTTPStatus; transport-layer
// errors propagate verbatim.
//
// The list endpoint deliberately returns
// `dict[str, list[dict[str, object]]]` (no `response_model`), so the
// typed envelope's `JSON200` is an untyped map; we decode the raw
// `*Response.Body` bytes into the package-private
// connectorListEnvelope shape so per-row helpers stay typed.
func getList(ctx context.Context, backplaneURL, status string) (*connectorListEnvelope, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(status)
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListEndpointApiV1ConnectorsGetResponse, error) {
			return authed.ListEndpointApiV1ConnectorsGetWithResponse(ctx, params)
		},
		func(r *api.ListEndpointApiV1ConnectorsGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	var out connectorListEnvelope
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, fmt.Errorf("decode connector list response: %w", err)
	}
	return &out, nil
}

func printListTable(w io.Writer, status string, r *connectorListEnvelope) {
	if len(r.Connectors) == 0 {
		fmt.Fprintf(w, "0 connector(s) with status=%s\n", status)
		return
	}
	fmt.Fprintf(w, "%d connector(s) with status=%s\n", len(r.Connectors), status)
	fmt.Fprintf(w, "%-32s %-10s %-10s %5s %5s\n",
		"connector_id", "status", "tenant", "grps", "ops",
	)
	for _, c := range r.Connectors {
		tenant := "(built-in)"
		if c.TenantID != nil && *c.TenantID != "" {
			tenant = *c.TenantID
		}
		fmt.Fprintf(w, "%-32s %-10s %-10s %5d %5d\n",
			truncate(c.ConnectorID, 32),
			deriveRollupLabel(c.StagedGroupCount, c.EnabledGroupCount, c.DisabledGroupCount),
			truncate(tenant, 10),
			c.GroupCount,
			c.OperationCount,
		)
	}
}

// deriveRollupLabel computes a connector-wide status rollup from the
// three per-group review_status counts the backend ships in
// ConnectorListItem. The canonical payload has no top-level
// review_status (a connector can hold a mix of staged / enabled /
// disabled groups), so the operator-facing label is derived here.
//
// Rules (load-bearing; mirrored in the `review` verb's header):
//
//   - "(empty)"  — connector has zero groups (post-ingest before
//     grouping, or every group was orphan-only and got pruned).
//   - "staged"   — at least one group is still awaiting review and
//     none have been enabled yet. The operator-facing question is
//     "is there anything left to review" and the answer is yes.
//   - "enabled"  — every group is enabled. The connector is fully
//     live; all `is_enabled=true` operations are dispatchable.
//   - "disabled" — every group is disabled. The connector was
//     rolled back; no operations are dispatchable. Per-op overrides
//     are preserved (a future re-enable picks them back up).
//   - "mixed"    — partial enable / partial review state, or an
//     unknown enum value the CLI doesn't recognise. The CLI bias
//     here is conservative: surface the heterogeneity rather than
//     hide it under a single label.
func deriveRollupLabel(staged, enabled, disabled int) string {
	total := staged + enabled + disabled
	if total == 0 {
		return "(empty)"
	}
	if staged == total {
		return "staged"
	}
	if enabled == total {
		return "enabled"
	}
	if disabled == total {
		return "disabled"
	}
	// At least two of {staged, enabled, disabled} are non-zero, or
	// the per-status counts don't sum to group_count (server shipped
	// a status the CLI doesn't recognise). "mixed" is the right
	// operator-facing answer in both cases — surface the
	// heterogeneity rather than hide it under a single label.
	return "mixed"
}
