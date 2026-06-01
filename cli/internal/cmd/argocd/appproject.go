// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package argocd

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAppProjectCmd returns the `meho argocd appproject` parent with one
// sub-verb: `list` (argocd.appproject.list).
func newAppProjectCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "appproject",
		Short:        "ArgoCD AppProject sub-verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAppProjectListCmd())
	return cmd
}

// newAppProjectListCmd returns the `meho argocd appproject list` command.
//
// Maps to op_id `argocd.appproject.list`. GETs /api/v1/projects and
// returns the AppProjects + their allow-lists as {items, metadata}.
func newAppProjectListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List ArgoCD AppProjects and their source/destination allow-lists",
		Long: "list dispatches argocd.appproject.list and renders the AppProjects\n" +
			"as a table of name / source-repo count / destination count. --json\n" +
			"emits the full OperationResult envelope (read spec.sourceRepos and\n" +
			"spec.destinations for the full allow-lists).\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho argocd appproject list --target rdc-argocd\n" +
			"  meho argocd appproject list --target rdc-argocd --json | jq '.result.items[].spec.sourceRepos'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAppProjectList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runAppProjectList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "argocd.appproject.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "argocd.appproject.list", r, jsonOut, printAppProjectList)
}

func printAppProjectList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s argocd.appproject.list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeItemsResult(r.Result)
	if err != nil || items == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-28s %-12s %s\n", "NAME", "SRC_REPOS", "DESTINATIONS")
	for _, proj := range items {
		name := truncate(appName(proj), 28)
		srcRepos := countList(proj, "spec", "sourceRepos")
		dests := countList(proj, "spec", "destinations")
		fmt.Fprintf(w, "  %-28s %-12d %d\n", name, srcRepos, dests)
	}
	fmt.Fprintf(w, "  (%d appprojects)\n", len(items))
}

// countList returns the length of the array reached by walking the key
// chain through nested objects, or 0 when any hop is missing or the leaf
// is not an array.
func countList(obj map[string]any, keys ...string) int {
	cur := obj
	for i, k := range keys {
		v, ok := cur[k]
		if !ok || v == nil {
			return 0
		}
		if i == len(keys)-1 {
			if arr, ok := v.([]any); ok {
				return len(arr)
			}
			return 0
		}
		next, ok := v.(map[string]any)
		if !ok {
			return 0
		}
		cur = next
	}
	return 0
}
