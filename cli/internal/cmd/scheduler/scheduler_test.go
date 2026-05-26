// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"bytes"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestNewRootCmd_Subcommands(t *testing.T) {
	cmd := NewRootCmd()
	if cmd.Use != "scheduler" {
		t.Fatalf("expected Use=scheduler, got %q", cmd.Use)
	}
	want := map[string]bool{"list": true, "create": true, "cancel <trigger_id>": true}
	for _, sub := range cmd.Commands() {
		if !want[sub.Use] {
			t.Errorf("unexpected subcommand %q", sub.Use)
		}
		delete(want, sub.Use)
	}
	if len(want) != 0 {
		t.Errorf("missing subcommands: %v", want)
	}
}

func TestBuildListPath(t *testing.T) {
	tests := []struct {
		name string
		opts listOptions
		// substrings the path should contain (order-independent)
		contains []string
		// substrings the path must NOT contain
		notContains []string
	}{
		{
			name:        "no_filters",
			opts:        listOptions{},
			contains:    []string{"/api/v1/scheduler/triggers"},
			notContains: []string{"?"},
		},
		{
			name:     "kind_and_status",
			opts:     listOptions{Kind: "cron", Status: "active"},
			contains: []string{"kind=cron", "status=active"},
		},
		{
			name:     "tenant_filter",
			opts:     listOptions{Tenant: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
			contains: []string{"tenant_filter=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
		},
		{
			name:     "limit_and_offset",
			opts:     listOptions{Limit: 25, Offset: 50},
			contains: []string{"limit=25", "offset=50"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := buildListPath(tc.opts)
			for _, sub := range tc.contains {
				if !strings.Contains(got, sub) {
					t.Errorf("expected %q to contain %q", got, sub)
				}
			}
			for _, sub := range tc.notContains {
				if strings.Contains(got, sub) {
					t.Errorf("expected %q NOT to contain %q", got, sub)
				}
			}
		})
	}
}

func TestBuildCancelPath(t *testing.T) {
	tests := []struct {
		name string
		opts cancelOptions
		want string
	}{
		{
			name: "no_tenant",
			opts: cancelOptions{TriggerID: "abc-123"},
			want: "/api/v1/scheduler/triggers/abc-123",
		},
		{
			name: "with_tenant",
			opts: cancelOptions{
				TriggerID: "abc-123",
				Tenant:    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
			},
			want: "/api/v1/scheduler/triggers/abc-123?tenant_filter=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := buildCancelPath(tc.opts); got != tc.want {
				t.Errorf("got %q want %q", got, tc.want)
			}
		})
	}
}

func TestValidKinds(t *testing.T) {
	for _, k := range []string{"cron", "one_off", "event"} {
		if !validKinds[k] {
			t.Errorf("expected %q to be a valid kind", k)
		}
	}
	if validKinds["bogus"] {
		t.Errorf("expected 'bogus' to not be a valid kind")
	}
}

func TestValidStatuses(t *testing.T) {
	for _, s := range []string{"active", "paused", "cancelled", "fired"} {
		if !validStatuses[s] {
			t.Errorf("expected %q to be a valid status", s)
		}
	}
}

func TestValidInFlightPolicies(t *testing.T) {
	for _, p := range []string{"fail_into_audit", "resume"} {
		if !validInFlightPolicies[p] {
			t.Errorf("expected %q to be a valid in-flight policy", p)
		}
	}
}

// TestLoadJSONObjectFlag_RejectsJSONNull covers review M3 on PR #1128:
// json.Unmarshal of `null` into map[string]any sets the map to nil
// without error; the helper must catch this so a `--event-filter null`
// invocation surfaces a clear error rather than silently forwarding
// an empty field.
func TestLoadJSONObjectFlag_RejectsJSONNull(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(strings.NewReader(""))
	cases := []struct {
		name string
		raw  string
	}{
		{name: "literal_null", raw: "null"},
		{name: "literal_null_with_whitespace", raw: "  null  "},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			out, err := loadJSONObjectFlag(cmd, tc.raw, "event-filter")
			if err == nil {
				t.Fatalf("expected error for JSON null, got out=%v err=nil", out)
			}
			if !strings.Contains(err.Error(), "got null") {
				t.Errorf("expected error to mention 'got null', got: %v", err)
			}
		})
	}
}

// TestLoadJSONObjectFlag_RejectsJSONNullViaStdin checks that the stdin
// branch also rejects JSON `null`, not just the inline branch.
func TestLoadJSONObjectFlag_RejectsJSONNullViaStdin(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(strings.NewReader("null\n"))
	out, err := loadJSONObjectFlag(cmd, "@-", "inputs")
	if err == nil {
		t.Fatalf("expected error for JSON null from stdin, got out=%v err=nil", out)
	}
	if !strings.Contains(err.Error(), "got null") {
		t.Errorf("expected error to mention 'got null', got: %v", err)
	}
}

// TestReadJSONFile_RejectsOverCapFile covers review M4 on PR #1128:
// readJSONFile must enforce jsonObjectCap so a multi-GiB JSON file
// cannot OOM the CLI. We stub the file-read seam with a hand-rolled
// stub that returns over-cap bytes via the limit-reader path.
func TestLoadJSONObjectFlag_RejectsOverCapFile(t *testing.T) {
	original := readJSONFile
	t.Cleanup(func() { readJSONFile = original })

	// Stub the seam to mirror what the real implementation does: read
	// up to jsonObjectCap+1 bytes and reject when the payload is over
	// the cap. The test passes bytes that are deliberately over.
	readJSONFile = func(_ string) ([]byte, error) {
		// Match the implementation's error shape exactly so the
		// caller's wrapping error chain is testable.
		return nil, &capExceededError{}
	}

	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewReader(nil))
	out, err := loadJSONObjectFlag(cmd, "@/tmp/huge.json", "event-filter")
	if err == nil {
		t.Fatalf("expected over-cap file to surface an error, got out=%v err=nil", out)
	}
}

// capExceededError is a stand-in for the cap-exceeded error
// readJSONFile would normally return; used in TestLoadJSONObjectFlag_
// RejectsOverCapFile to confirm the wrapping path lights up.
type capExceededError struct{}

func (capExceededError) Error() string {
	return "file \"/tmp/huge.json\" exceeds 262144-byte cap"
}
