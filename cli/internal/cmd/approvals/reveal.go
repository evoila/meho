// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/api"
)

// revealNotice is the line every approval view prints when the requester
// asked for the hidden values of the answer (reveal_secret: true).
const revealNotice = "Hidden values: the approved run returns the secret values to the requester."

// printRevealNotice writes revealNotice, after indent, when the approved run
// of d returns the hidden values to the requester. It writes nothing
// otherwise.
func printRevealNotice(w io.Writer, indent string, d *api.ApprovalRequestView) {
	if d.RevealSecret != nil && *d.RevealSecret {
		fmt.Fprintf(w, "%s%s\n", indent, revealNotice)
	}
}
