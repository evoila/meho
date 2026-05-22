// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// Tenant-plane verb: `meho vcf-automation project list` -- projects
// within a tenant organization; the deployment-scoping construct.
func newProjectCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "project",
		Short:        "Tenant-plane VCFA projects (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newProjectListCmd())
	return cmd
}

func newProjectListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List tenant-plane projects on a VCFA appliance",
		Example:       "  meho vcf-automation project list --target rdc-vcfa",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTenantListVerb(cmd,
				"GET:/iaas/api/projects",
				targetName, jsonOut, backplaneOverride,
				printProjectList,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func printProjectList(w io.Writer, r *CallResult) {
	const opID = "GET:/iaas/api/projects"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeTenantListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 projects)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-30s  %-36s\n", "id", "name", "organization_id")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-30s  %-36s\n",
			truncate(vcfaStringField(e, "id"), 36),
			truncate(vcfaStringField(e, "name"), 30),
			truncate(vcfaStringField(e, "organizationId"), 36),
		)
	}
}

// runTenantListVerb is the shared dispatch path for tenant-plane list
// verbs (project / deployment / blueprint). Enforces --plane tenant
// (when supplied) and renders the result through the per-verb printer.
func runTenantListVerb(
	cmd *cobra.Command,
	opID, targetName string,
	jsonOut bool,
	backplaneOverride string,
	printer func(io.Writer, *CallResult),
) error {
	if se := validatePlane(readPlane(cmd), PlaneTenant); se != nil {
		return output.RenderError(cmd.ErrOrStderr(), se, jsonOut)
	}
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, readFqdn(cmd), nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printer)
}

// runTenantGetVerb dispatches a tenant-plane read-by-id op_id with the
// path-template parameter forwarded under `params[paramName]`.
func runTenantGetVerb(
	cmd *cobra.Command,
	opID, paramName, paramValue, targetName string,
	jsonOut bool,
	backplaneOverride string,
	printer func(io.Writer, *CallResult),
) error {
	if se := validatePlane(readPlane(cmd), PlaneTenant); se != nil {
		return output.RenderError(cmd.ErrOrStderr(), se, jsonOut)
	}
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{paramName: paramValue}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, readFqdn(cmd), params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printer)
}
