// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newServerCmd returns `meho hetzner-robot server` with list / info subcommands.
func newServerCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "server",
		Short:        "List or inspect dedicated servers in the Hetzner Robot account",
		SilenceUsage: true,
	}
	cmd.AddCommand(newServerListCmd())
	cmd.AddCommand(newServerInfoCmd())
	return cmd
}

func newServerListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all dedicated servers in the Hetzner Robot account",
		Long: "list dispatches GET:/server against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders a table of server number, primary IP, product, datacenter,\n" +
			"and status. Large accounts may return many servers; the response is a\n" +
			"JSON array and may be wrapped in a JSONFlux handle.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot server list --target rdc-robot\n" +
			"  meho hetzner-robot server list --target rdc-robot --json | jq '.result[].server.server_ip'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runServerList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runServerList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/server", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/server", r, jsonOut, printServerList)
}

func printServerList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/server — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 servers)")
		return
	}
	fmt.Fprintf(w, "%-12s %-18s %-20s %-15s %s\n", "number", "ip", "product", "dc", "status")
	for _, item := range items {
		srv := getServerObj(item)
		number := int64(0)
		if v, ok := srv["server_number"].(float64); ok {
			number = int64(v)
		}
		ip, _ := srv["server_ip"].(string)
		product, _ := srv["product"].(string)
		dc, _ := srv["dc"].(string)
		status, _ := srv["status"].(string)
		fmt.Fprintf(w, "%-12d %-18s %-20s %-15s %s\n",
			number,
			truncate(ip, 18),
			truncate(product, 20),
			truncate(dc, 15),
			status,
		)
	}
}

// getServerObj extracts the server object from either a bare map or a
// {"server": {...}} wrapper object.
func getServerObj(item map[string]any) map[string]any {
	if srv, ok := item["server"].(map[string]any); ok {
		return srv
	}
	return item
}

func newServerInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <server-ip>",
		Short: "Show full detail for one dedicated server by its primary IP",
		Long: "info dispatches GET:/server/{server-ip} against\n" +
			"connector_id=\"hetzner-rest-2026.04\" and renders the server's\n" +
			"number, product, datacenter, traffic plan, and status.\n" +
			"<server-ip> is the primary IP from 'meho hetzner-robot server list'.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot server info 1.2.3.4 --target rdc-robot\n" +
			"  meho hetzner-robot server info 1.2.3.4 --target rdc-robot --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runServerInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runServerInfo(cmd *cobra.Command, serverIP, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	opID := "GET:/server/{server-ip}"
	params := map[string]any{"server-ip": serverIP}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printServerInfo)
}

func printServerInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/server/{server-ip} — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var wrapper struct {
		Server map[string]any `json:"server"`
	}
	if err := jsonUnmarshalStrict(r.Result, &wrapper); err == nil && wrapper.Server != nil {
		printServerFields(w, wrapper.Server)
		return
	}
	// Try bare object.
	var srv map[string]any
	if err := jsonUnmarshalStrict(r.Result, &srv); err == nil && len(srv) > 0 {
		printServerFields(w, srv)
		return
	}
	fallbackResultRender(w, r)
}

func printServerFields(w io.Writer, srv map[string]any) {
	printStringField(w, "server_ip", "server_ip", srv)
	if v, ok := srv["server_number"].(float64); ok {
		fmt.Fprintf(w, "  server_number: %d\n", int64(v))
	}
	printStringField(w, "server_name", "server_name", srv)
	printStringField(w, "product", "product", srv)
	printStringField(w, "dc", "dc", srv)
	printStringField(w, "traffic", "traffic", srv)
	printStringField(w, "status", "status", srv)
	if v, ok := srv["flatrate"].(bool); ok {
		fmt.Fprintf(w, "  flatrate:      %v\n", v)
	}
	if v, ok := srv["throttled"].(bool); ok && v {
		fmt.Fprintf(w, "  throttled:     %v\n", v)
	}
	if v, ok := srv["cancelled"].(bool); ok && v {
		fmt.Fprintf(w, "  cancelled:     %v\n", v)
	}
	printStringField(w, "paid_until", "paid_until", srv)
}

func printStringField(w io.Writer, label, key string, m map[string]any) {
	v, _ := m[key].(string)
	if v != "" {
		fmt.Fprintf(w, "  %-14s %s\n", label+":", v)
	}
}
