// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newNetworkingCmd returns the `meho holodeck networking` parent with
// one sub-verb: `show` (holodeck.networking.show).
func newNetworkingCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "networking",
		Short:        "Holodeck networking sub-verbs (show)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNetworkingShowCmd())
	return cmd
}

// newNetworkingShowCmd returns the `meho holodeck networking show`
// command.
//
// Maps to op_id `holodeck.networking.show`. Runs four sub-commands
// over the pooled SSH connection on the appliance:
//
//  1. `vtysh -c 'show bgp summary'` (FRR/BGP peer summary).
//  2. `vtysh -c 'show ip route'` (kernel routes as FRR sees them).
//  3. `pwsh` of `Get-DnsServerZone | Select-Object ZoneName,ZoneType
//     | ConvertTo-Json -Depth 4` (DNS zone summary).
//  4. `cat /var/lib/dhcp/dhcpd.leases` (raw DHCP leases file).
//
// Returns the composed envelope with four narrative sub-sections.
func newNetworkingShowCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show",
		Short: "Composite FRR/BGP + DNS + DHCP snapshot for the appliance",
		Long: "show dispatches holodeck.networking.show and returns a\n" +
			"composite envelope with four sub-sections:\n" +
			"  - bgp     — FRR/BGP peer summary text (vtysh)\n" +
			"  - routes  — kernel routing table text (vtysh)\n" +
			"  - dns     — DNS zone summary (parsed JSON via pwsh)\n" +
			"  - dhcp    — raw DHCP leases file content\n\n" +
			"Each sub-section carries an `ok` boolean so a single-component\n" +
			"failure does not blank the whole response. Pair with\n" +
			"`meho holodeck logs tail frr` for FRR log drill-in.\n\n" +
			"The human render lists each sub-section's ok flag and the\n" +
			"first few lines of any text payload; --json emits the full\n" +
			"OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho holodeck networking show --target holorouter-hetzner-dc\n" +
			"  meho holodeck networking show --target holorouter-hetzner-dc --json | jq .result.bgp",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNetworkingShow(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runNetworkingShow(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.networking.show", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.networking.show", r, jsonOut, printNetworkingShow)
}

func printNetworkingShow(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.networking.show — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	flat, err := decodeFlatResult(r.Result)
	if err != nil || flat == nil {
		fallbackResultRender(w, r)
		return
	}
	// BGP / routes / DHCP carry textual `summary_text` / `text` /
	// `leases_text`; DNS carries a structured `zones` list + `total`.
	renderTextSection(w, "bgp", flat["bgp"], "summary_text", 8)
	renderTextSection(w, "routes", flat["routes"], "text", 8)
	renderDNSSection(w, flat["dns"])
	renderTextSection(w, "dhcp", flat["dhcp"], "leases_text", 6)
}

// renderTextSection prints a `{summary_text, ok}`-shaped sub-section
// with a capped line preview. The first non-empty line count is
// surfaced; --json gives operators full content.
func renderTextSection(w io.Writer, label string, raw any, textKey string, previewLines int) {
	section, ok := raw.(map[string]any)
	if !ok || section == nil {
		fmt.Fprintf(w, "  %s: (missing)\n", label)
		return
	}
	okFlag, _ := section["ok"].(bool)
	text, _ := section[textKey].(string)
	if !okFlag {
		fmt.Fprintf(w, "  %s: ok=false (empty / cmd failed)\n", label)
		return
	}
	lines := splitLines(text)
	limit := len(lines)
	truncated := false
	if limit > previewLines {
		limit = previewLines
		truncated = true
	}
	fmt.Fprintf(w, "  %s: ok=true (%d lines)\n", label, len(lines))
	for _, line := range lines[:limit] {
		fmt.Fprintln(w, "    "+line)
	}
	if truncated {
		fmt.Fprintf(w, "    … (%d more lines — use --json to inspect all)\n", len(lines)-previewLines)
	}
}

// renderDNSSection prints the `{zones, total, ok}` DNS sub-section.
func renderDNSSection(w io.Writer, raw any) {
	section, ok := raw.(map[string]any)
	if !ok || section == nil {
		fmt.Fprintln(w, "  dns: (missing)")
		return
	}
	okFlag, _ := section["ok"].(bool)
	total, _ := section["total"].(float64)
	if !okFlag {
		fmt.Fprintf(w, "  dns: ok=false (zones=%d)\n", int(total))
		return
	}
	fmt.Fprintf(w, "  dns: ok=true (zones=%d)\n", int(total))
	zones, _ := section["zones"].([]any)
	for i, zone := range zones {
		if i >= 10 {
			fmt.Fprintf(w, "    … (%d more zones — use --json to inspect all)\n", len(zones)-10)
			break
		}
		entry, ok := zone.(map[string]any)
		if !ok {
			continue
		}
		name := stringField(entry, "ZoneName")
		ztype := stringField(entry, "ZoneType")
		fmt.Fprintf(w, "    %-30s %s\n", name, ztype)
	}
}
