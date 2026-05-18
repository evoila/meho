// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newFirewallCmd returns the `meho nsx firewall` parent command.
// Sub-tree: `policy list [--scope]` and `rule list <policy> [--scope]`.
func newFirewallCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "firewall",
		Short:        "NSX distributed-firewall verbs (policy list / rule list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFirewallPolicyCmd())
	cmd.AddCommand(newFirewallRuleCmd())
	return cmd
}

// newFirewallPolicyCmd returns the `meho nsx firewall policy` sub-tree.
func newFirewallPolicyCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "policy",
		Short:        "NSX distributed-firewall policy verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFirewallPolicyListCmd())
	return cmd
}

// newFirewallPolicyListCmd returns `meho nsx firewall policy list`.
// Dispatches GET:/policy/api/v1/infra/domains/{domain-id}/security-policies
// with the --scope flag mapped to the `domain-id` path parameter
// (default "default" — the standard NSX policy domain).
func newFirewallPolicyListCmd() *cobra.Command {
	var (
		scopeFlag         string
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List distributed-firewall security policies in a domain",
		Long: "list dispatches GET:/policy/api/v1/infra/domains/{domain-id}/\n" +
			"security-policies against connector_id=\"nsx-rest-4.2\".\n" +
			"--scope sets the domain-id path parameter (default \"default\").\n" +
			"Renders id / display_name / category for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx firewall policy list --target rdc-nsx\n" +
			"  meho nsx firewall policy list --scope default --target rdc-nsx\n" +
			"  meho nsx firewall policy list --target rdc-nsx --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runFirewallPolicyList(cmd, scopeFlag, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&scopeFlag, "scope", "default",
		"NSX policy domain-id (default \"default\")")
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runFirewallPolicyList(cmd *cobra.Command, scope, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	if scope == "" {
		scope = "default"
	}
	const opID = "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies"
	params := map[string]any{"domain-id": scope}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printFirewallPolicyList)
}

func printFirewallPolicyList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s firewall policy list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeNsxListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 security policies)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-16s\n", "id", "display_name", "category")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-30s %-16s\n",
			truncate(nsxStringField(e, "id"), 38),
			truncate(nsxStringField(e, "display_name"), 30),
			truncate(nsxStringField(e, "category"), 16),
		)
	}
}

// newFirewallRuleCmd returns the `meho nsx firewall rule` sub-tree.
func newFirewallRuleCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "rule",
		Short:        "NSX distributed-firewall rule verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFirewallRuleListCmd())
	return cmd
}

// newFirewallRuleListCmd returns `meho nsx firewall rule list <policy>`.
// Dispatches GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules
// with the <policy> positional arg mapped to `security-policy-id` and
// --scope mapped to `domain-id` (default "default").
func newFirewallRuleListCmd() *cobra.Command {
	var (
		scopeFlag         string
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list <policy-id>",
		Short: "List rules in a distributed-firewall security policy",
		Long: "list dispatches GET:/policy/api/v1/infra/domains/{domain-id}/\n" +
			"security-policies/{security-policy-id}/rules against\n" +
			"connector_id=\"nsx-rest-4.2\".\n" +
			"<policy-id> is the security-policy-id path parameter.\n" +
			"--scope sets the domain-id (default \"default\").\n" +
			"Renders id / display_name / action for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx firewall rule list policy-app-tier --target rdc-nsx\n" +
			"  meho nsx firewall rule list policy-app-tier --scope default --target rdc-nsx\n" +
			"  meho nsx firewall rule list policy-app-tier --target rdc-nsx --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runFirewallRuleList(cmd, args[0], scopeFlag, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&scopeFlag, "scope", "default",
		"NSX policy domain-id (default \"default\")")
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runFirewallRuleList(cmd *cobra.Command, policyID, scope, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	if scope == "" {
		scope = "default"
	}
	const opID = "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules"
	params := map[string]any{
		"domain-id":          scope,
		"security-policy-id": policyID,
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printFirewallRuleList)
}

func printFirewallRuleList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s firewall rule list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeNsxListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 rules)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-10s\n", "id", "display_name", "action")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-30s %-10s\n",
			truncate(nsxStringField(e, "id"), 38),
			truncate(nsxStringField(e, "display_name"), 30),
			truncate(nsxStringField(e, "action"), 10),
		)
	}
}
