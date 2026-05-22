// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRepositoryCmd returns `meho harbor repository` with list / info subcommands.
func newRepositoryCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "repository",
		Short:        "List or inspect Harbor repositories within a project",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRepositoryListCmd())
	cmd.AddCommand(newRepositoryInfoCmd())
	return cmd
}

func newRepositoryListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list <project_name>",
		Short: "List repositories within a Harbor project",
		Long: "list dispatches GET:/api/v2.0/projects/{project_name}/repositories\n" +
			"against connector_id=\"harbor-rest-2.x\" and renders a table of\n" +
			"repository names, artifact counts, and pull counts.\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor repository list library --target prod-harbor\n" +
			"  meho harbor repository list myproject --target prod-harbor --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRepositoryList(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRepositoryList(cmd *cobra.Command, projectName, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	opID := "GET:/api/v2.0/projects/{project_name}/repositories"
	params := map[string]any{"project_name": projectName}
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printRepositoryList)
}

func printRepositoryList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:.../repositories — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeHarborList(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(items) == 0 {
		fmt.Fprintf(w, "  (0 repositories)\n")
		return
	}
	fmt.Fprintf(w, "%-50s %9s %9s\n", "name", "artifacts", "pulls")
	for _, repo := range items {
		name, _ := repo["name"].(string)
		artifacts := int64(0)
		if v, ok := repo["artifact_count"].(float64); ok {
			artifacts = int64(v)
		}
		pulls := int64(0)
		if v, ok := repo["pull_count"].(float64); ok {
			pulls = int64(v)
		}
		fmt.Fprintf(w, "%-50s %9d %9d\n", truncate(name, 50), artifacts, pulls)
	}
}

func newRepositoryInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <project_name> <repository_name>",
		Short: "Show full details for a Harbor repository",
		Long: "info dispatches GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}\n" +
			"against connector_id=\"harbor-rest-2.x\" and renders repository\n" +
			"detail including pull count, artifact count, and timestamps.\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor repository info library ubuntu --target prod-harbor\n" +
			"  meho harbor repository info myproject myimage --target prod-harbor --json",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRepositoryInfo(cmd, args[0], args[1], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRepositoryInfo(cmd *cobra.Command, projectName, repoName, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	opID := "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}"
	params := map[string]any{
		"project_name":    projectName,
		"repository_name": repoName,
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printRepositoryInfo)
}

func printRepositoryInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:.../repositories/{name} — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var repo struct {
		Name          string `json:"name"`
		Description   string `json:"description"`
		ArtifactCount int    `json:"artifact_count"`
		PullCount     int    `json:"pull_count"`
		UpdateTime    string `json:"update_time"`
	}
	if err := jsonUnmarshalStrict(r.Result, &repo); err != nil || repo.Name == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  name:           %s\n", repo.Name)
	if repo.Description != "" {
		fmt.Fprintf(w, "  description:    %s\n", repo.Description)
	}
	fmt.Fprintf(w, "  artifact_count: %d\n", repo.ArtifactCount)
	fmt.Fprintf(w, "  pull_count:     %d\n", repo.PullCount)
	if repo.UpdateTime != "" {
		fmt.Fprintf(w, "  updated:        %s\n", repo.UpdateTime)
	}
}
