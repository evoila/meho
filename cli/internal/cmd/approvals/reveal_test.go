// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"strings"
	"testing"
)

func TestShowNamesAReveal(t *testing.T) {
	d := newApprovalView(t, "pending", "")
	yes := true
	d.RevealSecret = &yes
	cmd, stdout, _ := newCapturingCmd(t)

	printDetail(cmd, &d)

	if !strings.HasPrefix(stdout.String(), revealNotice+"\n") {
		t.Errorf("show output does not start with the reveal line:\n%s", stdout.String())
	}
}

func TestApprovePromptNamesAReveal(t *testing.T) {
	d := newApprovalView(t, "pending", "")
	yes := true
	d.RevealSecret = &yes
	var buf bytes.Buffer

	showRequest(&buf, "Request 1 of 1:", &d)

	if !strings.Contains(buf.String(), "\n  "+revealNotice+"\n") {
		t.Errorf("approve prompt misses the reveal line:\n%s", buf.String())
	}
}

func TestNoRevealLineWithoutTheFlag(t *testing.T) {
	for name, flag := range map[string]*bool{"absent": nil, "false": new(bool)} {
		d := newApprovalView(t, "pending", "")
		d.RevealSecret = flag
		cmd, stdout, _ := newCapturingCmd(t)
		var prompt bytes.Buffer

		printDetail(cmd, &d)
		showRequest(&prompt, "Request 1 of 1:", &d)

		if strings.Contains(stdout.String(), "Hidden values") || strings.Contains(prompt.String(), "Hidden values") {
			t.Errorf("%s: a reveal line without the flag:\n%s\n%s", name, stdout.String(), prompt.String())
		}
	}
}
