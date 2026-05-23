// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newOperationCmd returns `meho vcf-automation operation` with search /
// call meta-tool wrappers pre-scoped to vcfa-rest-9.0. The two
// subcommands are the escape hatch for ops that don't (yet) have a
// dedicated alias; the raw op_id already encodes its plane via the
// path prefix so neither subcommand consults the `--plane` flag.
func newOperationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "operation",
		Short:        "Pre-scoped meta-tool wrappers (search / call) for vcfa-rest-9.0",
		SilenceUsage: true,
	}
	cmd.AddCommand(newOperationSearchCmd())
	cmd.AddCommand(newOperationCallCmd())
	return cmd
}

// searchHit mirrors the backend OperationSearchHit Pydantic model.
type searchHit struct {
	OpID             string   `json:"op_id"`
	Summary          *string  `json:"summary"`
	Description      *string  `json:"description"`
	GroupKey         *string  `json:"group_key"`
	SafetyLevel      string   `json:"safety_level"`
	RequiresApproval bool     `json:"requires_approval"`
	FusedScore       float64  `json:"fused_score"`
	Bm25Score        *float64 `json:"bm25_score"`
	CosineScore      *float64 `json:"cosine_score"`
}

type searchResponse struct {
	Hits            []searchHit `json:"hits"`
	QueryDurationMs float64     `json:"query_duration_ms"`
}

func newOperationSearchCmd() *cobra.Command {
	var (
		groupKey          string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Hybrid BM25 + cosine RRF search across vcfa-rest-9.0 operations",
		Long: "search wraps GET /api/v1/operations/search with connector_id=\n" +
			"\"vcfa-rest-9.0\" pre-baked. The hits set spans both planes;\n" +
			"narrow with --group <group_key> to one plane's surface (e.g.\n" +
			"--group tenant-deployments).\n\n" +
			"Exit codes mirror meho operation search.",
		Example: "  meho vcf-automation operation search \"list deployments\"\n" +
			"  meho vcf-automation operation search \"orgs\" --group provider-orgs",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOperationSearch(cmd, args[0], groupKey, limit, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&groupKey, "group", "", "narrow the search to one group_key")
	cmd.Flags().IntVar(&limit, "limit", 10, "max hits (1..50, clamped by the API)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit machine-readable JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runOperationSearch(cmd *cobra.Command, query, groupKey string, limit int, jsonOut bool, backplaneOverride string) error {
	if limit < 1 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be >= 1; got %d", limit)),
			jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	result, err := getSearch(cmd.Context(), backplaneURL, query, groupKey, limit)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printSearchTable(cmd.OutOrStdout(), query, result)
	return nil
}

func getSearch(ctx context.Context, backplaneURL, query, groupKey string, limit int) (*searchResponse, error) {
	q := url.Values{}
	q.Set("connector_id", ConnectorID)
	q.Set("query", query)
	if groupKey != "" {
		q.Set("group", groupKey)
	}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	path := "/api/v1/operations/search?" + q.Encode()
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	var out searchResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode search response: %w", err)
	}
	return &out, nil
}

func printSearchTable(w io.Writer, query string, r *searchResponse) {
	fmt.Fprintf(w, "search %s %q — %d hit(s) in %.0fms\n",
		ConnectorID, query, len(r.Hits), r.QueryDurationMs)
	if len(r.Hits) == 0 {
		return
	}
	fmt.Fprintf(w, "%-50s %6s  %s\n", "op_id", "score", "summary")
	for _, h := range r.Hits {
		fmt.Fprintf(w, "%-50s %6.3f  %s\n",
			truncate(h.OpID, 50),
			h.FusedScore,
			truncate(strDeref(h.Summary), 80),
		)
	}
}

func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func newOperationCallCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "call <op_id>",
		Short: "Dispatch any vcfa-rest-9.0 op_id (escape hatch for ops without aliases)",
		Long: "call wraps POST /api/v1/operations/call with connector_id=\n" +
			"\"vcfa-rest-9.0\" pre-baked. Use when an op doesn't have a\n" +
			"dedicated alias yet. The raw op_id already carries the plane\n" +
			"via its path prefix; the --plane flag is ignored on this verb.\n" +
			"The persistent --fqdn flag still threads into the body.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-automation operation call GET:/cloudapi/1.0.0/site --target rdc-vcfa\n" +
			"  meho vcf-automation operation call GET:/iaas/api/about --target rdc-vcfa --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOperationCall(cmd, args[0], targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCFA target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runOperationCall(cmd *cobra.Command, opID, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, readFqdn(cmd), params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, nil)
}
