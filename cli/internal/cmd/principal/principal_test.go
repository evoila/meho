// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package principal

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

func TestRootRegistersTheThreeVerbs(t *testing.T) {
	names := map[string]bool{}
	for _, child := range NewRootCmd().Commands() {
		names[child.Name()] = true
	}
	for _, want := range []string{"block", "unblock", "blocked"} {
		if !names[want] {
			t.Errorf("missing verb %s", want)
		}
	}
}

func TestBlockNeedsAReason(t *testing.T) {
	cmd := NewRootCmd()
	var stderr bytes.Buffer
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"block", "person-sub"})
	if err := cmd.Execute(); err == nil {
		t.Fatal("expected an error without --reason")
	}
	if !strings.Contains(stderr.String(), "--reason") {
		t.Errorf("error does not name --reason: %q", stderr.String())
	}
}

func TestPrintBlocked(t *testing.T) {
	var out bytes.Buffer
	printBlocked(&out, nil)
	if !strings.Contains(out.String(), "nobody is blocked") {
		t.Errorf("empty list text: %q", out.String())
	}
	out.Reset()
	printBlocked(&out, []api.BlockRead{{
		Sub: "person-sub", Scope: "*", Reason: "left the team",
		BlockedAt: time.Date(2026, 11, 21, 10, 0, 0, 0, time.UTC),
	}})
	for _, want := range []string{"person-sub", "all", "left the team", "2026-11-21T10:00:00Z"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("table lacks %q: %q", want, out.String())
		}
	}
	out.Reset()
	agent := "agent:sample-agent"
	printBlocked(&out, []api.BlockRead{{
		Sub: "agent:sample-agent", Agent: &agent, Scope: "tenant-id", Reason: "agent revoked",
		BlockedAt: time.Date(2026, 11, 21, 10, 0, 0, 0, time.UTC),
	}})
	if !strings.Contains(out.String(), "AGENT") || strings.Count(out.String(), agent) != 2 {
		t.Errorf("table does not name the agent: %q", out.String())
	}
}

func TestDecodeDetailString(t *testing.T) {
	if got := decodeDetailString(`{"detail":"principal_not_blocked"}`); got != "principal_not_blocked" {
		t.Errorf("got %q", got)
	}
	if got := decodeDetailString("plain"); got != "plain" {
		t.Errorf("got %q", got)
	}
}
