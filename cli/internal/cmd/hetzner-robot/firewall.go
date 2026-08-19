// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newFirewallCmd returns `meho hetzner-robot firewall` with the get subcommand.
func newFirewallCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "firewall",
		Short:        "Read the packet-filter firewall of a dedicated server",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFirewallGetCmd())
	return cmd
}

func newFirewallGetCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get <server-ip>",
		Short: "Show the packet-filter firewall for one dedicated server by its primary IP",
		Long: "get dispatches GET:/firewall/{server-ip} against\n" +
			"connector_id=\"hetzner-rest-2026.04\" and renders the firewall status,\n" +
			"the Hetzner-services allowlist flag, and the ordered input/output rules.\n" +
			"<server-ip> is the primary IP from 'meho hetzner-robot server list'.\n" +
			"Use it to verify a server's edge firewall after the onboarding template\n" +
			"is applied (operator source allowed on the expected ports, final\n" +
			"default-discard). --json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho hetzner-robot firewall get 1.2.3.4 --target rdc-robot\n" +
			"  meho hetzner-robot firewall get 1.2.3.4 --target rdc-robot --json | jq '.result.firewall.rules.input[]'",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runFirewallGet(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Hetzner Robot target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runFirewallGet(cmd *cobra.Command, serverIP, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	opID := "GET:/firewall/{server-ip}"
	params := map[string]any{"server-ip": serverIP}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printFirewallGet)
}

func printFirewallGet(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/firewall/{server-ip} — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	fw := decodeFirewall(r.Result)
	if fw == nil {
		fallbackResultRender(w, r)
		return
	}
	printFirewallFields(w, fw)
}

// decodeFirewall extracts the firewall object from either a {"firewall": {...}}
// wrapper or a bare object carrying recognizable firewall fields. Returns nil
// when the payload is neither (e.g. the sandbox's empty {}).
func decodeFirewall(raw json.RawMessage) map[string]any {
	var wrapper struct {
		Firewall map[string]any `json:"firewall"`
	}
	if err := jsonUnmarshalStrict(raw, &wrapper); err == nil && wrapper.Firewall != nil {
		return wrapper.Firewall
	}
	var bare map[string]any
	if err := jsonUnmarshalStrict(raw, &bare); err == nil {
		if _, ok := bare["rules"]; ok {
			return bare
		}
		if _, ok := bare["status"]; ok {
			return bare
		}
	}
	return nil
}

func printFirewallFields(w io.Writer, fw map[string]any) {
	printStringField(w, "status", "status", fw)
	if v, ok := fw["whitelist_hos"].(bool); ok {
		fmt.Fprintf(w, "  %-14s %v\n", "whitelist_hos:", v)
	}
	if v, ok := fw["filter_ipv6"].(bool); ok {
		fmt.Fprintf(w, "  %-14s %v\n", "filter_ipv6:", v)
	}
	printStringField(w, "port", "port", fw)
	rules, _ := fw["rules"].(map[string]any)
	if rules == nil {
		return
	}
	printFirewallRuleSet(w, "input", rules)
	printFirewallRuleSet(w, "output", rules)
}

func printFirewallRuleSet(w io.Writer, direction string, rules map[string]any) {
	arr, _ := rules[direction].([]any)
	fmt.Fprintf(w, "  %s rules (%d):\n", direction, len(arr))
	for _, item := range arr {
		rule, ok := item.(map[string]any)
		if !ok {
			continue
		}
		fmt.Fprintf(w, "    - %-20s action=%-8s proto=%-5s src_ip=%-20s dst_port=%s\n",
			truncate(firewallRuleField(rule, "name", "(unnamed)"), 20),
			firewallRuleField(rule, "action", "?"),
			firewallRuleField(rule, "protocol", "any"),
			truncate(firewallRuleField(rule, "src_ip", "any"), 20),
			firewallRuleField(rule, "dst_port", "any"),
		)
	}
}

// firewallRuleField returns rule[key] as a string, or fallback when the field
// is absent, non-string, or empty. Hetzner emits JSON null for unset rule
// fields (src_ip, dst_port, protocol on an allow-all rule), which decode to a
// nil interface — the fallback keeps the rendered row aligned.
func firewallRuleField(rule map[string]any, key, fallback string) string {
	if v, ok := rule[key].(string); ok && v != "" {
		return v
	}
	return fallback
}
