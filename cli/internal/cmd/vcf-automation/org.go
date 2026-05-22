// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// Provider-plane verb: `meho vcf-automation org ...`. Two read-only
// subcommands cover the v0.5 core's two organization ops.
func newOrgCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "org",
		Short:        "Provider-plane VCFA organizations (list / get)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newOrgListCmd())
	cmd.AddCommand(newOrgGetCmd())
	return cmd
}

func newOrgListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List provider-plane organizations on a VCFA appliance",
		Long: "list dispatches GET:/cloudapi/1.0.0/orgs against\n" +
			"connector_id=\"vcfa-rest-9.0\" on the provider plane.\n" +
			"--plane provider is implicit; passing --plane tenant\n" +
			"errors early.",
		Example:       "  meho vcf-automation org list --target rdc-vcfa",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runProviderListVerb(cmd,
				"GET:/cloudapi/1.0.0/orgs",
				targetName, jsonOut, backplaneOverride,
				printOrgList,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func newOrgGetCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get <id>",
		Short: "Read one provider-plane organization by id",
		Long: "get dispatches GET:/cloudapi/1.0.0/orgs/{id} against\n" +
			"connector_id=\"vcfa-rest-9.0\" on the provider plane.\n" +
			"<id> is forwarded as the `id` path-template parameter.",
		Example:       "  meho vcf-automation org get a1b2c3d4 --target rdc-vcfa",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProviderGetVerb(cmd,
				"GET:/cloudapi/1.0.0/orgs/{id}",
				"id", args[0],
				targetName, jsonOut, backplaneOverride,
				printOrgGet,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func printOrgList(w io.Writer, r *CallResult) {
	const opID = "GET:/cloudapi/1.0.0/orgs"
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
		fmt.Fprintln(w, "(0 organizations)")
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

func printOrgGet(w io.Writer, r *CallResult) {
	const opID = "GET:/cloudapi/1.0.0/orgs/{id}"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	var org struct {
		ID           string `json:"id"`
		Name         string `json:"name"`
		DisplayName  string `json:"displayName"`
		Description  string `json:"description"`
		IsEnabled    bool   `json:"isEnabled"`
		OrgVdcCount  int    `json:"orgVdcCount"`
		UserCount    int    `json:"userCount"`
		CatalogCount int    `json:"catalogCount"`
	}
	if err := jsonUnmarshalStrict(r.Result, &org); err != nil || org.ID == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  id:            %s\n", org.ID)
	if org.Name != "" {
		fmt.Fprintf(w, "  name:          %s\n", org.Name)
	}
	if org.DisplayName != "" {
		fmt.Fprintf(w, "  display_name:  %s\n", org.DisplayName)
	}
	fmt.Fprintf(w, "  is_enabled:    %v\n", org.IsEnabled)
	fmt.Fprintf(w, "  org_vdc_count: %d\n", org.OrgVdcCount)
	fmt.Fprintf(w, "  user_count:    %d\n", org.UserCount)
	fmt.Fprintf(w, "  catalog_count: %d\n", org.CatalogCount)
}

// addStandardFlags wires the --target / --json / --backplane flags
// shared by every dispatch verb. Centralised so the per-verb body
// stays narrow.
func addStandardFlags(cmd *cobra.Command, targetName, backplaneOverride *string, jsonOut *bool) {
	// Note: positional layout matches the variadic call sites; the
	// signature is (cmd, target, json, backplane) on the call side
	// for readability.
	cmd.Flags().StringVar(targetName, "target", "", "VCFA target slug")
	cmd.Flags().BoolVar(jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
}

// runProviderListVerb is the shared dispatch path for provider-plane
// list verbs that take no positional arguments. It enforces the
// `--plane provider` constraint and renders the result through the
// per-verb printer.
func runProviderListVerb(
	cmd *cobra.Command,
	opID, targetName string,
	jsonOut bool,
	backplaneOverride string,
	printer func(io.Writer, *CallResult),
) error {
	if se := validatePlane(readPlane(cmd), PlaneProvider); se != nil {
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

// runProviderGetVerb is the shared dispatch path for provider-plane
// read-by-id verbs. The path-template parameter is forwarded via
// `params[paramName] = id` — the dispatcher's `_substitute_path`
// fills the `{name}` placeholder.
func runProviderGetVerb(
	cmd *cobra.Command,
	opID, paramName, paramValue, targetName string,
	jsonOut bool,
	backplaneOverride string,
	printer func(io.Writer, *CallResult),
) error {
	if se := validatePlane(readPlane(cmd), PlaneProvider); se != nil {
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
