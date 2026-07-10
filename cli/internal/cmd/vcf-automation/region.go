// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"
)

// Provider-plane verb: `meho vcf-automation region ...`. The VCFA 9
// "region" surface is the evolution of the vCloud-Director provider-VDC
// concept (each region maps to one NSX domain plus a collection of
// supervisors backed by one or more VCF workload domains).
func newRegionCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "region",
		Short:        "Provider-plane VCFA regions (list / get)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRegionListCmd())
	cmd.AddCommand(newRegionGetCmd())
	return cmd
}

func newRegionListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List provider-plane regions on a VCFA appliance",
		Example:       "  meho vcf-automation region list --target rdc-vcfa",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runProviderListVerb(cmd,
				"vcfa.provider.region.list",
				targetName, jsonOut, backplaneOverride,
				printRegionList,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func newRegionGetCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "get <id>",
		Short:         "Read one provider-plane region by id",
		Example:       "  meho vcf-automation region get r-1234 --target rdc-vcfa",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProviderGetVerb(cmd,
				"GET:/cloudapi/1.0.0/regions/{id}",
				"id", args[0],
				targetName, jsonOut, backplaneOverride,
				printRegionGet,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func printRegionList(w io.Writer, r *CallResult) {
	const opID = "vcfa.provider.region.list"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeProviderListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 regions)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-30s  %-10s\n", "id", "name", "enabled")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-30s  %-10v\n",
			truncate(vcfaStringField(e, "id"), 36),
			truncate(vcfaStringField(e, "name"), 30),
			e["isEnabled"],
		)
	}
}

func printRegionGet(w io.Writer, r *CallResult) {
	const opID = "GET:/cloudapi/1.0.0/regions/{id}"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	var region struct {
		ID          string `json:"id"`
		Name        string `json:"name"`
		Description string `json:"description"`
		IsEnabled   bool   `json:"isEnabled"`
		NsxManager  struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"nsxManager"`
	}
	if err := jsonUnmarshalStrict(r.Result, &region); err != nil || region.ID == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  id:          %s\n", region.ID)
	if region.Name != "" {
		fmt.Fprintf(w, "  name:        %s\n", region.Name)
	}
	fmt.Fprintf(w, "  is_enabled:  %v\n", region.IsEnabled)
	if region.NsxManager.Name != "" {
		fmt.Fprintf(w, "  nsx_manager: %s (%s)\n", region.NsxManager.Name, region.NsxManager.ID)
	}
	if region.Description != "" {
		fmt.Fprintf(w, "  description: %s\n", region.Description)
	}
}
