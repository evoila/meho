// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// ---- IP ----

// newIPCmd returns `meho hetzner-robot ip` with the list subcommand.
func newIPCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "ip",
		Short:        "List IP addresses assigned to the Hetzner Robot account",
		SilenceUsage: true,
	}
	cmd.AddCommand(newIPListCmd())
	return cmd
}

func newIPListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all IPs assigned to the Hetzner Robot account",
		Long: "list dispatches GET:/ip against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders a table of IP addresses with lock status and server routing.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot ip list --target rdc-robot\n" +
			"  meho hetzner-robot ip list --target rdc-robot --json | jq '.result[].ip.ip'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runIPList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runIPList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/ip", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/ip", r, jsonOut, printIPList)
}

func printIPList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/ip — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 IP addresses)")
		return
	}
	fmt.Fprintf(w, "%-20s %-18s %s\n", "ip", "server_ip", "locked")
	for _, item := range items {
		ipObj := getNestedObj(item, "ip")
		ip, _ := ipObj["ip"].(string)
		serverIP, _ := ipObj["server_ip"].(string)
		locked := "false"
		if v, ok := ipObj["locked"].(bool); ok && v {
			locked = "true"
		}
		fmt.Fprintf(w, "%-20s %-18s %s\n",
			truncate(ip, 20), truncate(serverIP, 18), locked)
	}
}

// ---- Subnet ----

// newSubnetCmd returns `meho hetzner-robot subnet` with the list subcommand.
func newSubnetCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "subnet",
		Short:        "List subnets assigned to the Hetzner Robot account",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSubnetListCmd())
	return cmd
}

func newSubnetListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all subnets assigned to the Hetzner Robot account",
		Long: "list dispatches GET:/subnet against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders a table of CIDR, gateway, IP version, and routed server.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot subnet list --target rdc-robot\n" +
			"  meho hetzner-robot subnet list --target rdc-robot --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSubnetList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runSubnetList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/subnet", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/subnet", r, jsonOut, printSubnetList)
}

func printSubnetList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/subnet — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 subnets)")
		return
	}
	fmt.Fprintf(w, "%-30s %-18s %-6s %s\n", "cidr", "gateway", "ipver", "server_ip")
	for _, item := range items {
		subnet := getNestedObj(item, "subnet")
		ip, _ := subnet["ip"].(string)
		mask := ""
		if v, ok := subnet["mask"].(float64); ok {
			mask = fmt.Sprintf("%d", int(v))
		} else if v, ok := subnet["mask"].(string); ok {
			mask = v
		}
		cidr := ip
		if mask != "" {
			cidr = ip + "/" + mask
		}
		gateway, _ := subnet["gateway"].(string)
		ipVer := ""
		if v, ok := subnet["ip_version"].(float64); ok {
			ipVer = fmt.Sprintf("%d", int(v))
		}
		serverIP, _ := subnet["server_ip"].(string)
		fmt.Fprintf(w, "%-30s %-18s %-6s %s\n",
			truncate(cidr, 30), truncate(gateway, 18), ipVer, serverIP)
	}
}

// ---- Failover ----

// newFailoverCmd returns `meho hetzner-robot failover` with the list subcommand.
func newFailoverCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "failover",
		Short:        "List failover IPs in the Hetzner Robot account",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFailoverListCmd())
	return cmd
}

func newFailoverListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all failover IPs and their active routing targets",
		Long: "list dispatches GET:/failover against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders a table of failover IP, owning server, and active server.\n" +
			"When server_ip != active_server_ip the failover is currently routed away.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot failover list --target rdc-robot\n" +
			"  meho hetzner-robot failover list --target rdc-robot --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runFailoverList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runFailoverList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/failover", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/failover", r, jsonOut, printFailoverList)
}

func printFailoverList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/failover — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 failover IPs)")
		return
	}
	fmt.Fprintf(w, "%-18s %-18s %s\n", "failover_ip", "server_ip", "active_server_ip")
	for _, item := range items {
		fo := getNestedObj(item, "failover")
		ip, _ := fo["ip"].(string)
		serverIP, _ := fo["server_ip"].(string)
		activeServerIP, _ := fo["active_server_ip"].(string)
		suffix := ""
		if serverIP != "" && activeServerIP != "" && serverIP != activeServerIP {
			suffix = "  [ROUTED AWAY]"
		}
		fmt.Fprintf(w, "%-18s %-18s %s%s\n",
			truncate(ip, 18), truncate(serverIP, 18), activeServerIP, suffix)
	}
}

// ---- rDNS ----

// newRdnsCmd returns `meho hetzner-robot rdns` with the list subcommand.
func newRdnsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "rdns",
		Short:        "List reverse DNS (PTR record) entries for the Hetzner Robot account",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRdnsListCmd())
	return cmd
}

func newRdnsListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all reverse DNS (PTR) entries set on the account's IPs",
		Long: "list dispatches GET:/rdns against connector_id=\"hetzner-rest-2026.04\"\n" +
			"and renders a table of IP addresses and their PTR record hostnames.\n" +
			"Only IPs with explicitly set PTR records appear in the output.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot rdns list --target rdc-robot\n" +
			"  meho hetzner-robot rdns list --target rdc-robot --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRdnsList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRdnsList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/rdns", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/rdns", r, jsonOut, printRdnsList)
}

func printRdnsList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/rdns — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 rDNS entries)")
		return
	}
	fmt.Fprintf(w, "%-22s %s\n", "ip", "ptr")
	for _, item := range items {
		rdns := getNestedObj(item, "rdns")
		ip, _ := rdns["ip"].(string)
		ptr, _ := rdns["ptr"].(string)
		fmt.Fprintf(w, "%-22s %s\n", truncate(ip, 22), ptr)
	}
}

// getNestedObj extracts a nested map from item[key], returning item
// itself as a fallback when the key is absent or not a map.
func getNestedObj(item map[string]any, key string) map[string]any {
	if nested, ok := item[key].(map[string]any); ok {
		return nested
	}
	return item
}
