// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import "github.com/spf13/cobra"

// NewRootCmdForDocs builds the full static command tree with backplane
// discovery disabled, for offline documentation generation.
//
// The shipped `meho` binary never calls this — the docgen tool under
// cmd/docgen does, to render docs-site/reference/cli.md from the local
// command set. Discovery is disabled (setDynamicRegistrar to a no-op) so
// generation is deterministic and needs no live backplane: the committed
// reference documents the built-in verbs, and a connected backplane may
// additionally surface discovered operations at runtime.
func NewRootCmdForDocs() *cobra.Command {
	restore := setDynamicRegistrar(func(*cobra.Command) {})
	defer restore()
	return newRootCmd()
}
