// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"fmt"
	"io"
	"strings"
	"text/tabwriter"

	"github.com/evoila/meho/cli/internal/api"
)

// PrintTargetsTable renders a list of TargetSummary rows as a tabwriter table.
// Columns: NAME, ALIASES, PRODUCT, HOST.
func PrintTargetsTable(w io.Writer, targets []api.TargetSummary) error {
	if len(targets) == 0 {
		fmt.Fprintln(w, "No targets found.")
		return nil
	}
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tALIASES\tPRODUCT\tHOST")
	for _, t := range targets {
		aliases := strings.Join(t.Aliases, ",")
		if aliases == "" {
			aliases = "-"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", t.Name, aliases, t.Product, t.Host)
	}
	return tw.Flush()
}

// PrintTarget renders a full Target as a key-value summary.
func PrintTarget(w io.Writer, t *api.Target) error {
	fmt.Fprintf(w, "Name:        %s\n", t.Name)
	if len(t.Aliases) > 0 {
		fmt.Fprintf(w, "Aliases:     %s\n", strings.Join(t.Aliases, ", "))
	}
	fmt.Fprintf(w, "Product:     %s\n", t.Product)
	fmt.Fprintf(w, "Host:        %s\n", t.Host)
	if t.Port != nil {
		fmt.Fprintf(w, "Port:        %d\n", *t.Port)
	}
	if t.FQDN != nil {
		fmt.Fprintf(w, "FQDN:        %s\n", *t.FQDN)
	}
	fmt.Fprintf(w, "Auth model:  %s\n", t.AuthModel)
	fmt.Fprintf(w, "VPN:         %v\n", t.VPNRequired)
	if t.Notes != nil && *t.Notes != "" {
		fmt.Fprintf(w, "Notes:       %s\n", *t.Notes)
	}
	fmt.Fprintf(w, "ID:          %s\n", t.ID)
	fmt.Fprintf(w, "Tenant:      %s\n", t.TenantID)
	fmt.Fprintf(w, "Created:     %s\n", t.CreatedAt)
	fmt.Fprintf(w, "Updated:     %s\n", t.UpdatedAt)
	return nil
}

// PrintProbeResult renders a ProbeResult for human consumption.
func PrintProbeResult(w io.Writer, pr *api.ProbeResult) error {
	status := "ok"
	if !pr.OK {
		status = "failed"
	}
	fmt.Fprintf(w, "Probe:     %s\n", status)
	if pr.Reason != nil && *pr.Reason != "" {
		fmt.Fprintf(w, "Reason:    %s\n", *pr.Reason)
	}
	if pr.LatencyMs != nil {
		fmt.Fprintf(w, "Latency:   %.1f ms\n", *pr.LatencyMs)
	}
	fmt.Fprintf(w, "Probed at: %s\n", pr.ProbedAt)
	return nil
}

// PrintTargetNearMisses renders the near-miss suggestions from a 404 detail.
func PrintTargetNearMisses(w io.Writer, query string, matches []api.TargetSummary) {
	fmt.Fprintf(w, "Target %q not found.\n", query)
	if len(matches) == 0 {
		return
	}
	fmt.Fprintln(w, "Did you mean one of:")
	for _, m := range matches {
		if len(m.Aliases) > 0 {
			fmt.Fprintf(w, "  %s  (aliases: %s)\n", m.Name, strings.Join(m.Aliases, ", "))
		} else {
			fmt.Fprintf(w, "  %s\n", m.Name)
		}
	}
}

// PrintAmbiguousTarget renders the ambiguous-alias matches from a 409 detail.
func PrintAmbiguousTarget(w io.Writer, query string, matches []api.TargetSummary) {
	fmt.Fprintf(w, "Ambiguous query %q matched multiple targets:\n", query)
	for _, m := range matches {
		fmt.Fprintf(w, "  %s  (product: %s, host: %s)\n", m.Name, m.Product, m.Host)
	}
	fmt.Fprintln(w, "Use the canonical target name instead of the alias.")
}
