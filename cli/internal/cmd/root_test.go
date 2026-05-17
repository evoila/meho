// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/discovery"
)

// TestRootCmdHasExpectedSubcommands pins the v0.1 command set so
// a future restructuring (e.g. renaming `status` to `health`) is
// caught at compile-test time.
func TestRootCmdHasExpectedSubcommands(t *testing.T) {
	// Inhibit the production discovery fetch so this test stays
	// hermetic — no network access, no config-file reads.
	restore := setDynamicRegistrar(func(*cobra.Command) {})
	defer restore()

	root := newRootCmd()
	names := map[string]bool{}
	for _, c := range root.Commands() {
		names[c.Name()] = true
	}
	for _, want := range []string{"version", "login", "status", "operation", "retrieval", "connector", "targets", "vmware"} {
		if !names[want] {
			t.Errorf("expected built-in subcommand %q; got %v", want, names)
		}
	}
}

// TestRootHelpListsDynamicCommands wires a synthetic manifest
// through setDynamicRegistrar and confirms `meho --help` lists the
// dynamic command. This is the acceptance criterion 6 mock test:
// fake `flarp` advertisement → registered → `--help` shows it.
// The synthetic name is deliberately not a real built-in
// (`k8s`/`vault`/`vmware`/...) so the dynamic registrar's
// "refuse to shadow a built-in" guard doesn't reject it.
func TestRootHelpListsDynamicCommands(t *testing.T) {
	manifest := &discovery.CommandManifest{
		Commands: []discovery.Command{
			{Name: "flarp", Short: "Synthetic test connector"},
		},
	}
	restore := setDynamicRegistrar(func(root *cobra.Command) {
		if err := discovery.Register(root, manifest); err != nil {
			t.Fatalf("Register: %v", err)
		}
	})
	defer restore()

	root := newRootCmd()
	var stdout bytes.Buffer
	root.SetOut(&stdout)
	root.SetErr(&stdout)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("--help failed: %v", err)
	}
	help := stdout.String()
	if !strings.Contains(help, "flarp") {
		t.Errorf("`meho --help` did not list dynamic flarp command:\n%s", help)
	}
	if !strings.Contains(help, "Synthetic test connector") {
		t.Errorf("`meho --help` missing dynamic short description:\n%s", help)
	}
}

// TestRootRunsDynamicCommand confirms `meho flarp --help` resolves
// and emits the dynamic command's help, not the root help. This
// is the latter half of the acceptance criterion 6 mock test.
func TestRootRunsDynamicCommand(t *testing.T) {
	manifest := &discovery.CommandManifest{
		Commands: []discovery.Command{
			{Name: "flarp", Short: "Synthetic test connector",
				Subcommands: []discovery.Command{
					{Name: "list", Short: "List managed clusters"},
				},
			},
		},
	}
	restore := setDynamicRegistrar(func(root *cobra.Command) {
		if err := discovery.Register(root, manifest); err != nil {
			t.Fatalf("Register: %v", err)
		}
	})
	defer restore()

	root := newRootCmd()
	var stdout bytes.Buffer
	root.SetOut(&stdout)
	root.SetErr(&stdout)
	root.SetArgs([]string{"flarp", "--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("flarp --help failed: %v", err)
	}
	help := stdout.String()
	if !strings.Contains(help, "list") {
		t.Errorf("`meho flarp --help` did not list nested `list` subcommand:\n%s", help)
	}
}
