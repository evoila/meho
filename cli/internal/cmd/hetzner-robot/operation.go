// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newOperationCmd returns `meho hetzner-robot operation` with search / call
// meta-tool wrappers pre-scoped to hetzner-rest-2026.04. The two
// subcommands are the escape hatch for ops that don't (yet) have a
// dedicated alias verb.
func newOperationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "operation",
		Short:        "Pre-scoped meta-tool wrappers (search / call) for hetzner-rest-2026.04",
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
		Short: "Hybrid BM25 + cosine RRF search across hetzner-rest-2026.04 operations",
		Long: "search wraps GET /api/v1/operations/search with connector_id=\n" +
			"\"hetzner-rest-2026.04\" pre-baked. Use to discover ops outside\n" +
			"the curated alias set.\n\n" +
			"Exit codes mirror meho operation search.",
		Example: "  meho hetzner-robot operation search \"list servers\"\n" +
			"  meho hetzner-robot operation search \"vswitch\" --group robot-networking",
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
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
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

func newOperationCallCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "call <op_id>",
		Short: "Dispatch any hetzner-rest-2026.04 op_id (escape hatch for ops without aliases)",
		Long: "call wraps POST /api/v1/operations/call with connector_id=\n" +
			"\"hetzner-rest-2026.04\" pre-baked. Use when an op doesn't have\n" +
			"a dedicated alias yet.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot operation call GET:/server --target rdc-robot\n" +
			"  meho hetzner-robot operation call GET:/query --target rdc-robot --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOperationCall(cmd, args[0], targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runOperationCall(cmd *cobra.Command, opID, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, nil)
}
