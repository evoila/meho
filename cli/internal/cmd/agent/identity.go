// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import "fmt"

// principalLabel renders a granted principal for a single-line CLI
// field: "<name> (<sub>)" when the backplane resolved a display name
// (the agent principal's operator handle), else the bare <sub>. The sub
// is always shown alongside the name, never replaced (#3337): it stays
// the stable, machine-truthful key, and the surface fails open to it
// when no handle resolved (name nil / empty). Mirrors the approvals CLI
// `principalLabel` (#3300) — the shared name-alongside-id posture — kept
// package-local so the two verb trees stay decoupled.
func principalLabel(sub string, name *string) string {
	if name != nil && *name != "" {
		return fmt.Sprintf("%s (%s)", *name, sub)
	}
	return sub
}
