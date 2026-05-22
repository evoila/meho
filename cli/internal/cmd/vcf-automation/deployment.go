// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"
)

// Tenant-plane verb: `meho vcf-automation deployment ...` -- the
// largest payload on the tenant surface; the dispatcher's JSONFlux
// seam wraps oversized responses in a ResultHandle (use `result_describe`
// / `result_query` to navigate).
func newDeploymentCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "deployment",
		Short:        "Tenant-plane VCFA catalog deployments (list / get)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newDeploymentListCmd())
	cmd.AddCommand(newDeploymentGetCmd())
	return cmd
}

func newDeploymentListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List tenant-plane catalog deployments",
		Example:       "  meho vcf-automation deployment list --target rdc-vcfa",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTenantListVerb(cmd,
				"GET:/iaas/api/deployments",
				targetName, jsonOut, backplaneOverride,
				printDeploymentList,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func newDeploymentGetCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "get <id>",
		Short:         "Read one tenant-plane deployment by id",
		Example:       "  meho vcf-automation deployment get dep-1234 --target rdc-vcfa",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runTenantGetVerb(cmd,
				"GET:/iaas/api/deployments/{id}",
				"id", args[0],
				targetName, jsonOut, backplaneOverride,
				printDeploymentGet,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func printDeploymentList(w io.Writer, r *CallResult) {
	const opID = "GET:/iaas/api/deployments"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	// JSONFlux handle path: when the dispatcher wraps the payload in
	// a ResultHandle, the result envelope's `handle` field is set and
	// the operator should use `result_describe` / `result_query`.
	// Renders a one-liner hint so the handle isn't silently dropped.
	entries, err := decodeTenantListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 deployments)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-30s  %-15s  %-36s\n", "id", "name", "status", "blueprintId")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-30s  %-15s  %-36s\n",
			truncate(vcfaStringField(e, "id"), 36),
			truncate(vcfaStringField(e, "name"), 30),
			truncate(vcfaStringField(e, "status"), 15),
			truncate(vcfaStringField(e, "blueprintId"), 36),
		)
	}
}

func printDeploymentGet(w io.Writer, r *CallResult) {
	const opID = "GET:/iaas/api/deployments/{id}"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	var d struct {
		ID          string `json:"id"`
		Name        string `json:"name"`
		Status      string `json:"status"`
		ProjectID   string `json:"projectId"`
		BlueprintID string `json:"blueprintId"`
		OwnedBy     string `json:"ownedBy"`
	}
	if err := jsonUnmarshalStrict(r.Result, &d); err != nil || d.ID == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  id:           %s\n", d.ID)
	if d.Name != "" {
		fmt.Fprintf(w, "  name:         %s\n", d.Name)
	}
	if d.Status != "" {
		fmt.Fprintf(w, "  status:       %s\n", d.Status)
	}
	if d.ProjectID != "" {
		fmt.Fprintf(w, "  project_id:   %s\n", d.ProjectID)
	}
	if d.BlueprintID != "" {
		fmt.Fprintf(w, "  blueprint_id: %s\n", d.BlueprintID)
	}
	if d.OwnedBy != "" {
		fmt.Fprintf(w, "  owned_by:     %s\n", d.OwnedBy)
	}
}
