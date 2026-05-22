// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newProjectCmd returns `meho harbor project` with list / info subcommands.
func newProjectCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "project",
		Short:        "List or inspect Harbor projects",
		SilenceUsage: true,
	}
	cmd.AddCommand(newProjectListCmd())
	cmd.AddCommand(newProjectInfoCmd())
	return cmd
}

func newProjectListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all Harbor projects",
		Long: "list dispatches GET:/api/v2.0/projects against connector_id=\"harbor-rest-2.x\"\n" +
			"and renders a table of project names, visibility, and repository counts.\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor project list --target prod-harbor\n" +
			"  meho harbor project list --target prod-harbor --json | jq '.result[].name'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runProjectList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runProjectList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/api/v2.0/projects", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/api/v2.0/projects", r, jsonOut, printProjectList)
}

func printProjectList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2.0/projects — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintf(w, "  (0 projects)\n")
		return
	}
	fmt.Fprintf(w, "%-30s %-8s %6s  %s\n", "name", "public", "repos", "owner")
	for _, p := range items {
		name, _ := p["name"].(string)
		owner, _ := p["owner_name"].(string)
		repos := int64(0)
		if v, ok := p["repo_count"].(float64); ok {
			repos = int64(v)
		}
		public := "false"
		if meta, ok := p["metadata"].(map[string]any); ok {
			if pub, ok := meta["public"].(string); ok {
				public = pub
			}
		}
		fmt.Fprintf(w, "%-30s %-8s %6d  %s\n",
			truncate(name, 30), truncate(public, 8), repos, truncate(owner, 30))
	}
}

func newProjectInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <project_name>",
		Short: "Show full details for a Harbor project",
		Long: "info dispatches GET:/api/v2.0/projects/{project_name} against\n" +
			"connector_id=\"harbor-rest-2.x\" and renders project quota, repo\n" +
			"count, and metadata.\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor project info library --target prod-harbor\n" +
			"  meho harbor project info myproject --target prod-harbor --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProjectInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runProjectInfo(cmd *cobra.Command, projectName, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	opID := "GET:/api/v2.0/projects/{project_name}"
	params := map[string]any{"project_name": projectName}
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printProjectInfo)
}

func printProjectInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2.0/projects/{project_name} — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var p struct {
		Name      string         `json:"name"`
		OwnerName string         `json:"owner_name"`
		RepoCount int            `json:"repo_count"`
		Metadata  map[string]any `json:"metadata"`
	}
	if err := jsonUnmarshalStrict(r.Result, &p); err != nil || p.Name == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  name:       %s\n", p.Name)
	fmt.Fprintf(w, "  owner:      %s\n", p.OwnerName)
	fmt.Fprintf(w, "  repo_count: %d\n", p.RepoCount)
	if len(p.Metadata) > 0 {
		if pub, ok := p.Metadata["public"].(string); ok {
			fmt.Fprintf(w, "  public:     %s\n", pub)
		}
	}
}
