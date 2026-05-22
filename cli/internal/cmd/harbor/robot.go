// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newRobotCmd returns `meho harbor robot` with list / create / delete subcommands.
func newRobotCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "robot",
		Short:        "List, create, or delete Harbor robot accounts",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRobotListCmd())
	cmd.AddCommand(newRobotCreateCmd())
	cmd.AddCommand(newRobotDeleteCmd())
	return cmd
}

func newRobotListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Harbor system-level robot accounts",
		Long: "list dispatches GET:/api/v2.0/robots against connector_id=\"harbor-rest-2.x\"\n" +
			"and renders a table of robot names, enabled status, and expiry.\n" +
			"Robot secrets are never returned by the list endpoint — they are only\n" +
			"available immediately after robot creation.\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor robot list --target prod-harbor\n" +
			"  meho harbor robot list --target prod-harbor --json | jq '.result[].name'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRobotList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRobotList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/api/v2.0/robots", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/api/v2.0/robots", r, jsonOut, printRobotList)
}

func printRobotList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2.0/robots — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintf(w, "  (0 robot accounts)\n")
		return
	}
	fmt.Fprintf(w, "%-6s %-50s %-8s %s\n", "id", "name", "enabled", "expires_at")
	for _, robot := range items {
		id := int64(0)
		if v, ok := robot["id"].(float64); ok {
			id = int64(v)
		}
		name, _ := robot["name"].(string)
		disabled, _ := robot["disable"].(bool)
		enabled := "true"
		if disabled {
			enabled = "false"
		}
		expiresAt := ""
		if v, ok := robot["expires_at"].(float64); ok {
			if int64(v) == -1 {
				expiresAt = "never"
			} else {
				expiresAt = fmt.Sprintf("%d", int64(v))
			}
		}
		fmt.Fprintf(w, "%-6d %-50s %-8s %s\n",
			id, truncate(name, 50), truncate(enabled, 8), expiresAt)
	}
}

func newRobotCreateCmd() *cobra.Command {
	var (
		robotName         string
		projectName       string
		duration          int
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a project-scoped robot account in Harbor",
		Long: "create dispatches harbor.robot.create against connector_id=\"harbor-rest-2.x\".\n" +
			"The minted secret is returned ONLY on creation — Harbor does not expose\n" +
			"it again after this call. Store it immediately.\n\n" +
			"The broadcast feed never carries the secret (credential_mint classification\n" +
			"collapses the event to aggregate-only).\n\n" +
			"--json emits the full OperationResult envelope including the minted secret.",
		Example: "  meho harbor robot create --name ci-push --project myproject --duration 90 --target prod-harbor\n" +
			"  meho harbor robot create --name ci-pull --project library --duration -1 --target prod-harbor --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRobotCreate(cmd, robotName, projectName, duration, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&robotName, "name", "", "robot account name (alphanumeric, hyphens, underscores)")
	cmd.Flags().StringVar(&projectName, "project", "", "Harbor project to scope the robot to")
	cmd.Flags().IntVar(&duration, "duration", -1, "validity in days (-1 = never expires)")
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("name")
	_ = cmd.MarkFlagRequired("project")
	return cmd
}

func runRobotCreate(cmd *cobra.Command, robotName, projectName string, duration int, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{
		"name":     robotName,
		"project":  projectName,
		"duration": duration,
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "harbor.robot.create", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "harbor.robot.create", r, jsonOut, printRobotCreate)
}

func printRobotCreate(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s harbor.robot.create — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var robot struct {
		ID     int    `json:"id"`
		Name   string `json:"name"`
		Secret string `json:"secret"`
	}
	if err := jsonUnmarshalStrict(r.Result, &robot); err != nil || robot.Name == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  id:     %d\n", robot.ID)
	fmt.Fprintf(w, "  name:   %s\n", robot.Name)
	if robot.Secret != "" {
		fmt.Fprintf(w, "  secret: %s\n", robot.Secret)
		fmt.Fprintf(w, "\nIMPORTANT: store the secret now — Harbor does not return it again.\n")
	}
}

func newRobotDeleteCmd() *cobra.Command {
	var (
		projectName       string
		robotID           int
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete",
		Short: "Delete a project-scoped robot account from Harbor",
		Long: "delete dispatches harbor.robot.delete against connector_id=\"harbor-rest-2.x\".\n" +
			"Requires the numeric --id returned by harbor robot create.\n" +
			"This operation is irreversible.\n\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor robot delete --project myproject --id 42 --target prod-harbor\n" +
			"  meho harbor robot delete --project myproject --id 42 --target prod-harbor --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRobotDelete(cmd, projectName, robotID, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&projectName, "project", "", "Harbor project that scopes the robot account")
	cmd.Flags().IntVar(&robotID, "id", 0, "numeric robot account ID")
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("project")
	_ = cmd.MarkFlagRequired("id")
	return cmd
}

func runRobotDelete(cmd *cobra.Command, projectName string, robotID int, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{
		"project": projectName,
		"id":      robotID,
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "harbor.robot.delete", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "harbor.robot.delete", r, jsonOut, printRobotDelete)
}

func printRobotDelete(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s harbor.robot.delete — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var result struct {
		ID      int  `json:"id"`
		Deleted bool `json:"deleted"`
	}
	if err := jsonUnmarshalStrict(r.Result, &result); err != nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  id:      %d\n", result.ID)
	fmt.Fprintf(w, "  deleted: %v\n", result.Deleted)
}
