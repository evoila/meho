// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newDomainCmd returns the `meho sddc-manager domain` sub-tree.
func newDomainCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "domain",
		Short:        "VCF domain operations (list / info)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newDomainListCmd())
	cmd.AddCommand(newDomainInfoCmd())
	return cmd
}

func newDomainListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List VCF domains (management + workload)",
		Long: "list dispatches GET:/v1/domains against connector_id=\"sddc-rest-9.0\".\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho sddc-manager domain list --target rdc-sddc-manager",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runDomainList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runDomainList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/v1/domains", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/v1/domains", r, jsonOut, printDomainList)
}

func printDomainList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		printGenericResult(w, "GET:/v1/domains", r)
		return
	}
	fmt.Fprintf(w, "VCF domains (%d)\n", len(entries))
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 domains)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-32s  %s\n", "id", "name", "type")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-32s  %s\n",
			truncate(sddcStringField(e, "id"), 36),
			truncate(sddcStringField(e, "name"), 32),
			sddcStringField(e, "type"),
		)
	}
}

func newDomainInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <domain-id>",
		Short: "Show full detail for one VCF domain",
		Long: "info dispatches GET:/v1/domains/{id} against connector_id=\"sddc-rest-9.0\".\n" +
			"Requires a domain id from `sddc-manager domain list`.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho sddc-manager domain info domain-mgmt --target rdc-sddc-manager",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDomainInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runDomainInfo(cmd *cobra.Command, domainID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"id": domainID}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/v1/domains/{id}", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/v1/domains/{id}", r, jsonOut, printDomainInfo)
}

func printDomainInfo(w io.Writer, r *CallResult) {
	if r.Status != "ok" {
		printGenericResult(w, "GET:/v1/domains/{id}", r)
		return
	}
	var d struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Type     string `json:"type"`
		VCenters []struct {
			ID   string `json:"id"`
			FQDN string `json:"fqdn"`
		} `json:"vcenters"`
		NsxtCluster *struct {
			ID      string `json:"id"`
			VipFQDN string `json:"vipFqdn"`
		} `json:"nsxtCluster"`
		Clusters []struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"clusters"`
		SSOID   string `json:"ssoId"`
		SSOName string `json:"ssoName"`
	}
	if err := jsonUnmarshalStrict(r.Result, &d); err != nil || d.ID == "" {
		printGenericResult(w, "GET:/v1/domains/{id}", r)
		return
	}
	fmt.Fprintf(w, "domain:  %s (%s) — type=%s\n", d.Name, d.ID, d.Type)
	if len(d.VCenters) > 0 {
		fmt.Fprintln(w, "vcenters:")
		for _, vc := range d.VCenters {
			fmt.Fprintf(w, "  %s  %s\n", vc.ID, vc.FQDN)
		}
	}
	if d.NsxtCluster != nil {
		fmt.Fprintf(w, "nsx_cluster: %s  vip=%s\n", d.NsxtCluster.ID, d.NsxtCluster.VipFQDN)
	}
	if len(d.Clusters) > 0 {
		fmt.Fprintln(w, "clusters:")
		for _, c := range d.Clusters {
			fmt.Fprintf(w, "  %-36s  %s\n", c.ID, c.Name)
		}
	}
	if d.SSOID != "" || d.SSOName != "" {
		fmt.Fprintf(w, "sso: id=%s  name=%s\n", d.SSOID, d.SSOName)
	}
}
