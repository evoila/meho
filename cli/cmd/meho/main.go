// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Command meho is the operator-facing CLI for the MEHO governance
// backplane. v0.1 ships three subcommands — login, status, version —
// per Goal #11. This entry point only wires the cobra tree and
// surfaces its exit code; everything else lives under internal/cmd.
package main

import (
	"os"

	"github.com/evoila/meho/cli/internal/cmd"
)

func main() {
	if err := cmd.Execute(); err != nil {
		// cobra has already printed the error to stderr via the
		// SilenceErrors/SilenceUsage settings on the root command;
		// our only job here is to propagate a non-zero exit code.
		os.Exit(1)
	}
}
