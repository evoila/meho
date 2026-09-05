// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import "fmt"

// principalLabel renders a principal for a single-line CLI field:
// "<name> (<sub>)" when the backplane resolved a display name, else the
// bare <sub>. The sub is always shown alongside the name, never replaced
// (#3300): it stays the stable, machine-truthful key, and the surface
// fails open to it when no name was recorded (name nil / empty).
func principalLabel(sub string, name *string) string {
	if name != nil && *name != "" {
		return fmt.Sprintf("%s (%s)", *name, sub)
	}
	return sub
}

// principalScanLabel renders a principal for a width-constrained table
// column, where "<name> (<sub>)" will not fit and the sub is already
// truncated below a full GUID. It shows the display name when one
// resolved (far more legible than a truncated UUID), else the sub. The
// full sub stays reachable via `meho approvals show` and `--json`
// (#3300) -- the same name-primary posture the audit list already takes.
func principalScanLabel(sub string, name *string) string {
	if name != nil && *name != "" {
		return *name
	}
	return sub
}
