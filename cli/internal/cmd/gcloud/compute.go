// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package gcloud

import (
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newComputeCmd returns the `meho gcloud compute` parent command and
// assembles its sub-tree:
//
//	gcloud compute instances list [--zone Z]   — gcloud.compute.instances.list
//	gcloud compute networks list               — gcloud.compute.networks.list
//	gcloud compute subnets list [--region R]   — gcloud.compute.subnetworks.list
func newComputeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "compute",
		Short:        "GCP Compute Engine verbs (instances, networks, subnets)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newComputeInstancesCmd())
	cmd.AddCommand(newComputeNetworksCmd())
	cmd.AddCommand(newComputeSubnetsCmd())
	return cmd
}

// --- instances sub-group ---

// newComputeInstancesCmd returns the `meho gcloud compute instances`
// parent command with its list verb.
func newComputeInstancesCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "instances",
		Short:        "Compute Engine instance verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newComputeInstancesListCmd())
	return cmd
}

// newComputeInstancesListCmd returns `meho gcloud compute instances list`.
// Maps to op_id `gcloud.compute.instances.list`. Output is the canonical
// `{rows, total}` envelope; rows carry zone, name, machine_type,
// status, internal_ips, external_ips, creation_timestamp.
func newComputeInstancesListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
		zone              string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Compute Engine instances (all zones or a specific zone)",
		Long: "list dispatches gcloud.compute.instances.list against\n" +
			"connector_id=\"gcloud-rest-1.0\". Omit --zone for a project-wide\n" +
			"inventory via aggregatedList (one API call). Set --zone to\n" +
			"restrict to a specific zone.\n\n" +
			"The response is a rows+total envelope compatible with the\n" +
			"JSONFlux reducer — large projects may return a handle instead\n" +
			"of inline rows when the reducer is active.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho gcloud compute instances list --target rdc-gcp-dev\n" +
			"  meho gcloud compute instances list --target rdc-gcp-dev --zone europe-west3-a\n" +
			"  meho gcloud compute instances list --target rdc-gcp-dev --json | jq '.result.total'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runComputeInstancesList(cmd, targetName, jsonOut, backplaneOverride, zone)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.Flags().StringVar(&zone, "zone", "",
		"optional zone filter (e.g. europe-west3-a); omit to list all zones via aggregatedList")
	return cmd
}

func runComputeInstancesList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
	zone string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	var params map[string]any
	if zone != "" {
		params = map[string]any{"zone": zone}
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.compute.instances.list", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.compute.instances.list", r, jsonOut, printComputeInstancesList)
}

// printComputeInstancesList renders the instance list. Each row carries
// zone, name, machine_type, status, internal_ips, external_ips,
// creation_timestamp.
func printComputeInstancesList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.compute.instances.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(rows) == 0 {
		fmt.Fprintln(w, "  (0 instances)")
		return
	}
	fmt.Fprintf(w, "%-30s %-25s %-25s %-10s %s\n", "zone", "name", "machine_type", "status", "internal_ips")
	for _, row := range rows {
		zone := stringField(row, "zone")
		name := stringField(row, "name")
		mt := stringField(row, "machine_type")
		status := stringField(row, "status")
		// internal_ips is []string
		var ips []string
		if ipList, ok := row["internal_ips"].([]any); ok {
			for _, ip := range ipList {
				if s, ok := ip.(string); ok {
					ips = append(ips, s)
				}
			}
		}
		fmt.Fprintf(w, "%-30s %-25s %-25s %-10s %s\n",
			truncate(zone, 30),
			truncate(name, 25),
			truncate(mt, 25),
			truncate(status, 10),
			strings.Join(ips, ","),
		)
	}
}

// --- networks sub-group ---

// newComputeNetworksCmd returns the `meho gcloud compute networks`
// parent command with its list verb.
func newComputeNetworksCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "networks",
		Short:        "VPC network verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newComputeNetworksListCmd())
	return cmd
}

