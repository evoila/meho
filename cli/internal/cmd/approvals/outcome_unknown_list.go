// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// printOutcomeUnknownTable renders `meho approvals list --outcome-unknown`
// (#5026): what ran, on which target, when the latest run started, who
// asked and who approved.
// Columns: ID, WHY, OP, TARGET, STARTED, PRINCIPAL, APPROVED-BY. WHY is
// "uncertain" or "no-result". The advice follows the table.
func printOutcomeUnknownTable(w io.Writer, items []api.ApprovalRequestView, offset int) {
	if len(items) == 0 {
		if offset > 0 {
			fmt.Fprintf(w, "no approved changes with an unknown outcome after skipping %d (use a smaller --offset)\n", offset)
			return
		}
		fmt.Fprintln(w, "no approved changes with an unknown outcome")
		return
	}
	fmt.Fprintf(w, "%-36s %-10s %-32s %-36s %-32s %-20s %s\n",
		"ID", "WHY", "OP", "TARGET", "STARTED", "PRINCIPAL", "APPROVED-BY")
	for _, e := range items {
		target := "-"
		if e.TargetId != nil {
			target = e.TargetId.String()
		}
		// STARTED is when the latest run started (the latest re-run, else
		// the first run): the time the list counts its minutes from.
		started := "-"
		if e.OutcomeUnknown != nil && e.OutcomeUnknown.RunStartedAt != nil {
			started = *e.OutcomeUnknown.RunStartedAt
		}
		approvedBy := "-"
		if e.ReviewedBy != nil {
			approvedBy = principalScanLabel(*e.ReviewedBy, e.ReviewedByName)
		}
		fmt.Fprintf(w, "%-36s %-10s %-32s %-36s %-32s %-20s %s\n",
			e.Id.String(),
			outcomeUnknownLabel(e.OutcomeUnknown),
			truncate(output.SafeText(e.OpId), 32),
			output.SafeText(target),
			output.SafeText(started),
			truncate(principalScanLabel(e.PrincipalSub, e.PrincipalName), 20),
			approvedBy,
		)
	}
	fmt.Fprintf(w, "\nShowing %d.\n", len(items))
	fmt.Fprintln(w, "MEHO does not know if these changes happened. Do not run them again. Check each target first,")
	fmt.Fprintln(w, "then record what you found: meho approvals mark-checked <id> --outcome happened|did-not-happen --note TEXT")
}

// outcomeUnknownLabel is the one-word reason of the WHY column.
func outcomeUnknownLabel(u *api.OutcomeUnknownView) string {
	if u == nil {
		return "-"
	}
	if u.Reason == api.OutcomeUnknownReasonStartedNoResult {
		return "no-result"
	}
	return output.SafeText(string(u.Reason))
}

// printOutcomeUnknown prints the advice for a request on the
// outcome-unknown list whose run started and stored no result (#5026). An
// uncertain run already has its warning line from printRunOutcome.
func printOutcomeUnknown(w io.Writer, d *api.ApprovalRequestView) {
	u := d.OutcomeUnknown
	if u == nil || u.Reason != api.OutcomeUnknownReasonStartedNoResult {
		return
	}
	fmt.Fprintln(w, "Warning:      "+output.SafeText(u.Advice))
}
