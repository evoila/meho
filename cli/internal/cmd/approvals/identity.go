// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"fmt"

	"github.com/evoila/meho/cli/internal/output"
)

// principalLabel renders a principal for a single-line CLI field:
// "<name> (<sub>)" when the backplane resolved a display name, else the
// bare <sub>. The sub is always shown alongside the name, never replaced
// (#3300): it stays the stable, machine-truthful key, and the surface
// fails open to it when no name was recorded (name nil / empty).
func principalLabel(sub string, name *string) string {
	if name != nil && *name != "" {
		return output.SafeText(fmt.Sprintf("%s (%s)", *name, sub))
	}
	return output.SafeText(sub)
}

// principalScanLabel renders a principal for a width-constrained table
// column, where "<name> (<sub>)" will not fit and the sub is already
// truncated below a full GUID. It shows the display name when one
// resolved (far more legible than a truncated UUID), else the sub. The
// full sub stays reachable via `meho approvals show` and `--json`
// (#3300) -- the same name-primary posture the audit list already takes.
func principalScanLabel(sub string, name *string) string {
	if name != nil && *name != "" {
		return output.SafeText(*name)
	}
	return output.SafeText(sub)
}

// machineKinds are the principal_kind values of a machine login: an
// agent, a service account or a check runner, not a person (#3885).
var machineKinds = map[string]bool{"agent": true, "service": true, "runner": true}

// machineKind returns the kind when a machine login made the request,
// else "". A nil kind (a request made before the server saved it) and
// "user" both give "".
func machineKind(kind *string) string {
	if kind != nil && machineKinds[*kind] {
		return *kind
	}
	return ""
}

// requesterLabel is principalLabel for the requester, with the kind in
// brackets after it when a machine login asked: "triage (<sub>) [agent]".
func requesterLabel(sub string, name, kind *string) string {
	label := principalLabel(sub, name)
	if k := machineKind(kind); k != "" {
		return label + " [" + k + "]"
	}
	return label
}

// requesterScanLabel is principalScanLabel for the requester. The kind
// comes first ("[agent] triage"), so a cut column still shows it.
func requesterScanLabel(sub string, name, kind *string) string {
	label := principalScanLabel(sub, name)
	if k := machineKind(kind); k != "" {
		return "[" + k + "] " + label
	}
	return label
}