// newComputeNetworksListCmd returns `meho gcloud compute networks list`.
// Maps to op_id `gcloud.compute.networks.list`.
func newComputeNetworksListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List VPC networks in the project",
		Long: "list dispatches gcloud.compute.networks.list against\n" +
			"connector_id=\"gcloud-rest-1.0\". Returns one row per VPC\n" +
			"network with name, auto_create_subnetworks, routing_mode, mtu,\n" +
			"and creation_timestamp. Use as the first step in a network\n" +
			"topology audit before drilling into subnets.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho gcloud compute networks list --target rdc-gcp-dev\n" +
			"  meho gcloud compute networks list --target rdc-gcp-dev --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runComputeNetworksList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runComputeNetworksList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.compute.networks.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.compute.networks.list", r, jsonOut, printComputeNetworksList)
}

// printComputeNetworksList renders the VPC network list. Each row
// carries name, auto_create_subnetworks, routing_mode, mtu,
// creation_timestamp.
func printComputeNetworksList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.compute.networks.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(rows) == 0 {
		fmt.Fprintln(w, "  (0 networks)")
		return
	}
	fmt.Fprintf(w, "%-30s %-8s %-20s %s\n", "name", "auto", "routing_mode", "mtu")
	for _, row := range rows {
		name := stringField(row, "name")
		auto := boolField(row, "auto_create_subnetworks")
		routing := stringField(row, "routing_mode")
		mtu := ""
		if v, ok := row["mtu"]; ok && v != nil {
			mtu = fmt.Sprintf("%v", v)
		}
		autoStr := "false"
		if auto {
			autoStr = "true"
		}
		fmt.Fprintf(w, "%-30s %-8s %-20s %s\n",
			truncate(name, 30),
			autoStr,
			truncate(routing, 20),
			mtu,
		)
	}
}

// --- subnets sub-group ---

// newComputeSubnetsCmd returns the `meho gcloud compute subnets`
// parent command with its list verb.
func newComputeSubnetsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "subnets",
		Short:        "VPC subnet verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newComputeSubnetsListCmd())
	return cmd
}

// newComputeSubnetsListCmd returns `meho gcloud compute subnets list`.
// Maps to op_id `gcloud.compute.subnetworks.list`.
func newComputeSubnetsListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
		region            string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List VPC subnets (all regions or a specific region)",
		Long: "list dispatches gcloud.compute.subnetworks.list against\n" +
			"connector_id=\"gcloud-rest-1.0\". Omit --region for a\n" +
			"project-wide audit via aggregatedList. Set --region to restrict\n" +
			"to a specific region. Each row carries region, name, cidr_range,\n" +
			"network (parent VPC URL), purpose, and private_ip_google_access.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho gcloud compute subnets list --target rdc-gcp-dev\n" +
			"  meho gcloud compute subnets list --target rdc-gcp-dev --region europe-west3\n" +
			"  meho gcloud compute subnets list --target rdc-gcp-dev --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runComputeSubnetsList(cmd, targetName, jsonOut, backplaneOverride, region)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.Flags().StringVar(&region, "region", "",
		"optional region filter (e.g. europe-west3); omit to list all regions via aggregatedList")
	return cmd
}

func runComputeSubnetsList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
	region string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	var params map[string]any
	if region != "" {
		params = map[string]any{"region": region}
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.compute.subnetworks.list", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.compute.subnetworks.list", r, jsonOut, printComputeSubnetsList)
}

// printComputeSubnetsList renders the subnet list. Each row carries
// region, name, cidr_range, network, purpose, private_ip_google_access,
// creation_timestamp.
func printComputeSubnetsList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.compute.subnetworks.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(rows) == 0 {
		fmt.Fprintln(w, "  (0 subnets)")
		return
	}
	fmt.Fprintf(w, "%-20s %-30s %-20s %s\n", "region", "name", "cidr_range", "purpose")
	for _, row := range rows {
		region := stringField(row, "region")
		name := stringField(row, "name")
		cidr := stringField(row, "cidr_range")
		purpose := stringField(row, "purpose")
		fmt.Fprintf(w, "%-20s %-30s %-20s %s\n",
			truncate(region, 20),
			truncate(name, 30),
			truncate(cidr, 20),
			truncate(purpose, 30),
		)
	}
}
