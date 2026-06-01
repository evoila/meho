// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package argocd

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newRepoCmd returns the `meho argocd repo` parent with one sub-verb:
// `list` (argocd.repo.list).
func newRepoCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "repo",
		Short:        "ArgoCD repository sub-verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRepoListCmd())
	return cmd
}

// newRepoListCmd returns the `meho argocd repo list` command.
//
// Maps to op_id `argocd.repo.list`. GETs /api/v1/repositories and
// returns the configured repos + their connection state as
// {items, metadata}.
func newRepoListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List configured ArgoCD repositories and their connection state",
		Long: "list dispatches argocd.repo.list and renders the configured\n" +
			"repositories as a table of repo URL / type / connection status.\n" +
			"Use to diagnose a 'repository not accessible' / ComparisonError\n" +
			"app condition. --json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho argocd repo list --target rdc-argocd\n" +
			"  meho argocd repo list --target rdc-argocd --json | jq '.result.items[].connectionState'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRepoList(cmd, targetName, jsonOut, backplaneOverride)
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

func runRepoList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "argocd.repo.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "argocd.repo.list", r, jsonOut, printRepoList)
}

func printRepoList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s argocd.repo.list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeItemsResult(r.Result)
	if err != nil || items == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-48s %-6s %s\n", "REPO", "TYPE", "CONNECTION")
	for _, repo := range items {
		url := truncate(stringField(repo, "repo"), 48)
		repoType := truncate(stringField(repo, "type"), 6)
		conn := nestedString(repo, "connectionState", "status")
		fmt.Fprintf(w, "  %-48s %-6s %s\n", url, repoType, conn)
	}
	fmt.Fprintf(w, "  (%d repositories)\n", len(items))
}
