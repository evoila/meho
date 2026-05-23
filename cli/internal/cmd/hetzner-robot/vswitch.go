// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"fmt"
	"io"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newVswitchCmd returns `meho hetzner-robot vswitch` with list / info subcommands.
func newVswitchCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "vswitch",
		Short:        "List or inspect vSwitches in the Hetzner Robot account",
		SilenceUsage: true,
	}
	cmd.AddCommand(newVswitchListCmd())
	cmd.AddCommand(newVswitchInfoCmd())
	return cmd
}

func newVswitchListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all vSwitches in the Hetzner Robot account",
		Long: "list dispatches GET:/vswitch against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders a table of vSwitch ID, name, VLAN, and member server count.\n" +
			"Use 'meho hetzner-robot vswitch info <id>' for full member server detail.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot vswitch list --target rdc-robot\n" +
			"  meho hetzner-robot vswitch list --target rdc-robot --json | jq '.result[].vswitch.id'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runVswitchList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runVswitchList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/vswitch", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/vswitch", r, jsonOut, printVswitchList)
}

func printVswitchList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/vswitch — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeRobotList(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(items) == 0 {
		fmt.Fprintln(w, "  (0 vSwitches)")
		return
	}
	fmt.Fprintf(w, "%-8s %-30s %-6s %s\n", "id", "name", "vlan", "servers")
	for _, item := range items {
		vs := getNestedObj(item, "vswitch")
		id := ""
		if v, ok := vs["id"].(float64); ok {
			id = strconv.FormatInt(int64(v), 10)
		}
		name, _ := vs["name"].(string)
		vlan := ""
		if v, ok := vs["vlan"].(float64); ok {
			vlan = strconv.FormatInt(int64(v), 10)
		}
		serverCount := 0
		if servers, ok := vs["server"].([]any); ok {
			serverCount = len(servers)
		}
		fmt.Fprintf(w, "%-8s %-30s %-6s %d\n",
			id, truncate(name, 30), vlan, serverCount)
	}
}

func newVswitchInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <id>",
		Short: "Show full detail for one vSwitch by its numeric ID",
		Long: "info dispatches GET:/vswitch/{id} against\n" +
			"connector_id=\"hetzner-rest-2026.04\" and renders the vSwitch's\n" +
			"name, VLAN, and full member server list.\n" +
			"<id> is the numeric ID from 'meho hetzner-robot vswitch list'.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot vswitch info 4321 --target rdc-robot\n" +
			"  meho hetzner-robot vswitch info 4321 --target rdc-robot --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runVswitchInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runVswitchInfo(cmd *cobra.Command, vswitchID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	opID := "GET:/vswitch/{id}"
	params := map[string]any{"id": vswitchID}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printVswitchInfo)
}

func printVswitchInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/vswitch/{id} — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var wrapper struct {
		Vswitch map[string]any `json:"vswitch"`
	}
	vs := map[string]any{}
	if err := jsonUnmarshalStrict(r.Result, &wrapper); err == nil && wrapper.Vswitch != nil {
		vs = wrapper.Vswitch
	} else {
		if err := jsonUnmarshalStrict(r.Result, &vs); err != nil || len(vs) == 0 {
			fallbackResultRender(w, r)
			return
		}
	}
	if v, ok := vs["id"].(float64); ok {
		fmt.Fprintf(w, "  id:        %d\n", int64(v))
	}
	if name, ok := vs["name"].(string); ok && name != "" {
		fmt.Fprintf(w, "  name:      %s\n", name)
	}
	if v, ok := vs["vlan"].(float64); ok {
		fmt.Fprintf(w, "  vlan:      %d\n", int64(v))
	}
	if v, ok := vs["cancelled"].(bool); ok && v {
		fmt.Fprintln(w, "  cancelled: true")
	}
	if servers, ok := vs["server"].([]any); ok && len(servers) > 0 {
		fmt.Fprintf(w, "  servers (%d):\n", len(servers))
		for _, s := range servers {
			srv, ok := s.(map[string]any)
			if !ok {
				continue
			}
			serverIP, _ := srv["server_ip"].(string)
			status, _ := srv["status"].(string)
			number := ""
			if v, ok := srv["server_number"].(float64); ok {
				number = strconv.FormatInt(int64(v), 10)
			}
			fmt.Fprintf(w, "    - %s  (number=%s, status=%s)\n", serverIP, number, status)
		}
	}
}
